from __future__ import annotations

import random
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from eve_courier_optimizer.domain import (
    ActionKind,
    ActiveShipment,
    CollateralMode,
    PlanningConstraints,
    ProofStatus,
    RoutableContract,
    SecurityPolicy,
    TravelLegKind,
    TravelTimeModel,
)
from eve_courier_optimizer.planning import PreparedProblem, prepare_problem
from eve_courier_optimizer.reference_solver import solve_reference
from eve_courier_optimizer.sde import UniverseGraph
from eve_courier_optimizer.solver import SolverConfig, _run_dense_decomposition, solve_exact
from eve_courier_optimizer.verification import PlannedAction, simulate_and_verify

from .conftest import make_contract, make_snapshot


def constraints(
    now: datetime,
    *,
    cargo: int = 20,
    collateral: int = 200,
    mode: CollateralMode = CollateralMode.LOCKED,
    horizon: int = 1_000,
) -> PlanningConstraints:
    return PlanningConstraints(
        start_system_id=1,
        cargo_capacity_units=cargo,
        collateral_budget_units=collateral,
        horizon_seconds=horizon,
        snapshot_time=now,
        collateral_mode=mode,
        travel=TravelTimeModel(10, 1),
        security=SecurityPolicy(0.45),
    )


def exact(prepared: PreparedProblem, graph: UniverseGraph) -> object:
    return solve_exact(prepared, graph, config=SolverConfig(max_time_seconds=10))


def test_solver_proves_optimal_interleaved_route(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    first = make_contract(now, 1, 101, 102, reward=500)
    second = make_contract(now, 2, 102, 103, reward=900)
    prepared = prepare_problem(make_snapshot(now, first, second), tiny_graph, constraints(now))

    result = solve_exact(prepared, tiny_graph, config=SolverConfig(max_time_seconds=10))

    assert result.certificate.status is ProofStatus.PROVEN_OPTIMAL
    assert result.certificate.best_bound_units == 1_400
    assert result.certificate.objective_units == 1_400
    assert result.certificate.absolute_gap_units == 0
    assert result.certificate.feasibility_verified
    assert result.certificate.independent_reference_verified
    assert result.selected_contract_ids == (1, 2)
    assert [step.contract_id for step in result.route].count(1) == 2
    assert [step.contract_id for step in result.route].count(2) == 2


def test_solver_preserves_exact_objective_above_binary_float_integer_range(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    reward = 2**53 + 1
    public = make_contract(now, 1, 101, 102, reward=reward)
    prepared = prepare_problem(make_snapshot(now, public), tiny_graph, constraints(now))

    result = solve_exact(prepared, tiny_graph, config=SolverConfig(max_time_seconds=10))

    assert result.certificate.status is ProofStatus.PROVEN_OPTIMAL
    assert result.total_reward_units == reward
    assert result.certificate.objective_units == reward
    assert result.certificate.best_bound_units == reward


def test_zero_cargo_can_solve_required_waypoint_loop_as_pure_route(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    route_only = replace(
        constraints(now, cargo=0, horizon=100),
        required_system_ids=frozenset({3}),
    )
    prepared = prepare_problem(make_snapshot(now), tiny_graph, route_only)

    result = solve_exact(prepared, tiny_graph, config=SolverConfig(max_time_seconds=10))

    assert result.certificate.status is ProofStatus.PROVEN_OPTIMAL
    assert result.certificate.feasibility_verified
    assert result.selected_contract_ids == ()
    assert result.route == ()
    assert result.finish_seconds == 40
    assert [
        (leg.kind, leg.from_system_id, leg.to_system_id, leg.jump_path)
        for leg in result.travel_legs
    ] == [
        (TravelLegKind.WAYPOINT, 1, 3, (1, 2, 3)),
        (TravelLegKind.FINISH, 3, 1, (3, 2, 1)),
    ]


def test_open_route_can_require_a_fixed_finish_without_contracts(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    route_only = replace(
        constraints(now, cargo=0, horizon=100),
        return_to_start=False,
        finish_system_id=3,
    )
    prepared = prepare_problem(make_snapshot(now), tiny_graph, route_only)

    result = solve_exact(prepared, tiny_graph, config=SolverConfig(max_time_seconds=10))

    assert result.certificate.status is ProofStatus.PROVEN_OPTIMAL
    assert result.finish_seconds == 20
    assert len(result.travel_legs) == 1
    assert result.travel_legs[0].kind is TravelLegKind.FINISH
    assert result.travel_legs[0].jump_path == (1, 2, 3)


def test_simultaneous_contract_limit_changes_the_proven_optimum(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    long = make_contract(now, 1, 101, 103, volume=1, reward=600)
    short = make_contract(now, 2, 101, 102, volume=1, reward=500)
    snapshot = make_snapshot(now, long, short)
    base = constraints(now, cargo=20, collateral=200, horizon=50)

    unrestricted = solve_exact(
        prepare_problem(snapshot, tiny_graph, base),
        tiny_graph,
        config=SolverConfig(max_time_seconds=10),
    )
    limited = solve_exact(
        prepare_problem(snapshot, tiny_graph, replace(base, max_simultaneous_contracts=1)),
        tiny_graph,
        config=SolverConfig(max_time_seconds=10),
    )

    assert unrestricted.certificate.status is ProofStatus.PROVEN_OPTIMAL
    assert unrestricted.total_reward_units == 1_100
    assert limited.certificate.status is ProofStatus.PROVEN_OPTIMAL
    assert limited.certificate.independent_reference_verified
    assert limited.total_reward_units == 600
    in_trunk: set[int] = set()
    for step in limited.route:
        if step.action is ActionKind.PICKUP:
            in_trunk.add(step.contract_id)
        else:
            in_trunk.remove(step.contract_id)
        assert len(in_trunk) <= 1


def test_locked_vs_rolling_collateral_reuse(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    first = make_contract(now, 1, 101, 102, collateral=100, reward=500)
    second = make_contract(now, 2, 102, 103, collateral=100, reward=700)
    snapshot = make_snapshot(now, first, second)

    locked = prepare_problem(snapshot, tiny_graph, constraints(now, collateral=100))
    locked_result = solve_exact(locked, tiny_graph, config=SolverConfig(max_time_seconds=10))
    assert locked_result.total_reward_units == 700

    rolling = prepare_problem(
        snapshot,
        tiny_graph,
        constraints(now, collateral=100, mode=CollateralMode.ROLLING),
    )
    rolling_result = solve_exact(rolling, tiny_graph, config=SolverConfig(max_time_seconds=10))
    assert rolling_result.total_reward_units == 1_200
    assert [step.action.value for step in rolling_result.route] == [
        "pickup",
        "delivery",
        "pickup",
        "delivery",
    ]


def test_cargo_capacity_forces_delivery_before_next_pickup(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    first = make_contract(now, 1, 101, 102, volume=15, reward=500)
    second = make_contract(now, 2, 102, 103, volume=15, reward=600)
    prepared = prepare_problem(
        make_snapshot(now, first, second),
        tiny_graph,
        constraints(now, cargo=20),
    )
    result = solve_exact(prepared, tiny_graph, config=SolverConfig(max_time_seconds=10))
    action_pairs = [(step.action.value, step.contract_id) for step in result.route]
    assert action_pairs == [
        ("pickup", 1),
        ("delivery", 1),
        ("pickup", 2),
        ("delivery", 2),
    ]


def test_decomposition_learns_higher_order_cargo_infeasibility(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    contracts = tuple(
        make_contract(
            now,
            contract_id,
            102,
            103,
            volume=40,
            collateral=1,
            reward=1_000 + contract_id,
        )
        for contract_id in range(1, 21)
    )
    prepared = prepare_problem(
        make_snapshot(now, *contracts),
        tiny_graph,
        constraints(now, cargo=100, collateral=200, horizon=50),
    )

    outcome = _run_dense_decomposition(
        prepared,
        tiny_graph,
        SolverConfig(
            max_time_seconds=5,
            num_workers=1,
            minimize_finish_time_after_proof=False,
            relaxation_time_seconds=1,
            decomposition_time_seconds=3,
            decomposition_subproblem_time_seconds=1,
            decomposition_max_iterations=1,
        ),
    )

    assert outcome.simulation is None
    assert not outcome.proven_infeasible
    assert outcome.status_name == "iteration_limit"
    assert outcome.iteration_count == 1
    assert outcome.selection_cuts.pairs == ()
    assert len(outcome.learned_infeasibility_cores) == 1
    assert len(outcome.learned_infeasibility_cores[0]) == 3


def test_active_picked_shipment_can_make_model_infeasible(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    public = make_contract(now, 9, 101, 103, reward=1_000)
    routable = RoutableContract(public, 1, 3)
    active = ActiveShipment(routable, deadline=now + timedelta(seconds=5), picked=True)
    snapshot = make_snapshot(now)
    prepared = prepare_problem(
        snapshot,
        tiny_graph,
        constraints(now),
        active_shipments=(active,),
    )
    result = solve_exact(prepared, tiny_graph, config=SolverConfig(max_time_seconds=10))
    assert result.certificate.status is ProofStatus.PROVEN_INFEASIBLE
    assert not result.route


def test_committed_unpicked_contract_is_mandatory_and_ordered(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    public = make_contract(now, 9, 102, 103, reward=1_000)
    routable = RoutableContract(public, 2, 3)
    active = ActiveShipment(routable, deadline=now + timedelta(hours=1), picked=False)
    prepared = prepare_problem(
        make_snapshot(now),
        tiny_graph,
        constraints(now),
        active_shipments=(active,),
    )
    result = solve_exact(prepared, tiny_graph, config=SolverConfig(max_time_seconds=10))
    assert result.certificate.status is ProofStatus.PROVEN_OPTIMAL
    assert [(step.action.value, step.contract_id) for step in result.route] == [
        ("pickup", 9),
        ("delivery", 9),
    ]
    assert result.total_reward_units == 1_000


def test_heuristic_candidate_cap_is_visible_in_certificate(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    snapshot = make_snapshot(
        now,
        make_contract(now, 1, 101, 102, reward=500),
        make_contract(now, 2, 101, 102, reward=600),
    )
    prepared = prepare_problem(snapshot, tiny_graph, constraints(now), max_candidates=1)
    result = solve_exact(prepared, tiny_graph, config=SolverConfig(max_time_seconds=10))
    assert result.certificate.status is ProofStatus.PROVEN_OPTIMAL
    assert not result.certificate.scope_untruncated
    assert "truncated" in result.certificate.claim


def test_independent_simulator_rejects_delivery_before_pickup(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    public = make_contract(now, 1, 101, 102)
    prepared = prepare_problem(make_snapshot(now, public), tiny_graph, constraints(now))
    simulation = simulate_and_verify(
        prepared.problem,
        tiny_graph,
        (PlannedAction(action=ActionKind.DELIVERY, contract_id=1),),
        (1,),
    )
    assert not simulation.report.valid
    assert any("before pickup" in violation for violation in simulation.report.violations)


def test_cp_sat_matches_reference_on_random_small_instances(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    rng = random.Random(7)
    station_ids = [101, 102, 103]
    for case in range(8):
        contracts = []
        for index in range(5):
            origin_index = rng.randrange(0, 2)
            destination_index = rng.randrange(origin_index + 1, 3)
            contracts.append(
                make_contract(
                    now,
                    case * 10 + index + 1,
                    station_ids[origin_index],
                    station_ids[destination_index],
                    volume=rng.randint(3, 12),
                    collateral=rng.randint(20, 80),
                    reward=rng.randint(100, 1_000),
                )
            )
        prepared = prepare_problem(
            make_snapshot(now, *contracts),
            tiny_graph,
            constraints(now, cargo=20, collateral=160, horizon=100),
        )
        reference = solve_reference(prepared, contract_limit=10)
        result = solve_exact(
            prepared,
            tiny_graph,
            config=SolverConfig(max_time_seconds=10, independent_reference_limit=0),
        )
        assert result.certificate.status is ProofStatus.PROVEN_OPTIMAL
        assert result.total_reward_units == reference.objective_units


def test_reference_solver_rejects_unsupported_modes(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    prepared = prepare_problem(
        make_snapshot(now, make_contract(now, 1, 101, 102)),
        tiny_graph,
        constraints(now, mode=CollateralMode.ROLLING),
    )
    with pytest.raises(ValueError, match="locked collateral"):
        solve_reference(prepared)
