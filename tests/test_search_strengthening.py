from __future__ import annotations

import random
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from ortools.sat.python import cp_model

import eve_courier_optimizer.solver as solver_module
from eve_courier_optimizer.bounds import (
    build_system_relaxation_master,
    hint_system_relaxation_master,
    solve_system_relaxation,
    solve_system_relaxation_master,
)
from eve_courier_optimizer.construction import insert_additional_contracts
from eve_courier_optimizer.domain import (
    ActiveShipment,
    CollateralMode,
    ProofStatus,
    TravelTimeModel,
)
from eve_courier_optimizer.planning import prepare_problem
from eve_courier_optimizer.reference_solver import solve_reference
from eve_courier_optimizer.sde import UniverseGraph
from eve_courier_optimizer.solver import (
    SolverConfig,
    _build_greedy_route_hint,
    _build_model,
    _hint_verified_route,
    _run_dense_decomposition,
    _solve_reduced_exact_oracle,
    solve_exact,
)
from eve_courier_optimizer.verification import PlannedAction, simulate_and_verify

from .conftest import make_contract, make_snapshot
from .test_bounds import constraints


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
        prepared = prepare_problem(make_snapshot(now, *contracts), tiny_graph, route)
        optimum = solve_reference(prepared).objective_units
        bound = solve_system_relaxation(prepared, max_time_seconds=2)
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
        resolved = prepare_problem(snapshot, tiny_graph, route).problem.contracts[0]
        active = (ActiveShipment(resolved, now + timedelta(seconds=80), picked=case % 2 == 0),)
        prepared = prepare_problem(snapshot, tiny_graph, route, active_shipments=active)
        config = SolverConfig(max_time_seconds=3, minimize_finish_time_after_proof=False)
        with monkeypatch.context() as patch:
            patch.setattr(solver_module, "add_resource_work_bounds", lambda *args: 0)
            baseline = solve_exact(prepared, tiny_graph, config=config)
        strengthened = solve_exact(prepared, tiny_graph, config=config)
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
    prepared = prepare_problem(
        make_snapshot(now, *contracts), tiny_graph, constraints(now, horizon=100)
    )
    relaxed = solve_system_relaxation(prepared, max_time_seconds=2)
    # Two loaded outward jumps and an empty return per batch; service is also paid.
    assert relaxed.upper_bound_units == 400  # Two batches of two indivisible parcels.


def test_asymmetric_metric_keeps_valid_reverse_transport(
    now: datetime, tiny_graph: UniverseGraph
) -> None:
    prepared = prepare_problem(
        make_snapshot(now, *(make_contract(now, i, 103, 101, volume=40) for i in (1, 2))),
        tiny_graph,
        constraints(now, horizon=125),
    )
    # Directed line: outward edges cost five jumps, return edges cost one. Both parcels fit
    # one 124-second loop. A symmetric negative-potential inequality would incorrectly cut it.
    directed = {(s, d): (5 * (d - s) if d >= s else s - d) for s in (1, 2, 3) for d in (1, 2, 3)}
    prepared = replace(prepared, jump_matrix=directed)
    optimum = solve_reference(prepared).objective_units
    bound = solve_system_relaxation(prepared, max_time_seconds=2)
    assert optimum == sum(item.contract.reward_units for item in prepared.problem.contracts)
    assert bound.upper_bound_units == optimum


def test_portfolio_oracle_returns_a_proven_core_without_irrelevant_contracts(
    now: datetime, tiny_graph: UniverseGraph
) -> None:
    prepared = prepare_problem(
        make_snapshot(
            now,
            make_contract(now, 1, 101, 103, volume=60),
            make_contract(now, 2, 101, 103, volume=60),
            make_contract(now, 3, 101, 101, volume=1),
        ),
        tiny_graph,
        constraints(now, horizon=50),
    )
    result = _solve_reduced_exact_oracle(
        prepared, tiny_graph, (1, 2, 3), SolverConfig(num_workers=4), max_time_seconds=2
    )
    assert result.status == cp_model.INFEASIBLE
    assert result.infeasible_core_ids == (1, 2)
    # Independently establish that neither conflicting literal may be removed from the cut.
    for excluded in (1, 2):
        subset = replace(
            prepared,
            problem=replace(
                prepared.problem,
                contracts=tuple(
                    i for i in prepared.problem.contracts if i.contract.contract_id != excluded
                ),
            ),
        )
        assert solve_reference(subset).objective_units == sum(
            i.contract.reward_units for i in subset.problem.contracts
        )


@pytest.mark.parametrize("parcel_limit, expected_reward", [(None, 200), (1, 100)])
def test_insertion_uses_shared_haul_and_respects_capacity(
    now: datetime, tiny_graph: UniverseGraph, parcel_limit: int | None, expected_reward: int
) -> None:
    prepared = prepare_problem(
        make_snapshot(
            now, *(make_contract(now, i, 101, 103, volume=40, reward=100) for i in range(1, 5))
        ),
        tiny_graph,
        replace(constraints(now, horizon=50), max_simultaneous_contracts=parcel_limit),
    )
    visits = _build_greedy_route_hint(prepared)
    ids = tuple(sorted({v.contract_id for v in visits if isinstance(v, PlannedAction)}))
    seed = simulate_and_verify(prepared.problem, tiny_graph, visits, ids)
    assert seed.report.valid and seed.total_reward_units == 100
    _, improved = insert_additional_contracts(prepared, tiny_graph, visits, seed)
    assert improved.report.valid and improved.total_reward_units == expected_reward


def test_complete_hints_are_feasible_with_waypoints_and_resources(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    contracts = (make_contract(now, 1, 101, 102), make_contract(now, 2, 102, 103))
    prepared = prepare_problem(
        make_snapshot(now, *contracts),
        tiny_graph,
        replace(
            constraints(now, mode=CollateralMode.ROLLING),
            required_system_ids=frozenset({2}),
            max_simultaneous_contracts=1,
        ),
    )
    visits = _build_greedy_route_hint(prepared)
    ids = tuple(sorted({v.contract_id for v in visits if isinstance(v, PlannedAction)}))
    simulation = simulate_and_verify(prepared.problem, tiny_graph, visits, ids)
    assert simulation.report.valid
    route = _build_model(prepared)
    _hint_verified_route(route, prepared, visits, ids)
    master = build_system_relaxation_master(prepared)
    hint_system_relaxation_master(
        master,
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
    prepared = prepare_problem(
        make_snapshot(now, *(make_contract(now, i + 1, 101, 103) for i in range(6))),
        tiny_graph,
        replace(constraints(now, horizon=50), cargo_capacity_units=10),
    )
    result = solve_exact(
        prepared,
        tiny_graph,
        config=SolverConfig(max_time_seconds=1e-8, minimize_finish_time_after_proof=False),
    )
    assert result.certificate.feasibility_verified
    assert result.total_reward_units > 0
    assert result.certificate.status is ProofStatus.FEASIBLE_NOT_PROVEN
    assert result.certificate.best_bound_units is not None
    assert result.certificate.best_bound_units > result.total_reward_units


@pytest.mark.parametrize("count", [2, 20])
def test_seed_reward_proof_still_refines_duration(
    now: datetime, tiny_graph: UniverseGraph, count: int
) -> None:
    prepared = prepare_problem(
        make_snapshot(
            now,
            *(make_contract(now, i + 1, 101, 103, volume=40, collateral=100) for i in range(count)),
        ),
        tiny_graph,
        constraints(now, horizon=100, collateral=200),
    )
    result = solve_exact(
        prepared,
        tiny_graph,
        config=SolverConfig(
            max_time_seconds=1e-8,
            decomposition_time_seconds=0,
            secondary_time_seconds=2,
        ),
    )
    # The seed hauls the two jobs sequentially in 84 seconds. Either the total reward sum
    # (small case) or the bound-only master proves it optimal, despite no primary assignment.
    # Requested duration refinement must still combine both parcels into one 44-second loop.
    assert result.certificate.status is ProofStatus.PROVEN_OPTIMAL
    assert result.certificate.feasibility_verified
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
    prepared = prepare_problem(
        make_snapshot(now, *contracts), tiny_graph, constraints(now, horizon=100)
    )
    original = solve_system_relaxation_master

    # Preserve a real feasible master assignment but expose a deliberately unclosed ceiling.
    from eve_courier_optimizer.bounds import SystemRelaxationBound, SystemRelaxationMaster

    calls = 0

    def bounded_master(
        master: SystemRelaxationMaster, *, max_time_seconds: float, random_seed: int = 0
    ) -> SystemRelaxationBound:
        nonlocal calls
        calls += 1
        result = original(master, max_time_seconds=max_time_seconds, random_seed=random_seed)
        assert result.objective_units is not None
        if closes_on_second_solve and calls > 1:
            return result
        return replace(
            result, status_name="FEASIBLE", upper_bound_units=result.objective_units + 100
        )

    monkeypatch.setattr(solver_module, "solve_system_relaxation_master", bounded_master)
    outcome = _run_dense_decomposition(
        prepared, tiny_graph, SolverConfig(minimize_finish_time_after_proof=False)
    )
    assert outcome.status_name == ("bound_matched" if closes_on_second_solve else "incumbent_found")
    assert outcome.iteration_count == 2
    assert outcome.simulation is not None and outcome.simulation.report.valid
    assert not outcome.learned_infeasibility_cores
