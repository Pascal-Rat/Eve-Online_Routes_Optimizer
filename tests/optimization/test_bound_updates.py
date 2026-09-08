"""Reward ceilings remain useful even before CP-SAT finds its first solution."""

from __future__ import annotations

import math
import random
from datetime import datetime
from itertools import product
from unittest.mock import Mock

import pytest
from ortools.sat.python import cp_model

# The response solution sequence exists at runtime but is untyped in OR-Tools 9.15.
from eve_courier_optimizer.domain import ProofStatus
from eve_courier_optimizer.optimization import RouteOptimizer, SolverConfig
from eve_courier_optimizer.optimization.models.selection_bounds import RewardBoundRecorder
from eve_courier_optimizer.optimization.models.system_tour import SystemTourModel
from eve_courier_optimizer.optimization.search.haul_batches import solve_batches
from eve_courier_optimizer.routing.route_problem import RouteProblem
from eve_courier_optimizer.routing.universe import UniverseGraph
from tests.conftest import make_contract, make_snapshot
from tests.optimization.test_reward_bounds import constraints


@pytest.fixture
def bound_problem(now: datetime, tiny_graph: UniverseGraph) -> RouteProblem:
    # At most one parcel can be accepted: 60 + 60 > the collateral budget of 100.
    return RouteProblem.from_snapshot(
        make_snapshot(
            now,
            *(make_contract(now, i, 101, 103, collateral=60, reward=100) for i in range(1, 4)),
        ),
        tiny_graph,
        constraints(now, collateral=100),
    )


def interrupt_before_solution(monkeypatch: pytest.MonkeyPatch, *, presolve: bool) -> None:
    solve = cp_model.CpSolver.solve

    def interrupted(solver: cp_model.CpSolver, model: cp_model.CpModel) -> cp_model.CpSolverStatus:
        # Deterministic cancellation points, independent of CPU speed. No incumbent hint
        # may be accepted by this solve; the optimizer retains its independently verified one.
        model.proto.clear_solution_hint()
        solver.parameters.cp_model_presolve = presolve
        solver.parameters.stop_after_presolve = presolve
        solver.parameters.stop_after_root_propagation = not presolve
        status = solve(solver, model)
        assert status == cp_model.UNKNOWN
        assert not solver.response_proto.solution  # pyright: ignore[reportUnknownMemberType]
        if presolve:
            assert solver.best_objective_bound == 0
        return status

    monkeypatch.setattr(cp_model.CpSolver, "solve", interrupted)


@pytest.mark.parametrize("presolve", [False, True])
def test_unknown_master_keeps_only_explicit_bounds(
    bound_problem: RouteProblem, monkeypatch: pytest.MonkeyPatch, presolve: bool
) -> None:
    interrupt_before_solution(monkeypatch, presolve=presolve)
    result = SystemTourModel(bound_problem).solve(max_time_seconds=2)
    assert result.status_name == "UNKNOWN"
    assert result.objective_units is None
    assert result.selected_contract_ids == ()
    assert result.upper_bound_units == (None if presolve else 200)


@pytest.mark.parametrize("presolve", [False, True])
def test_unknown_route_keeps_bound_and_independently_verified_incumbent(
    bound_problem: RouteProblem,
    tiny_graph: UniverseGraph,
    monkeypatch: pytest.MonkeyPatch,
    presolve: bool,
) -> None:
    interrupt_before_solution(monkeypatch, presolve=presolve)
    result = RouteOptimizer(
        bound_problem,
        tiny_graph,
        config=SolverConfig(
            max_time_seconds=5,
            independent_reference_limit=0,
            minimize_finish_time_after_proof=False,
        ),
    ).solve()
    assert result.total_reward_units == 100
    assert result.certificate.solver_status == "UNKNOWN_WITH_INCUMBENT"
    assert result.certificate.best_bound_units == (300 if presolve else 200)
    assert result.certificate.feasibility_verified
    assert result.certificate.status is ProofStatus.FEASIBLE_NOT_PROVEN


@pytest.mark.parametrize("presolve", [False, True])
def test_unknown_batch_keeps_bound_without_inventing_a_route(
    bound_problem: RouteProblem, monkeypatch: pytest.MonkeyPatch, presolve: bool
) -> None:
    interrupt_before_solution(monkeypatch, presolve=presolve)
    result = solve_batches(bound_problem, max_time_seconds=2)
    assert result is not None
    assert result.objective_units is None
    assert result.visits == ()
    assert result.selected_contract_ids == ()
    assert not result.complete
    assert result.upper_bound_units == (None if presolve else 100)


@pytest.mark.parametrize("stage", ["route", "master", "batch"])
def test_bound_event_can_close_proof_without_a_solver_solution(
    now: datetime,
    tiny_graph: UniverseGraph,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
) -> None:
    problem = RouteProblem.from_snapshot(
        make_snapshot(
            now,
            *(
                make_contract(now, i, 101, 103, collateral=60, reward=100)
                for i in range(1, 4 if stage == "route" else 21)
            ),
        ),
        tiny_graph,
        constraints(now, collateral=100),
    )
    solve = cp_model.CpSolver.solve
    reward_solves = 0

    def interrupted(solver: cp_model.CpSolver, model: cp_model.CpModel) -> cp_model.CpSolverStatus:
        nonlocal reward_solves
        recorder = solver.best_bound_callback
        if not isinstance(recorder, RewardBoundRecorder):
            # Refinement is a separate minimization; its seconds must not become a reward bound.
            return solve(solver, model)
        reward_solves += 1
        model.proto.clear_solution_hint()

        def stop_at_bound(bound: float) -> None:
            recorder(bound)
            solver.stop_search()

        solver.best_bound_callback = stop_at_bound
        status = solve(solver, model)
        assert status == cp_model.UNKNOWN
        assert not solver.response_proto.solution  # pyright: ignore[reportUnknownMemberType]
        return status

    monkeypatch.setattr(cp_model.CpSolver, "solve", interrupted)
    if stage == "master":
        monkeypatch.setattr(
            "eve_courier_optimizer.optimization.search.haul_batches.solve_batches",
            Mock(return_value=None),
        )
    result = RouteOptimizer(
        problem,
        tiny_graph,
        config=SolverConfig(max_time_seconds=5, independent_reference_limit=0),
    ).solve()
    assert reward_solves == 1
    assert result.total_reward_units == result.certificate.best_bound_units == 100
    assert result.certificate.status is ProofStatus.PROVEN_OPTIMAL
    assert result.certificate.feasibility_verified
    if stage == "batch":
        assert result.certificate.system_relaxation_status == "BATCH_UNKNOWN"
    elif stage == "master":
        assert result.certificate.system_relaxation_status == "UNKNOWN"
    else:
        assert result.certificate.solver_status == "UNKNOWN_WITH_INCUMBENT"


def test_bound_recorder_handles_zero_nonfinite_and_float_integer_boundary() -> None:
    recorder = RewardBoundRecorder()
    for value in (math.nan, math.inf, -math.inf):
        recorder(value)
    initial_bound = recorder.upper_bound_units
    assert initial_bound is None
    exact = 2**53 + 1
    recorder(float(exact))
    assert recorder.upper_bound_units is not None
    assert recorder.upper_bound_units >= exact
    recorder(117.25)
    recorder(200)
    assert recorder.upper_bound_units == 118
    recorder(0)
    assert recorder.upper_bound_units == 0
    assert RewardBoundRecorder().upper_bound_units is None


@pytest.mark.parametrize("workers", [1, 4])
def test_interrupted_bound_events_dominate_enumerated_optima(workers: int) -> None:
    rng = random.Random(5187)
    for _ in range(12):
        weights = [rng.randrange(1, 20) for _ in range(8)]
        rewards = [rng.randrange(1, 100) for _ in weights]
        capacity = sum(weights) // 3
        offset = 17
        optimum = max(
            offset + sum(r * x for r, x in zip(rewards, bits, strict=True))
            for bits in product((0, 1), repeat=len(weights))
            if sum(w * x for w, x in zip(weights, bits, strict=True)) <= capacity
        )
        model = cp_model.CpModel()
        selected = [model.new_bool_var(f"job_{i}") for i in range(len(weights))]
        model.add(sum(w * x for w, x in zip(weights, selected, strict=True)) <= capacity)
        model.maximize(offset + sum(r * x for r, x in zip(rewards, selected, strict=True)))
        solver = cp_model.CpSolver()
        solver.parameters.num_search_workers = workers
        recorder = RewardBoundRecorder()

        def stop_at_bound(
            bound: float,
            recorder: RewardBoundRecorder = recorder,
            solver: cp_model.CpSolver = solver,
        ) -> None:
            recorder(bound)
            solver.stop_search()

        solver.best_bound_callback = stop_at_bound
        assert solver.solve(model) == cp_model.UNKNOWN
        assert not solver.response_proto.solution  # pyright: ignore[reportUnknownMemberType]
        assert recorder.upper_bound_units is not None
        assert recorder.upper_bound_units >= optimum
