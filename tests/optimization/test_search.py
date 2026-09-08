from __future__ import annotations

import random
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from ortools.sat.python import cp_model

from eve_courier_optimizer.domain import (
    ActiveShipment,
    CollateralMode,
    PlannedAction,
    PlannedWaypoint,
    ProofStatus,
    TravelTimeModel,
)
from eve_courier_optimizer.optimization import RouteOptimizer, SolverConfig
from eve_courier_optimizer.optimization.models.pickup_delivery import PickupDeliveryModel
from eve_courier_optimizer.optimization.models.system_tour import SystemTourModel
from eve_courier_optimizer.optimization.search import haul_batches
from eve_courier_optimizer.optimization.search.contract_selection import ContractSelectionSearch
from eve_courier_optimizer.optimization.search.fixed_contract_route import check_selection
from eve_courier_optimizer.optimization.search.route_insertion import (
    build_greedy_route_hint,
    improve_incumbent,
    insert_additional_contracts,
)
from eve_courier_optimizer.routing.route_problem import RouteProblem
from eve_courier_optimizer.routing.universe import UniverseGraph
from eve_courier_optimizer.verification.exhaustive_optimum import solve_exhaustively
from eve_courier_optimizer.verification.route_replay import simulate_and_verify
from tests.conftest import make_contract, make_snapshot
from tests.optimization.test_reward_bounds import constraints


def test_transport_bounds_preserve_random_exact_optima(
    now: datetime, tiny_graph: UniverseGraph
) -> None:
    rng = random.Random(291)
    for case in range(40):
        contracts = tuple(
            make_contract(
                now,
                i + 1,
                rng.choice((101, 102, 103)),
                rng.choice((101, 102, 103)),
                volume=rng.randint(10, 60),
                collateral=rng.randint(10, 90),
                reward=rng.randint(1, 1000),
            )
            for i in range(5)
        )
        route = replace(
            constraints(now, horizon=rng.randint(40, 140)),
            cargo_capacity_units=rng.randint(20, 100),
            collateral_budget_units=rng.randint(100, 400),
            travel=TravelTimeModel(10, rng.choice((0, 1, 2))),
            return_to_start=case % 3 == 0,
            finish_system_id=3 if case % 3 == 1 else None,
            max_simultaneous_contracts=rng.choice((None, 1, 2)),
        )
        problem = RouteProblem.from_snapshot(make_snapshot(now, *contracts), tiny_graph, route)
        optimum = solve_exhaustively(problem).objective_units
        bound = SystemTourModel(problem).solve(max_time_seconds=2)
        assert optimum is not None
        assert bound.upper_bound_units is not None and bound.upper_bound_units >= optimum


def test_resource_work_preserves_rolling_and_active_shipments(
    now: datetime,
    tiny_graph: UniverseGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rng = random.Random(55)
    for case in range(12):
        contracts = tuple(
            make_contract(
                now,
                i + 1,
                rng.choice((101, 102, 103)),
                rng.choice((101, 102, 103)),
                volume=rng.randint(5, 30),
                collateral=rng.randint(10, 80),
                reward=rng.randint(10, 100),
            )
            for i in range(5)
        )
        route = replace(
            constraints(now, horizon=90, mode=CollateralMode.ROLLING),
            cargo_capacity_units=40,
            collateral_budget_units=100,
            return_to_start=case % 2 == 0,
            finish_system_id=3 if case % 2 else None,
            required_system_ids=frozenset({2}),
            max_simultaneous_contracts=2,
        )
        snapshot = make_snapshot(now, *contracts)
        resolved = RouteProblem.from_snapshot(snapshot, tiny_graph, route).contracts[0]
        active = (ActiveShipment(resolved, now + timedelta(seconds=80), picked=case % 2 == 0),)
        problem = RouteProblem.from_snapshot(snapshot, tiny_graph, route, active_shipments=active)
        config = SolverConfig(max_time_seconds=3, minimize_finish_time_after_proof=False)
        with monkeypatch.context() as patch:
            patch.setattr(
                "eve_courier_optimizer.optimization.models.pickup_delivery.add_resource_work_bounds",
                lambda *args: 0,
            )
            baseline = RouteOptimizer(problem, tiny_graph, config=config).solve()
        strengthened = RouteOptimizer(problem, tiny_graph, config=config).solve()
        assert strengthened.certificate.status == baseline.certificate.status
        assert strengthened.total_reward_units == baseline.total_reward_units
        assert strengthened.certificate.status in {
            ProofStatus.PROVEN_OPTIMAL,
            ProofStatus.PROVEN_INFEASIBLE,
        }


def test_capacity_work_tightens_loop_bound(now: datetime, tiny_graph: UniverseGraph) -> None:
    contracts = tuple(
        make_contract(now, i + 1, 101, 103, volume=40, collateral=1, reward=100) for i in range(20)
    )
    problem = RouteProblem.from_snapshot(
        make_snapshot(now, *contracts), tiny_graph, constraints(now, horizon=100)
    )
    relaxed = SystemTourModel(problem).solve(max_time_seconds=2)
    # Two loaded outward jumps and an empty return per batch; service is also paid.
    assert relaxed.upper_bound_units == 400  # Two batches of two indivisible parcels.


def test_asymmetric_metric_keeps_valid_reverse_transport(
    now: datetime, tiny_graph: UniverseGraph
) -> None:
    problem = RouteProblem.from_snapshot(
        make_snapshot(now, *(make_contract(now, i, 103, 101, volume=40) for i in (1, 2))),
        tiny_graph,
        constraints(now, horizon=125),
    )
    # Directed line: outward edges cost five jumps, return edges cost one. Both parcels fit
    # one 124-second loop. A symmetric negative-potential inequality would incorrectly cut it.
    directed = {(s, d): (5 * (d - s) if d >= s else s - d) for s in (1, 2, 3) for d in (1, 2, 3)}
    problem = replace(problem, jump_matrix=directed)
    optimum = solve_exhaustively(problem).objective_units
    bound = SystemTourModel(problem).solve(max_time_seconds=2)
    assert optimum == sum(item.reward_units for item in problem.contracts)
    assert bound.upper_bound_units == optimum


def test_portfolio_oracle_returns_a_proven_core_without_irrelevant_contracts(
    now: datetime, tiny_graph: UniverseGraph
) -> None:
    problem = RouteProblem.from_snapshot(
        make_snapshot(
            now,
            make_contract(now, 1, 101, 103, volume=60),
            make_contract(now, 2, 101, 103, volume=60),
            make_contract(now, 3, 101, 101, volume=1),
        ),
        tiny_graph,
        constraints(now, horizon=50),
    )
    result = check_selection(
        problem, tiny_graph, (1, 2, 3), SolverConfig(num_workers=4), max_time_seconds=2
    )
    assert result.status == cp_model.INFEASIBLE
    assert result.infeasible_core_ids == (1, 2)
    # Independently establish that neither conflicting literal may be removed from the cut.
    for excluded in (1, 2):
        subset = problem.restrict_contracts(
            tuple(
                contract.contract_id
                for contract in problem.contracts
                if contract.contract_id != excluded
            )
        )
        assert solve_exhaustively(subset).objective_units == sum(
            i.reward_units for i in subset.contracts
        )


@pytest.mark.parametrize("parcel_limit, expected_reward", [(None, 200), (1, 100)])
def test_insertion_uses_shared_haul_and_respects_capacity(
    now: datetime, tiny_graph: UniverseGraph, parcel_limit: int | None, expected_reward: int
) -> None:
    problem = RouteProblem.from_snapshot(
        make_snapshot(
            now, *(make_contract(now, i, 101, 103, volume=40, reward=100) for i in range(1, 5))
        ),
        tiny_graph,
        replace(constraints(now, horizon=50), max_simultaneous_contracts=parcel_limit),
    )
    visits = build_greedy_route_hint(problem)
    ids = tuple(sorted({v.contract_id for v in visits if isinstance(v, PlannedAction)}))
    seed = simulate_and_verify(problem, tiny_graph, visits, ids)
    assert seed.report.valid and seed.total_reward_units == 100
    _, improved = insert_additional_contracts(problem, tiny_graph, visits, seed)
    assert improved.report.valid and improved.total_reward_units == expected_reward


def test_complete_hints_are_feasible_with_waypoints_and_resources(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    contracts = (make_contract(now, 1, 101, 102), make_contract(now, 2, 102, 103))
    problem = RouteProblem.from_snapshot(
        make_snapshot(now, *contracts),
        tiny_graph,
        replace(
            constraints(now, mode=CollateralMode.ROLLING),
            required_system_ids=frozenset({2}),
            max_simultaneous_contracts=1,
        ),
    )
    visits = build_greedy_route_hint(problem)
    ids = tuple(sorted({v.contract_id for v in visits if isinstance(v, PlannedAction)}))
    simulation = simulate_and_verify(problem, tiny_graph, visits, ids)
    assert simulation.report.valid
    route = PickupDeliveryModel(problem, selection_hint=build_greedy_route_hint(problem))
    route.hint(visits, ids)
    master = SystemTourModel(problem)
    master.hint(
        ids,
        tuple(leg.to_system_id for leg in simulation.travel_legs),
        simulation.total_reward_units,
    )
    for model in (route.model, master.model):
        assert len(model.proto.solution_hint.vars) == len(model.proto.variables)
        solver = cp_model.CpSolver()
        solver.parameters.fix_variables_to_their_hinted_value = True
        assert solver.solve(model) == cp_model.OPTIMAL


def test_tiny_budget_preserves_verified_incumbent(now: datetime, tiny_graph: UniverseGraph) -> None:
    problem = RouteProblem.from_snapshot(
        make_snapshot(now, *(make_contract(now, i + 1, 101, 103) for i in range(6))),
        tiny_graph,
        replace(constraints(now, horizon=50), cargo_capacity_units=10),
    )
    result = RouteOptimizer(
        problem,
        tiny_graph,
        config=SolverConfig(max_time_seconds=1e-8, minimize_finish_time_after_proof=False),
    ).solve()
    assert result.certificate.feasibility_verified
    assert result.total_reward_units > 0
    assert result.certificate.status is ProofStatus.FEASIBLE_NOT_PROVEN
    assert result.certificate.best_bound_units is not None
    assert result.certificate.best_bound_units > result.total_reward_units


@pytest.mark.parametrize("count", [2, 20])
def test_seed_reward_proof_still_refines_duration(
    now: datetime, tiny_graph: UniverseGraph, count: int
) -> None:
    problem = RouteProblem.from_snapshot(
        make_snapshot(
            now,
            *(make_contract(now, i + 1, 101, 103, volume=40, collateral=100) for i in range(count)),
        ),
        tiny_graph,
        constraints(now, horizon=100, collateral=200),
    )
    result = RouteOptimizer(
        problem,
        tiny_graph,
        config=SolverConfig(
            max_time_seconds=5,
            decomposition_time_seconds=0,
            secondary_time_seconds=2,
        ),
    ).solve()
    # The trivial ceiling (small case) or bound-only master proves maximum reward.
    # Refinement shares the remaining overall allowance and keeps the 44-second loop.
    assert result.certificate.status is ProofStatus.PROVEN_OPTIMAL
    assert result.certificate.feasibility_verified
    assert result.finish_seconds == 44


def test_duration_search_cannot_replace_a_faster_verified_incumbent(
    now: datetime, tiny_graph: UniverseGraph, monkeypatch: pytest.MonkeyPatch
) -> None:
    from eve_courier_optimizer.domain import ActionKind
    from eve_courier_optimizer.optimization.search.fixed_contract_route import SelectionCheck
    from eve_courier_optimizer.optimization.search.route_insertion import construct_incumbent

    problem = RouteProblem.from_snapshot(
        make_snapshot(
            now,
            *(make_contract(now, i + 1, 101, 103, volume=40, collateral=100) for i in range(20)),
        ),
        tiny_graph,
        constraints(now, horizon=100, collateral=200),
    )
    incumbent = construct_incumbent(problem, tiny_graph)
    assert incumbent is not None and incumbent.simulation.finish_seconds == 44
    sequential = tuple(
        PlannedAction(action, cid)
        for cid in incumbent.selected_contract_ids
        for action in (ActionKind.PICKUP, ActionKind.DELIVERY)
    )
    slower = simulate_and_verify(problem, tiny_graph, sequential, incumbent.selected_contract_ids)
    assert slower.report.valid and slower.finish_seconds == 84
    refinement = SelectionCheck(
        cp_model.FEASIBLE, "FEASIBLE", incumbent.selected_contract_ids, slower, (), 0.0, 0, 0
    )
    monkeypatch.setattr(
        "eve_courier_optimizer.optimization.search.fixed_contract_route.refine_selection",
        lambda *args, **kwargs: refinement,
    )
    result = RouteOptimizer(
        problem,
        tiny_graph,
        config=SolverConfig(max_time_seconds=5, decomposition_time_seconds=0),
    ).solve()
    assert result.certificate.status is ProofStatus.PROVEN_OPTIMAL
    assert result.total_reward_units == incumbent.simulation.total_reward_units
    assert result.finish_seconds == 44


@pytest.mark.parametrize("closes_on_second_solve", [False, True])
def test_feasible_master_selection_improves_master_without_false_proof(
    now: datetime,
    tiny_graph: UniverseGraph,
    monkeypatch: pytest.MonkeyPatch,
    closes_on_second_solve: bool,
) -> None:
    contracts = tuple(
        make_contract(now, i + 1, 101, 103, volume=1, collateral=1, reward=100) for i in range(20)
    )
    problem = RouteProblem.from_snapshot(
        make_snapshot(now, *contracts), tiny_graph, constraints(now, horizon=100)
    )
    # Preserve a real feasible master assignment but expose a deliberately unclosed ceiling.
    from eve_courier_optimizer.optimization.models.system_tour import (
        SystemRewardBound,
        SystemTourModel,
    )

    original = SystemTourModel.solve

    calls = 0

    def bounded_master(
        master: SystemTourModel, *, max_time_seconds: float, random_seed: int = 0
    ) -> SystemRewardBound:
        nonlocal calls
        calls += 1
        result = original(master, max_time_seconds=max_time_seconds, random_seed=random_seed)
        assert result.objective_units is not None
        if closes_on_second_solve and calls > 1:
            return result
        return replace(
            result, status_name="FEASIBLE", upper_bound_units=result.objective_units + 100
        )

    monkeypatch.setattr(SystemTourModel, "solve", bounded_master)
    monkeypatch.setattr(haul_batches, "solve_batches", lambda *args, **kwargs: None)
    outcome = ContractSelectionSearch(
        problem, tiny_graph, SolverConfig(minimize_finish_time_after_proof=False)
    ).run()
    assert outcome.status_name == ("bound_matched" if closes_on_second_solve else "incumbent_found")
    assert outcome.iteration_count == 2
    assert outcome.incumbent is not None and outcome.incumbent.simulation.report.valid
    assert not outcome.learned_infeasibility_cores


def test_reconstruction_replaces_blocking_choice_and_preserves_waypoints(
    now: datetime, tiny_graph: UniverseGraph
) -> None:
    problem = RouteProblem.from_snapshot(
        make_snapshot(
            now,
            make_contract(now, 1, 101, 101, collateral=100, reward=600),
            *(
                make_contract(now, i, 101, 103, volume=40, collateral=50, reward=500)
                for i in (2, 3)
            ),
        ),
        tiny_graph,
        replace(constraints(now, horizon=80, collateral=100), required_system_ids=frozenset({2})),
    )
    visits = build_greedy_route_hint(problem)
    ids = tuple(sorted({v.contract_id for v in visits if isinstance(v, PlannedAction)}))
    seed = simulate_and_verify(problem, tiny_graph, visits, ids)
    _, old = insert_additional_contracts(problem, tiny_graph, visits, seed)
    assert old.total_reward_units == 600
    result, improved = improve_incumbent(
        problem,
        tiny_graph,
        visits,
        seed,
        restart_visits=(PlannedWaypoint(2),),
    )
    assert improved.report.valid
    assert improved.total_reward_units == 1000
    assert {v.contract_id for v in result if isinstance(v, PlannedAction)} == {2, 3}
    # A bad restart must never erase an already verified incumbent.
    _, retained = improve_incumbent(problem, tiny_graph, visits, seed, restart_visits=())
    assert retained == old
