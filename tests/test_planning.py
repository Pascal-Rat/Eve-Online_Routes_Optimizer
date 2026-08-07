from __future__ import annotations

from dataclasses import replace
from datetime import datetime

import pytest

from eve_courier_optimizer.domain import (
    ActiveShipment,
    CollateralMode,
    PlanningConstraints,
    RoutableContract,
    SecurityBand,
    SecurityPolicy,
    ThreatCategory,
    TravelTimeModel,
)
from eve_courier_optimizer.planning import prepare_problem, rank_single_contracts
from eve_courier_optimizer.sde import Region, UniverseGraph
from eve_courier_optimizer.snapshot import ContractSnapshot

from .conftest import make_contract, make_snapshot


def constraints(now: datetime, **overrides: object) -> PlanningConstraints:
    values: dict[str, object] = {
        "start_system_id": 1,
        "cargo_capacity_units": 20,
        "collateral_budget_units": 200,
        "horizon_seconds": 1_000,
        "snapshot_time": now,
        "travel": TravelTimeModel(10, 1),
        "security": SecurityPolicy(0.45),
    }
    values.update(overrides)
    return PlanningConstraints(**values)  # type: ignore[arg-type]


def test_prepare_problem_applies_declared_and_safe_filters(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    good = make_contract(now, 1, 101, 102)
    too_big = make_contract(now, 2, 101, 102, volume=21)
    lowsec = make_contract(now, 3, 101, 104)
    unsupported = make_contract(now, 4, 999_999, 102)
    zero_reward = make_contract(now, 5, 101, 102, reward=0)
    expired = make_contract(now, 6, 101, 102, expiry_hours=-1)
    snapshot = make_snapshot(now, good, too_big, lowsec, unsupported, zero_reward, expired)

    prepared = prepare_problem(snapshot, tiny_graph, constraints(now))

    assert [item.contract.contract_id for item in prepared.problem.contracts] == [1]
    assert dict(prepared.problem.scope.policy_exclusions) == {
        "security_policy": 1,
        "unsupported_non_npc_station_endpoint": 1,
    }
    assert dict(prepared.problem.scope.safe_reductions) == {
        "listing_expired": 1,
        "nonpositive_reward": 1,
        "volume_exceeds_capacity": 1,
    }


def test_candidate_cap_marks_proof_scope_as_truncated(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    snapshot = make_snapshot(
        now,
        make_contract(now, 1, 101, 102, reward=100),
        make_contract(now, 2, 101, 102, reward=300),
    )
    prepared = prepare_problem(snapshot, tiny_graph, constraints(now), max_candidates=1)
    assert not prepared.problem.scope.is_untruncated
    assert dict(prepared.problem.scope.heuristic_reductions) == {"candidate_cap": 1}
    assert prepared.problem.contracts[0].contract.contract_id == 2


def test_active_contract_still_visible_in_snapshot_is_not_double_counted(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    public = make_contract(now, 1, 101, 102, reward=500)
    active = ActiveShipment(RoutableContract(public, 1, 2), deadline=public.date_expired)

    prepared = prepare_problem(
        make_snapshot(now, public),
        tiny_graph,
        constraints(now),
        active_shipments=(active,),
    )

    assert not prepared.problem.contracts
    assert prepared.problem.active_shipments == (active,)
    assert dict(prepared.problem.scope.safe_reductions) == {"already_active_commitment": 1}


def test_solo_ranking_prefers_reward_rate(now: datetime, tiny_graph: UniverseGraph) -> None:
    snapshot = make_snapshot(
        now,
        make_contract(now, 1, 101, 103, reward=100),
        make_contract(now, 2, 101, 102, reward=90),
    )
    prepared = prepare_problem(snapshot, tiny_graph, constraints(now))
    ranking = rank_single_contracts(prepared)
    assert ranking[0].contract.contract.contract_id == 2


def test_snapshot_sde_mismatch_blocks_proof(now: datetime, tiny_graph: UniverseGraph) -> None:
    snapshot = ContractSnapshot(now, "x", 999, (10,), ())
    with pytest.raises(ValueError, match="snapshot uses SDE"):
        prepare_problem(snapshot, tiny_graph, constraints(now))


def test_start_must_exist_and_be_permitted(now: datetime, tiny_graph: UniverseGraph) -> None:
    snapshot = make_snapshot(now)
    with pytest.raises(ValueError, match="not present"):
        prepare_problem(snapshot, tiny_graph, constraints(now, start_system_id=999))
    with pytest.raises(ValueError, match="excluded"):
        prepare_problem(snapshot, tiny_graph, constraints(now, start_system_id=4))


def test_required_route_system_must_exist_and_obey_route_policy(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    snapshot = make_snapshot(now)
    with pytest.raises(ValueError, match="required route system 999 is not present"):
        prepare_problem(
            snapshot,
            tiny_graph,
            constraints(now, required_system_ids=frozenset({999})),
        )
    with pytest.raises(
        ValueError,
        match="required route system Low is excluded by security policy",
    ):
        prepare_problem(
            snapshot,
            tiny_graph,
            constraints(now, required_system_ids=frozenset({4})),
        )


def test_zero_simultaneous_limit_is_a_safe_no_contract_reduction(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    snapshot = make_snapshot(now, make_contract(now, 1, 101, 102))
    prepared = prepare_problem(
        snapshot,
        tiny_graph,
        constraints(now, max_simultaneous_contracts=0),
    )
    assert prepared.problem.contracts == ()
    assert dict(prepared.problem.scope.safe_reductions) == {
        "simultaneous_contract_limit_zero": 1,
    }


def test_threat_aware_proof_requires_every_route_reachable_region(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    graph = UniverseGraph(
        systems={
            **tiny_graph.systems,
            3: replace(tiny_graph.systems[3], region_id=11),
        },
        adjacency=tiny_graph.adjacency,
        station_systems=tiny_graph.station_systems,
        regions={**tiny_graph.regions, 11: Region(11, "Transit Region")},
        metadata=tiny_graph.metadata,
    )
    policy = SecurityPolicy(
        minimum_security=0.45,
        threat_categories=frozenset({ThreatCategory.ANY_GATE_PVP}),
        threat_min_events=1,
        threat_intel_fetched_at=now,
        threat_window_seconds=7_200,
        threat_gate_radius_m=250_000,
        threat_coverage_region_ids=frozenset({10}),
    )
    with pytest.raises(ValueError, match="does not cover.*Transit Region"):
        prepare_problem(make_snapshot(now), graph, constraints(now, security=policy))

    complete_policy = replace(policy, threat_coverage_region_ids=frozenset({10, 11}))
    prepared = prepare_problem(
        make_snapshot(now),
        graph,
        constraints(now, security=complete_policy),
    )
    assert prepared.problem.scope.eligible_contracts == 0


def test_policy_exclusions_distinguish_manual_security_and_activity(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    snapshot = make_snapshot(
        now,
        make_contract(now, 1, 101, 102),
        make_contract(now, 2, 101, 103),
        make_contract(now, 3, 101, 104),
    )
    policy = SecurityPolicy(
        minimum_security=None,
        avoided_system_ids=frozenset({3}),
        allowed_bands=frozenset({SecurityBand.HIGH}),
        gank_avoided_system_ids=frozenset({2}),
        gank_ship_kill_threshold=5,
        gank_activity_fetched_at=now,
    )
    prepared = prepare_problem(snapshot, tiny_graph, constraints(now, security=policy))
    assert not prepared.problem.contracts
    assert dict(prepared.problem.scope.policy_exclusions) == {
        "gank_activity_policy": 1,
        "manual_avoid_policy": 1,
        "security_policy": 1,
    }


def test_rolling_mode_filters_listing_that_cannot_be_reached(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    contract = make_contract(now, 1, 103, 102, expiry_hours=1)
    snapshot = make_snapshot(now, contract)
    very_slow = constraints(
        now,
        collateral_mode=CollateralMode.ROLLING,
        travel=TravelTimeModel(2_000, 1),
        horizon_seconds=20_000,
    )
    prepared = prepare_problem(snapshot, tiny_graph, very_slow)
    assert not prepared.problem.contracts
    reductions = dict(prepared.problem.scope.safe_reductions)
    assert reductions["solo_lower_bound_misses_listing_expiry"] == 1


def test_rolling_mode_treats_exact_expiry_as_too_late(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    contract = make_contract(now, 1, 103, 102, expiry_hours=1)
    prepared = prepare_problem(
        make_snapshot(now, contract),
        tiny_graph,
        constraints(
            now,
            collateral_mode=CollateralMode.ROLLING,
            travel=TravelTimeModel(1_800, 1),
            horizon_seconds=10_000,
        ),
    )
    assert not prepared.problem.contracts
    assert dict(prepared.problem.scope.safe_reductions)[
        "solo_lower_bound_misses_listing_expiry"
    ] == 1
