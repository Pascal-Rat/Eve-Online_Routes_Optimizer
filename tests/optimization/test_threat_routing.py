"""Threat observations must constrain every transit, regardless of contract reward."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import pytest

from eve_courier_optimizer.domain import (
    GateEvidence,
    GateThreatEvent,
    PlanningConstraints,
    ProofStatus,
    ThreatCategory,
    TravelTimeModel,
)
from eve_courier_optimizer.optimization import RouteOptimizer, SolverConfig
from eve_courier_optimizer.routing.route_problem import RouteProblem
from eve_courier_optimizer.routing.security import observed_security_policy
from eve_courier_optimizer.routing.universe import Region, SdeMetadata, SolarSystem, UniverseGraph
from tests.conftest import make_contract, make_snapshot


@pytest.mark.parametrize(
    ("detour", "horizon", "expected_selected", "expected_finish"),
    [
        pytest.param(False, 80, (2,), 20, id="both-entrances-camped"),
        pytest.param(True, 80, (1, 2), 80, id="permitted-detour-fits"),
        pytest.param(True, 50, (2,), 20, id="permitted-detour-too-long"),
    ],
)
def test_valuable_contract_behind_threatened_transit_systems(
    now: datetime,
    detour: bool,
    horizon: int,
    expected_selected: tuple[int, ...],
    expected_finish: int,
) -> None:
    # Two short entrances to system 3 are camped (2 and 4); neither contract endpoint is.
    # The optional three-jump detour is 1-5-6-3. System 7 offers a modest nearby contract.
    edges = [(1, 2), (2, 3), (1, 4), (4, 3), (1, 7)]
    if detour:
        edges.extend(((1, 5), (5, 6), (6, 3)))
    adjacency: dict[int, list[int]] = {i: [] for i in range(1, 8)}
    for source, target in edges:
        adjacency[source].append(target)
        adjacency[target].append(source)
    graph = UniverseGraph(
        systems={i: SolarSystem(i, 10, f"System {i}", 0.9) for i in adjacency},
        adjacency={i: tuple(sorted(neighbors)) for i, neighbors in adjacency.items()},
        station_systems={100 + i: i for i in adjacency},
        regions={10: Region(10, "Trap regression")},
        metadata=SdeMetadata(1, now.isoformat(), "test://threat-routing"),
    )
    categories = frozenset({ThreatCategory.GATE_CAMP})
    events = tuple(
        GateThreatEvent(
            killmail_id=1000 + system,
            occurred_at=now,
            system_id=system,
            region_id=10,
            gate_id=500 + system,
            distance_to_gate_m=0,
            evidence=GateEvidence.ZKILL_LOCATION,
            categories=categories,
            victim_ship_type_id=1,
            player_attacker_count=2,
        )
        for system in (2, 4)
    )
    snapshot = replace(
        make_snapshot(
            now,
            make_contract(now, 1, 101, 103, reward=10**12),
            make_contract(now, 2, 101, 107, reward=100),
        ),
        threat_intel_fetched_at=now,
        threat_window_seconds=7200,
        threat_gate_radius_m=250_000,
        threat_coverage_region_ids=(10,),
        threat_killmails_seen=len(events),
        gate_threat_events=events,
    )
    constraints = PlanningConstraints(
        start_system_id=1,
        cargo_capacity_units=100,
        collateral_budget_units=1000,
        horizon_seconds=horizon,
        snapshot_time=now,
        travel=TravelTimeModel(10, 0),
    )
    config = SolverConfig(max_time_seconds=2, minimize_finish_time_after_proof=False)
    # Establish that the bait is attractive without threat restrictions. Reusing the graph
    # also catches accidental reuse of cached distances from the unrestricted policy.
    unrestricted = RouteProblem.from_snapshot(snapshot, graph, constraints)
    control = RouteOptimizer(unrestricted, graph, config=config).solve()
    assert 1 in control.selected_contract_ids
    assert any({2, 4}.intersection(leg.jump_path) for leg in control.travel_legs)

    policy = observed_security_policy(
        snapshot, minimum_security=0.45, threat_categories=categories, threat_min_events=1
    )
    assert policy.threat_avoided_system_ids == frozenset({2, 4})
    assert all(policy.rejection_reason(system, 0.9) is None for system in (1, 3, 7))
    problem = RouteProblem.from_snapshot(snapshot, graph, replace(constraints, security=policy))
    result = RouteOptimizer(problem, graph, config=config).solve()

    assert result.certificate.status is ProofStatus.PROVEN_OPTIMAL
    assert result.certificate.feasibility_verified
    assert result.selected_contract_ids == expected_selected
    assert result.finish_seconds == expected_finish
    assert all({2, 4}.isdisjoint(leg.jump_path) for leg in result.travel_legs)
    if 1 in result.selected_contract_ids:
        assert any({5, 6}.issubset(leg.jump_path) for leg in result.travel_legs)
