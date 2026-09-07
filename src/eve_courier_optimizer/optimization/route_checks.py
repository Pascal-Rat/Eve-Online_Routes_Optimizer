"""Exact feasibility and duration searches for a fixed optional contract selection."""

from __future__ import annotations

import time
from dataclasses import dataclass

from ortools.sat.python import cp_model

from eve_courier_optimizer.routing.preparation import PreparedProblem
from eve_courier_optimizer.routing.replay import SimulationResult, simulate_and_verify
from eve_courier_optimizer.routing.universe import UniverseGraph

from .config import SolverConfig
from .events import EventModel


@dataclass(frozen=True, slots=True)
class SelectionCheck:
    status: cp_model.CpSolverStatus
    status_name: str
    selected_contract_ids: tuple[int, ...]
    simulation: SimulationResult | None
    infeasible_core_ids: tuple[int, ...]
    wall_time_seconds: float
    branches: int
    conflicts: int


def check_selection(
    prepared: PreparedProblem,
    graph: UniverseGraph,
    selected_contract_ids: tuple[int, ...],
    config: SolverConfig,
    *,
    max_time_seconds: float,
) -> SelectionCheck:
    """Test one master selection exactly and return a sufficient infeasibility core when needed."""

    reduced_problem = prepared.restrict_contracts(selected_contract_ids)
    route_model = EventModel(reduced_problem)
    route_model.model.clear_objective()  # type: ignore[no-untyped-call]
    contract_id_by_assumption_index = {
        literal.index: contract_id
        for contract_id, literal in route_model.contract_is_selected.items()
    }
    solve_deadline = time.perf_counter() + max_time_seconds
    total_wall_time_seconds = 0.0
    total_branches = 0
    total_conflicts = 0

    def solve_assuming_selected_contracts(
        assumed_selected_contract_ids: tuple[int, ...],
        time_limit_seconds: float,
    ) -> tuple[cp_model.CpSolver, cp_model.CpSolverStatus]:
        route_model.model.clear_assumptions()
        route_model.model.add_assumptions(
            [
                route_model.contract_is_selected[contract_id]
                for contract_id in assumed_selected_contract_ids
            ]
        )
        validation_error = route_model.model.validate()
        if validation_error:
            raise ValueError(f"invalid reduced exact model: {validation_error}")
        solver = config.solver(seconds=time_limit_seconds, workers=1)
        # Assumptions require single-worker search in OR-Tools 9.15. Use the core from this
        # solve directly, leaving the remaining budget for deletion checks.
        status = solver.solve(route_model.model)
        if status == cp_model.MODEL_INVALID:
            raise ValueError("CP-SAT rejected the validated reduced exact model")
        return solver, status

    selection_solver, status = solve_assuming_selected_contracts(
        selected_contract_ids, max_time_seconds
    )
    total_wall_time_seconds += selection_solver.wall_time
    total_branches += selection_solver.num_branches
    total_conflicts += selection_solver.num_conflicts
    status_name = selection_solver.status_name(status)
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        visits, solved_contract_ids = route_model.extract(selection_solver)
        expected_contract_ids = tuple(sorted(selected_contract_ids))
        if solved_contract_ids != expected_contract_ids:
            raise RuntimeError(
                f"reduced exact oracle did not enforce its selection: {solved_contract_ids}"
            )
        simulation = simulate_and_verify(prepared.problem, graph, visits, solved_contract_ids)
        if not simulation.report.valid:
            raise RuntimeError(
                "reduced exact route failed independent full-problem verification: "
                + "; ".join(simulation.report.violations)
            )
        return SelectionCheck(
            status=status,
            status_name=status_name,
            selected_contract_ids=solved_contract_ids,
            simulation=simulation,
            infeasible_core_ids=(),
            wall_time_seconds=total_wall_time_seconds,
            branches=total_branches,
            conflicts=total_conflicts,
        )
    if status != cp_model.INFEASIBLE:
        return SelectionCheck(
            status=status,
            status_name=status_name,
            selected_contract_ids=(),
            simulation=None,
            infeasible_core_ids=(),
            wall_time_seconds=total_wall_time_seconds,
            branches=total_branches,
            conflicts=total_conflicts,
        )

    assumption_core_indexes = selection_solver.sufficient_assumptions_for_infeasibility()
    unexpected_assumption_indexes = tuple(
        index for index in assumption_core_indexes if index not in contract_id_by_assumption_index
    )
    if unexpected_assumption_indexes:
        raise RuntimeError(
            f"CP-SAT returned unexpected assumption literals: {unexpected_assumption_indexes}"
        )
    infeasible_contract_ids = tuple(
        sorted(contract_id_by_assumption_index[index] for index in assumption_core_indexes)
    )

    # CP-SAT promises a sufficient core, not a minimal one. Deletion checks can make the learned
    # master cut substantially stronger. A literal is removed only after another exact INFEASIBLE
    # result, so an UNKNOWN shrink attempt can never make the proof unsafe.
    for contract_id in tuple(infeasible_contract_ids):
        if contract_id not in infeasible_contract_ids:
            continue
        remaining_time_seconds = solve_deadline - time.perf_counter()
        if remaining_time_seconds <= 0.001:
            break
        trial_contract_ids = tuple(
            candidate_contract_id
            for candidate_contract_id in infeasible_contract_ids
            if candidate_contract_id != contract_id
        )
        shrink_solver, shrink_status = solve_assuming_selected_contracts(
            trial_contract_ids, remaining_time_seconds
        )
        total_wall_time_seconds += shrink_solver.wall_time
        total_branches += shrink_solver.num_branches
        total_conflicts += shrink_solver.num_conflicts
        if shrink_status != cp_model.INFEASIBLE:
            continue
        smaller_core_indexes = shrink_solver.sufficient_assumptions_for_infeasibility()
        unexpected_smaller_core_indexes = tuple(
            index for index in smaller_core_indexes if index not in contract_id_by_assumption_index
        )
        if unexpected_smaller_core_indexes:
            raise RuntimeError(
                "CP-SAT returned unexpected shrink-core literals: "
                f"{unexpected_smaller_core_indexes}"
            )
        infeasible_contract_ids = tuple(
            sorted(contract_id_by_assumption_index[index] for index in smaller_core_indexes)
        )

    return SelectionCheck(
        status=status,
        status_name=status_name,
        selected_contract_ids=(),
        simulation=None,
        infeasible_core_ids=infeasible_contract_ids,
        wall_time_seconds=total_wall_time_seconds,
        branches=total_branches,
        conflicts=total_conflicts,
    )


def refine_selection(
    prepared: PreparedProblem,
    graph: UniverseGraph,
    selected_contract_ids: tuple[int, ...],
    config: SolverConfig,
) -> SelectionCheck:
    """Optionally minimize finish time after contract selection has proved maximum reward."""

    reduced_problem = prepared.restrict_contracts(selected_contract_ids)
    route_model = EventModel(reduced_problem)
    for contract_id in selected_contract_ids:
        route_model.model.add(route_model.contract_is_selected[contract_id] == 1)
    route_model.model.minimize(route_model.finish_time_seconds)
    validation_error = route_model.model.validate()
    if validation_error:
        raise ValueError(f"invalid fixed-selection refinement model: {validation_error}")
    solver = config.solver(seconds=config.secondary_time_seconds)
    status = solver.solve(route_model.model)
    if status == cp_model.MODEL_INVALID:
        raise ValueError("CP-SAT rejected the validated fixed-selection refinement model")
    simulation: SimulationResult | None = None
    solved_contract_ids: tuple[int, ...] = ()
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        visits, solved_contract_ids = route_model.extract(solver)
        expected_contract_ids = tuple(sorted(selected_contract_ids))
        if solved_contract_ids != expected_contract_ids:
            raise RuntimeError(
                f"fixed-selection refinement changed its contract set: {solved_contract_ids}"
            )
        simulation = simulate_and_verify(prepared.problem, graph, visits, solved_contract_ids)
        if not simulation.report.valid:
            raise RuntimeError(
                "fixed-selection refinement failed independent verification: "
                + "; ".join(simulation.report.violations)
            )
    elif status == cp_model.INFEASIBLE:
        raise RuntimeError("fixed-selection refinement contradicted a verified feasible route")
    return SelectionCheck(
        status=status,
        status_name=solver.status_name(status),
        selected_contract_ids=solved_contract_ids,
        simulation=simulation,
        infeasible_core_ids=(),
        wall_time_seconds=solver.wall_time,
        branches=solver.num_branches,
        conflicts=solver.num_conflicts,
    )
