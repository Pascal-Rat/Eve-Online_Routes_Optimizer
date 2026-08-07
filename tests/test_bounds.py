from __future__ import annotations

import random
from dataclasses import replace
from datetime import datetime

from eve_courier_optimizer.bounds import build_selection_cuts, solve_system_relaxation
from eve_courier_optimizer.domain import (
    CollateralMode,
    PlanningConstraints,
    ProofStatus,
    SecurityPolicy,
    TravelTimeModel,
)
from eve_courier_optimizer.planning import prepare_problem
from eve_courier_optimizer.reference_solver import solve_reference
from eve_courier_optimizer.reporting import solve_result_to_dict
from eve_courier_optimizer.sde import UniverseGraph
from eve_courier_optimizer.solver import SolverConfig, solve_exact

from .conftest import make_contract, make_snapshot


def constraints(
    now: datetime,
    *,
    horizon: int = 100,
    collateral: int = 1_000,
    mode: CollateralMode = CollateralMode.LOCKED,
) -> PlanningConstraints:
    return PlanningConstraints(
        start_system_id=1,
        cargo_capacity_units=100,
        collateral_budget_units=collateral,
        horizon_seconds=horizon,
        snapshot_time=now,
        collateral_mode=mode,
        travel=TravelTimeModel(seconds_per_jump=10, service_seconds=1),
        security=SecurityPolicy(0.45),
    )


def test_system_relaxation_preserves_shared_route_value(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    first = make_contract(now, 1, 101, 103, volume=10, collateral=100, reward=600)
    second = make_contract(now, 2, 101, 103, volume=10, collateral=100, reward=500)
    prepared = prepare_problem(
        make_snapshot(now, first, second),
        tiny_graph,
        constraints(now, horizon=44),
    )

    bound = solve_system_relaxation(prepared, max_time_seconds=2)

    assert bound.status_name == "OPTIMAL"
    assert bound.objective_units == 1_100
    assert bound.upper_bound_units == 1_100


def test_system_relaxation_is_never_below_reference_optimum_on_random_cases(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    rng = random.Random(23)
    stations = (101, 102, 103)
    for case in range(40):
        contracts = []
        for index in range(5):
            origin_index = rng.randrange(0, 2)
            destination_index = rng.randrange(origin_index + 1, 3)
            contracts.append(
                make_contract(
                    now,
                    case * 10 + index + 1,
                    stations[origin_index],
                    stations[destination_index],
                    volume=rng.randint(1, 20),
                    collateral=rng.randint(20, 100),
                    reward=rng.randint(100, 1_000),
                )
            )
        prepared = prepare_problem(
            make_snapshot(now, *contracts),
            tiny_graph,
            replace(
                constraints(now),
                cargo_capacity_units=rng.randint(12, 30),
                collateral_budget_units=rng.randint(100, 220),
                max_simultaneous_contracts=rng.choice((None, 1, 2, 3)),
            ),
        )
        exact = solve_reference(prepared, contract_limit=10)
        relaxed = solve_system_relaxation(prepared, max_time_seconds=2)

        assert relaxed.upper_bound_units is not None
        assert exact.objective_units <= relaxed.upper_bound_units


def test_rolling_relaxation_does_not_apply_locked_total_collateral(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    first = make_contract(now, 1, 101, 102, collateral=100, reward=500)
    second = make_contract(now, 2, 102, 103, collateral=100, reward=700)
    prepared = prepare_problem(
        make_snapshot(now, first, second),
        tiny_graph,
        constraints(now, collateral=100, mode=CollateralMode.ROLLING),
    )

    relaxed = solve_system_relaxation(prepared, max_time_seconds=2)

    assert relaxed.status_name == "OPTIMAL"
    assert relaxed.upper_bound_units == 1_200


def test_pair_conflicts_and_clique_cut_capture_joint_horizon_infeasibility(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    contracts = tuple(
        make_contract(now, contract_id, 101, 103, volume=1, collateral=10, reward=100)
        for contract_id in (1, 2, 3)
    )
    prepared = prepare_problem(
        make_snapshot(now, *contracts),
        tiny_graph,
        constraints(now, horizon=43),
    )

    cuts = build_selection_cuts(prepared)

    assert cuts.pairs == ((1, 2), (1, 3), (2, 3))
    assert cuts.cliques == ((1, 2, 3),)


def test_pair_cut_accounts_for_capacity_when_shared_travel_needs_both_parcels(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    first = make_contract(now, 1, 101, 103, volume=15, collateral=10, reward=100)
    second = make_contract(now, 2, 101, 103, volume=15, collateral=10, reward=100)
    prepared = prepare_problem(
        make_snapshot(now, first, second),
        tiny_graph,
        replace(constraints(now, horizon=60), cargo_capacity_units=20),
    )

    cuts = build_selection_cuts(prepared)

    assert cuts.pairs == ((1, 2),)


def test_dense_exact_solve_records_and_uses_bound_strengthening(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    contracts = tuple(
        make_contract(now, contract_id, 101, 103, volume=1, collateral=1, reward=100)
        for contract_id in range(1, 21)
    )
    prepared = prepare_problem(
        make_snapshot(now, *contracts),
        tiny_graph,
        constraints(now, horizon=100),
    )

    result = solve_exact(
        prepared,
        tiny_graph,
        config=SolverConfig(
            max_time_seconds=5,
            relaxation_time_seconds=2,
            independent_reference_limit=0,
        ),
    )

    assert result.certificate.status is ProofStatus.PROVEN_OPTIMAL
    assert result.total_reward_units == 2_000
    assert result.certificate.system_relaxation_status == "OPTIMAL"
    assert result.certificate.system_relaxation_bound_units == 2_000
    assert result.certificate.system_relaxation_systems == 2
    assert result.certificate.decomposition_status == "bound_matched"
    assert result.certificate.decomposition_iterations == 1
    assert result.certificate.decomposition_learned_cuts == 0
    assert result.certificate.decomposition_proof_closed
    payload = solve_result_to_dict(result, prepared.problem)
    assert payload["certificate"]["bound_strengthening"]["system_relaxation_bound_units"] == 2_000
    assert payload["certificate"]["bound_strengthening"]["decomposition_proof_closed"] is True
