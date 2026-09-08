from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from eve_courier_optimizer.application.courier_trip import CourierTrip
from eve_courier_optimizer.application.trip_file import read_trip, write_trip
from eve_courier_optimizer.domain import (
    CollateralMode,
    GateEvidence,
    GateThreatEvent,
    SecurityBand,
    SecurityPolicy,
    SystemKillActivity,
    ThreatCategory,
)
from eve_courier_optimizer.optimization import RouteOptimizer, SolverConfig
from eve_courier_optimizer.routing.route_problem import RouteProblem
from eve_courier_optimizer.routing.universe import UniverseGraph
from tests.support.scenarios import make_contract, make_snapshot
from tests.support.scenarios import trip_constraints as constraints


def test_locked_plan_becomes_mandatory_commitment_and_can_advance(
    now: datetime,
    tiny_graph: UniverseGraph,
    tmp_path: Path,
) -> None:
    public = make_contract(now, 1, 101, 102, reward=500)
    snapshot = make_snapshot(now, public)
    problem = RouteProblem.from_snapshot(
        snapshot, tiny_graph, constraints(now, CollateralMode.LOCKED)
    )
    result = RouteOptimizer(problem, tiny_graph, config=SolverConfig(max_time_seconds=10)).solve()
    state = CourierTrip.from_plan(problem.constraints, problem.contracts, (), result)
    assert len(state.active_shipments) == 1
    assert not state.active_shipments[0].picked

    picked = state.pick_up(snapshot, tiny_graph, 1, now + timedelta(minutes=1))
    assert picked.active_shipments[0].picked
    assert picked.current_system_id == 1
    delivered = picked.deliver(1, now + timedelta(minutes=2))
    assert not delivered.active_shipments
    assert delivered.current_system_id == 2
    assert delivered.completed_contract_ids == (1,)

    path = tmp_path / "state.json"
    write_trip(path, delivered)
    assert read_trip(path) == delivered


def test_rolling_pickup_accepts_new_contract_and_replan_uses_remaining_time(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    public = make_contract(now, 1, 101, 102, reward=500)
    snapshot = make_snapshot(now, public)
    problem = RouteProblem.from_snapshot(
        snapshot, tiny_graph, constraints(now, CollateralMode.ROLLING)
    )
    result = RouteOptimizer(problem, tiny_graph, config=SolverConfig(max_time_seconds=10)).solve()
    state = CourierTrip.from_plan(problem.constraints, problem.contracts, (), result)
    assert not state.active_shipments
    picked = state.pick_up(snapshot, tiny_graph, 1, now + timedelta(minutes=5))
    assert picked.active_shipments[0].picked
    assert picked.active_shipments[0].deadline == now + timedelta(days=1, minutes=5)
    replanning = picked.replanning_constraints(snapshot)
    assert replanning.start_system_id == 1
    assert replanning.horizon_seconds == 3_300


def test_execution_preserves_original_loop_waypoints_and_parcel_limit(
    now: datetime,
    tiny_graph: UniverseGraph,
    tmp_path: Path,
) -> None:
    route_constraints = replace(
        constraints(now, CollateralMode.ROLLING),
        cargo_capacity_units=0,
        required_system_ids=frozenset({3}),
        max_simultaneous_contracts=1,
    )
    snapshot = make_snapshot(now)
    problem = RouteProblem.from_snapshot(snapshot, tiny_graph, route_constraints)
    result = RouteOptimizer(problem, tiny_graph, config=SolverConfig(max_time_seconds=10)).solve()
    state = CourierTrip.from_plan(route_constraints, (), (), result)

    assert state.terminal_system_id == 1
    assert state.remaining_required_system_ids == frozenset({3})
    assert state.max_simultaneous_contracts == 1

    reached = state.reach_system(3, now + timedelta(minutes=1))
    assert reached.current_system_id == 3
    assert reached.remaining_required_system_ids == frozenset()
    replanning = reached.replanning_constraints(snapshot)
    assert replanning.start_system_id == 3
    assert not replanning.return_to_start
    assert replanning.finish_system_id == 1
    assert replanning.max_simultaneous_contracts == 1

    path = tmp_path / "route-state.json"
    write_trip(path, reached)
    serialized = json.loads(path.read_text())
    assert serialized["schema_version"] == 3
    assert serialized["terminal_system_id"] == 1
    assert serialized["max_simultaneous_contracts"] == 1
    assert read_trip(path) == reached


def test_execution_enforces_simultaneous_contract_limit_on_real_pickups(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    first = make_contract(now, 1, 101, 102, volume=1)
    second = make_contract(now, 2, 101, 103, volume=1)
    snapshot = make_snapshot(now, first, second)
    limited = replace(
        constraints(now, CollateralMode.ROLLING),
        max_simultaneous_contracts=1,
    )
    problem = RouteProblem.from_snapshot(snapshot, tiny_graph, limited)
    result = RouteOptimizer(problem, tiny_graph, config=SolverConfig(max_time_seconds=10)).solve()
    state = CourierTrip.from_plan(limited, problem.contracts, (), result)

    picked = state.pick_up(snapshot, tiny_graph, 1, now + timedelta(minutes=1))
    with pytest.raises(ValueError, match="simultaneous-contract limit"):
        picked.pick_up(snapshot, tiny_graph, 2, now + timedelta(minutes=2))


def test_execution_state_rejects_illegal_transitions(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    public = make_contract(now, 1, 101, 102, collateral=500)
    snapshot = make_snapshot(now, public)
    problem = RouteProblem.from_snapshot(
        snapshot, tiny_graph, constraints(now, CollateralMode.ROLLING)
    )
    result = RouteOptimizer(problem, tiny_graph, config=SolverConfig(max_time_seconds=10)).solve()
    state = CourierTrip.from_plan(problem.constraints, problem.contracts, (), result)
    with pytest.raises(ValueError, match="collateral"):
        state.pick_up(snapshot, tiny_graph, 1, now + timedelta(minutes=1))
    with pytest.raises(ValueError, match="not an active"):
        state.deliver(1, now + timedelta(minutes=1))


def test_replan_refreshes_gank_activity_and_exempts_mandatory_endpoints(
    now: datetime,
    tiny_graph: UniverseGraph,
    tmp_path: Path,
) -> None:
    public = make_contract(now, 1, 101, 102, reward=500)
    snapshot = make_snapshot(now, public)
    risk_policy = SecurityPolicy(
        minimum_security=None,
        allowed_bands=frozenset({SecurityBand.HIGH, SecurityBand.LOW}),
        gank_avoided_system_ids=frozenset({4}),
        gank_ship_kill_threshold=5,
        gank_activity_fetched_at=now,
    )
    initial_constraints = replace(
        constraints(now, CollateralMode.LOCKED),
        security=risk_policy,
        required_system_ids=frozenset({3}),
    )
    problem = RouteProblem.from_snapshot(snapshot, tiny_graph, initial_constraints)
    result = RouteOptimizer(problem, tiny_graph, config=SolverConfig(max_time_seconds=10)).solve()
    state = CourierTrip.from_plan(problem.constraints, problem.contracts, (), result)

    refreshed = replace(
        snapshot,
        fetched_at=now + timedelta(minutes=1),
        system_kills_fetched_at=now + timedelta(minutes=1),
        system_kill_activity=(
            SystemKillActivity(1, 50, 0, 0),  # current / required origin
            SystemKillActivity(2, 30, 0, 0),  # required destination
            SystemKillActivity(3, 40, 0, 0),  # required route system
            SystemKillActivity(4, 9, 0, 0),
        ),
    )
    replanning = state.replanning_constraints(refreshed)
    assert replanning.security.gank_avoided_system_ids == frozenset({4})
    assert replanning.security.gank_activity_fetched_at == now + timedelta(minutes=1)

    path = tmp_path / "risk-state.json"
    write_trip(path, state)
    restored = read_trip(path)
    assert restored.security.allowed_bands == frozenset({SecurityBand.HIGH, SecurityBand.LOW})
    assert restored.security.gank_ship_kill_threshold == 5

    without_activity = replace(
        refreshed,
        system_kills_fetched_at=None,
        system_kill_activity=(),
    )
    with pytest.raises(ValueError, match="system-kill activity"):
        state.replanning_constraints(without_activity)


def test_replan_refreshes_gate_threats_and_preserves_auditable_policy(
    now: datetime,
    tiny_graph: UniverseGraph,
    tmp_path: Path,
) -> None:
    public = make_contract(now, 1, 101, 102, reward=500)
    snapshot = make_snapshot(now, public)
    categories = frozenset({ThreatCategory.SMARTBOMB, ThreatCategory.GATE_CAMP})
    policy = SecurityPolicy(
        minimum_security=None,
        allowed_bands=frozenset({SecurityBand.HIGH, SecurityBand.LOW}),
        threat_avoided_system_ids=frozenset({4}),
        threat_categories=categories,
        threat_min_events=1,
        threat_intel_fetched_at=now,
        threat_window_seconds=86_400,
        threat_gate_radius_m=250_000,
        threat_coverage_region_ids=frozenset({10}),
    )
    problem = RouteProblem.from_snapshot(
        snapshot,
        tiny_graph,
        replace(
            constraints(now, CollateralMode.LOCKED),
            security=policy,
            required_system_ids=frozenset({3}),
        ),
    )
    result = RouteOptimizer(problem, tiny_graph, config=SolverConfig(max_time_seconds=10)).solve()
    state = CourierTrip.from_plan(problem.constraints, problem.contracts, (), result)

    def event(killmail_id: int, system_id: int) -> GateThreatEvent:
        return GateThreatEvent(
            killmail_id=killmail_id,
            occurred_at=now + timedelta(seconds=1),
            system_id=system_id,
            region_id=10,
            gate_id=500 + system_id,
            distance_to_gate_m=0,
            evidence=GateEvidence.ZKILL_LOCATION,
            categories=frozenset({ThreatCategory.SMARTBOMB}),
            victim_ship_type_id=1,
            player_attacker_count=1,
        )

    refreshed = replace(
        snapshot,
        fetched_at=now + timedelta(minutes=1),
        threat_intel_fetched_at=now + timedelta(minutes=1),
        threat_window_seconds=43_200,
        threat_gate_radius_m=100_000,
        threat_coverage_region_ids=(10,),
        threat_incomplete_region_ids=(20,),
        threat_killmails_seen=4,
        gate_threat_events=(event(1, 1), event(2, 2), event(3, 3), event(4, 4)),
    )
    replanning = state.replanning_constraints(refreshed)
    assert replanning.security.threat_avoided_system_ids == frozenset({4})
    assert replanning.security.threat_categories == categories
    assert replanning.security.threat_window_seconds == 43_200
    assert replanning.security.threat_gate_radius_m == 100_000
    assert replanning.security.threat_incomplete_region_ids == frozenset({20})

    path = tmp_path / "threat-state.json"
    write_trip(path, state)
    assert read_trip(path) == state

    with pytest.raises(ValueError, match="zKill intel"):
        state.replanning_constraints(
            replace(
                refreshed,
                threat_intel_fetched_at=None,
                threat_window_seconds=None,
                threat_gate_radius_m=None,
                threat_coverage_region_ids=(),
                threat_incomplete_region_ids=(),
                threat_killmails_seen=0,
                gate_threat_events=(),
            )
        )
