from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from eve_courier_optimizer.domain import (
    CollateralMode,
    GateEvidence,
    GateThreatEvent,
    PlanningConstraints,
    SecurityBand,
    SecurityPolicy,
    SystemKillActivity,
    ThreatCategory,
    TravelTimeModel,
)
from eve_courier_optimizer.execution import (
    constraints_for_replan,
    initial_execution_state,
    read_execution_state,
    record_delivery,
    record_pickup,
    record_route_system,
    write_execution_state,
)
from eve_courier_optimizer.planning import prepare_problem
from eve_courier_optimizer.sde import UniverseGraph
from eve_courier_optimizer.solver import SolverConfig, solve_exact

from .conftest import make_contract, make_snapshot


def constraints(now: datetime, mode: CollateralMode) -> PlanningConstraints:
    return PlanningConstraints(
        start_system_id=1,
        cargo_capacity_units=20,
        collateral_budget_units=200,
        horizon_seconds=3_600,
        snapshot_time=now,
        collateral_mode=mode,
        travel=TravelTimeModel(10, 1),
        security=SecurityPolicy(0.45),
    )


def test_locked_plan_becomes_mandatory_commitment_and_can_advance(
    now: datetime,
    tiny_graph: UniverseGraph,
    tmp_path: Path,
) -> None:
    public = make_contract(now, 1, 101, 102, reward=500)
    snapshot = make_snapshot(now, public)
    prepared = prepare_problem(snapshot, tiny_graph, constraints(now, CollateralMode.LOCKED))
    result = solve_exact(prepared, tiny_graph, config=SolverConfig(max_time_seconds=10))
    state = initial_execution_state(
        prepared.problem.constraints,
        prepared.problem.contracts,
        (),
        result,
    )
    assert len(state.active_shipments) == 1
    assert not state.active_shipments[0].picked

    picked = record_pickup(state, snapshot, tiny_graph, 1, now + timedelta(minutes=1))
    assert picked.active_shipments[0].picked
    assert picked.current_system_id == 1
    delivered = record_delivery(picked, 1, now + timedelta(minutes=2))
    assert not delivered.active_shipments
    assert delivered.current_system_id == 2
    assert delivered.completed_contract_ids == (1,)

    path = tmp_path / "state.json"
    write_execution_state(path, delivered)
    assert read_execution_state(path) == delivered


def test_rolling_pickup_accepts_new_contract_and_replan_uses_remaining_time(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    public = make_contract(now, 1, 101, 102, reward=500)
    snapshot = make_snapshot(now, public)
    prepared = prepare_problem(snapshot, tiny_graph, constraints(now, CollateralMode.ROLLING))
    result = solve_exact(prepared, tiny_graph, config=SolverConfig(max_time_seconds=10))
    state = initial_execution_state(
        prepared.problem.constraints,
        prepared.problem.contracts,
        (),
        result,
    )
    assert not state.active_shipments
    picked = record_pickup(state, snapshot, tiny_graph, 1, now + timedelta(minutes=5))
    assert picked.active_shipments[0].picked
    assert picked.active_shipments[0].deadline == now + timedelta(days=1, minutes=5)
    replanning = constraints_for_replan(picked, snapshot)
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
    prepared = prepare_problem(snapshot, tiny_graph, route_constraints)
    result = solve_exact(prepared, tiny_graph, config=SolverConfig(max_time_seconds=10))
    state = initial_execution_state(route_constraints, (), (), result)

    assert state.terminal_system_id == 1
    assert state.remaining_required_system_ids == frozenset({3})
    assert state.max_simultaneous_contracts == 1

    reached = record_route_system(state, 3, now + timedelta(minutes=1))
    assert reached.current_system_id == 3
    assert reached.remaining_required_system_ids == frozenset()
    replanning = constraints_for_replan(reached, snapshot)
    assert replanning.start_system_id == 3
    assert not replanning.return_to_start
    assert replanning.finish_system_id == 1
    assert replanning.max_simultaneous_contracts == 1

    path = tmp_path / "route-state.json"
    write_execution_state(path, reached)
    serialized = json.loads(path.read_text())
    assert serialized["schema_version"] == 3
    assert serialized["terminal_system_id"] == 1
    assert serialized["max_simultaneous_contracts"] == 1
    assert read_execution_state(path) == reached


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
    prepared = prepare_problem(snapshot, tiny_graph, limited)
    result = solve_exact(prepared, tiny_graph, config=SolverConfig(max_time_seconds=10))
    state = initial_execution_state(limited, prepared.problem.contracts, (), result)

    picked = record_pickup(state, snapshot, tiny_graph, 1, now + timedelta(minutes=1))
    with pytest.raises(ValueError, match="simultaneous-contract limit"):
        record_pickup(picked, snapshot, tiny_graph, 2, now + timedelta(minutes=2))


def test_execution_state_rejects_illegal_transitions(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    public = make_contract(now, 1, 101, 102, collateral=500)
    snapshot = make_snapshot(now, public)
    prepared = prepare_problem(snapshot, tiny_graph, constraints(now, CollateralMode.ROLLING))
    result = solve_exact(prepared, tiny_graph, config=SolverConfig(max_time_seconds=10))
    state = initial_execution_state(
        prepared.problem.constraints,
        prepared.problem.contracts,
        (),
        result,
    )
    with pytest.raises(ValueError, match="collateral"):
        record_pickup(state, snapshot, tiny_graph, 1, now + timedelta(minutes=1))
    with pytest.raises(ValueError, match="not an active"):
        record_delivery(state, 1, now + timedelta(minutes=1))


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
    prepared = prepare_problem(snapshot, tiny_graph, initial_constraints)
    result = solve_exact(prepared, tiny_graph, config=SolverConfig(max_time_seconds=10))
    state = initial_execution_state(
        prepared.problem.constraints,
        prepared.problem.contracts,
        (),
        result,
    )

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
    replanning = constraints_for_replan(state, refreshed)
    assert replanning.security.gank_avoided_system_ids == frozenset({4})
    assert replanning.security.gank_activity_fetched_at == now + timedelta(minutes=1)

    path = tmp_path / "risk-state.json"
    write_execution_state(path, state)
    restored = read_execution_state(path)
    assert restored.security.allowed_bands == frozenset({SecurityBand.HIGH, SecurityBand.LOW})
    assert restored.security.gank_ship_kill_threshold == 5

    without_activity = replace(
        refreshed,
        system_kills_fetched_at=None,
        system_kill_activity=(),
    )
    with pytest.raises(ValueError, match="no system-kill activity"):
        constraints_for_replan(state, without_activity)


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
    prepared = prepare_problem(
        snapshot,
        tiny_graph,
        replace(
            constraints(now, CollateralMode.LOCKED),
            security=policy,
            required_system_ids=frozenset({3}),
        ),
    )
    result = solve_exact(prepared, tiny_graph, config=SolverConfig(max_time_seconds=10))
    state = initial_execution_state(
        prepared.problem.constraints,
        prepared.problem.contracts,
        (),
        result,
    )

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
    replanning = constraints_for_replan(state, refreshed)
    assert replanning.security.threat_avoided_system_ids == frozenset({4})
    assert replanning.security.threat_categories == categories
    assert replanning.security.threat_window_seconds == 43_200
    assert replanning.security.threat_gate_radius_m == 100_000
    assert replanning.security.threat_incomplete_region_ids == frozenset({20})

    path = tmp_path / "threat-state.json"
    write_execution_state(path, state)
    assert read_execution_state(path) == state

    with pytest.raises(ValueError, match="no zKill intel"):
        constraints_for_replan(
            state,
            replace(
                refreshed,
                threat_intel_fetched_at=None,
                threat_window_seconds=None,
                threat_gate_radius_m=None,
                threat_coverage_region_ids=(),
                threat_incomplete_region_ids=(),
                threat_killmails_seen=0,
                gate_threat_events=(),
            ),
        )
