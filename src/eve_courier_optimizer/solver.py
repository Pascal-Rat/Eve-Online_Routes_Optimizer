"""Optimize reward, retain verified incumbents, and assemble one auditable proof result."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from importlib.metadata import version as package_version

from ortools.sat.python import cp_model

from .bounds import add_selection_cuts, add_subset_reward_cut, integer_upper_bound
from .construction import construct_incumbent, diversify_incumbent
from .decomposition import DecompositionResult, prove_with_decomposition, refine_selection
from .domain import CollateralMode, OptimalityCertificate, ProofStatus, SolveResult
from .event_model import EventModel
from .planning import PreparedProblem
from .proof import canonical_problem_sha256, optimality_claim
from .reference_solver import solve_reference
from .sde import UniverseGraph
from .search_config import INCUMBENT_DIVERSIFICATION_SECONDS, SolverConfig
from .verification import VerifiedRoute


@dataclass(slots=True)
class _SearchResult:
    incumbent: VerifiedRoute | None
    upper_bound_units: int | None
    solver_status: str
    proven_infeasible: bool = False
    reference_verified: bool = False
    wall_time_seconds: float = 0.0
    branches: int = 0
    conflicts: int = 0

    def record(self, solver: cp_model.CpSolver) -> None:
        self.wall_time_seconds += solver.wall_time
        self.branches += solver.num_branches
        self.conflicts += solver.num_conflicts

    def consider(self, candidate: VerifiedRoute) -> None:
        if self.incumbent is None or candidate.quality > self.incumbent.quality:
            self.incumbent = candidate

    @property
    def reward_proven(self) -> bool:
        return (
            self.incumbent is not None
            and self.upper_bound_units == self.incumbent.simulation.total_reward_units
        )


def _search_complete_model(
    prepared: PreparedProblem,
    graph: UniverseGraph,
    config: SolverConfig,
    decomposition: DecompositionResult,
    incumbent: VerifiedRoute | None,
    progress: Callable[[str], None] | None,
) -> _SearchResult:
    route = EventModel(prepared)
    bound = decomposition.upper_bound_units
    if bound is not None:
        route.model.add(route.total_reward_units <= bound)
    add_selection_cuts(route.model, route.contract_is_selected, decomposition.selection_cuts)
    for core in decomposition.learned_infeasibility_cores:
        route.model.add(sum(route.contract_is_selected[cid] for cid in core) <= len(core) - 1)
    for cut in decomposition.learned_reward_cuts:
        add_subset_reward_cut(route.model, route.contract_is_selected, cut)
    if incumbent is not None:
        route.hint(incumbent.simulation.visits, incumbent.selected_contract_ids)
    validation_error = route.model.validate()
    if validation_error:
        raise ValueError(f"invalid or numerically unsafe CP-SAT model: {validation_error}")

    solver = config.solver()
    status = solver.solve(route.model)
    result = _SearchResult(incumbent, bound, solver.status_name(status))
    result.record(solver)
    if status == cp_model.MODEL_INVALID:
        raise ValueError("CP-SAT rejected the validated exact model")
    if status == cp_model.INFEASIBLE:
        if incumbent is not None:
            raise RuntimeError("exact infeasibility contradicts a verified incumbent")
        result.proven_infeasible = True
        result.upper_bound_units = None
        return result
    if status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
        visits, ids = route.extract(solver)
        candidate = VerifiedRoute.verify(prepared.problem, graph, visits, ids)
        # IntVar reads preserve exact units above 2**53; the floating objective does not.
        reward = int(solver.value(route.total_reward_units))
        if candidate.simulation.total_reward_units != reward:
            raise RuntimeError("independent simulation reward does not match solver objective")
        result.consider(candidate)
        solver_bound = (
            reward
            if status == cp_model.OPTIMAL
            else integer_upper_bound(solver.best_objective_bound)
        )
        result.upper_bound_units = solver_bound if bound is None else min(bound, solver_bound)
    elif incumbent is not None:
        result.solver_status += "_WITH_INCUMBENT"
        if bound is None:
            result.upper_bound_units = prepared.problem.committed_reward_units + sum(
                contract.reward_units for contract in prepared.problem.contracts
            )

    if result.reward_proven and config.minimize_finish_time_after_proof:
        if progress:
            progress("Reward proven; refining route duration")
        assert result.incumbent is not None
        route.model.add(route.total_reward_units == result.upper_bound_units)
        route.model.minimize(route.finish_time_seconds)
        duration_solver = config.solver(seconds=config.secondary_time_seconds)
        duration_status = duration_solver.solve(route.model)
        result.record(duration_solver)
        if duration_status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            visits, ids = route.extract(duration_solver)
            candidate = VerifiedRoute.verify(prepared.problem, graph, visits, ids)
            if candidate.simulation.total_reward_units != result.upper_bound_units:
                raise RuntimeError("duration refinement changed the proven reward")
            result.consider(candidate)
        elif duration_status in (cp_model.INFEASIBLE, cp_model.MODEL_INVALID):
            raise RuntimeError("duration refinement rejected a verified feasible reward")

    if (
        status == cp_model.OPTIMAL
        and prepared.problem.constraints.collateral_mode is CollateralMode.LOCKED
        and not prepared.problem.active_shipments
        and not prepared.problem.constraints.required_system_ids
        and len(prepared.problem.contracts) <= config.independent_reference_limit
    ):
        reference = solve_reference(prepared, contract_limit=config.independent_reference_limit)
        if reference.objective_units != result.upper_bound_units:
            raise RuntimeError(
                "CP-SAT optimum disagrees with independent exhaustive reference solver: "
                f"{result.upper_bound_units} != {reference.objective_units}"
            )
        result.reference_verified = True
    return result


def _certify(
    prepared: PreparedProblem,
    search: _SearchResult,
    decomposition: DecompositionResult,
    *,
    decomposition_closed: bool,
) -> SolveResult:
    simulation = search.incumbent.simulation if search.incumbent else None
    objective = simulation.total_reward_units if simulation else None
    bound = search.upper_bound_units
    if objective is not None and (search.proven_infeasible or bound is None or bound < objective):
        raise RuntimeError("proof contradicts the independently verified route")
    gap = bound - objective if bound is not None and objective is not None else None
    if search.proven_infeasible:
        status = ProofStatus.PROVEN_INFEASIBLE
    elif gap == 0:
        status = ProofStatus.PROVEN_OPTIMAL
    elif simulation is not None:
        status = ProofStatus.FEASIBLE_NOT_PROVEN
    else:
        status = ProofStatus.UNKNOWN
    relaxation = decomposition.relaxation
    master_seconds = relaxation.wall_time_seconds if relaxation else 0.0
    certificate = OptimalityCertificate(
        status=status,
        solver_status=search.solver_status,
        objective_units=objective,
        best_bound_units=bound,
        absolute_gap_units=gap,
        relative_gap=gap / max(1, abs(objective))
        if gap is not None and objective is not None
        else None,
        problem_sha256=canonical_problem_sha256(prepared.problem, prepared.jump_matrix),
        solver_name="OR-Tools CP-SAT",
        solver_version=package_version("ortools"),
        wall_time_seconds=(
            search.wall_time_seconds + master_seconds + decomposition.subproblem_wall_time_seconds
        ),
        branches=search.branches
        + (relaxation.branches if relaxation else 0)
        + decomposition.subproblem_branches,
        conflicts=search.conflicts
        + (relaxation.conflicts if relaxation else 0)
        + decomposition.subproblem_conflicts,
        scope_untruncated=prepared.problem.scope.is_untruncated,
        feasibility_verified=simulation is not None,
        independent_reference_verified=search.reference_verified,
        claim=optimality_claim(prepared.problem),
        system_relaxation_status=relaxation.status_name if relaxation else None,
        system_relaxation_bound_units=decomposition.upper_bound_units,
        system_relaxation_wall_time_seconds=master_seconds,
        system_relaxation_systems=relaxation.routed_systems if relaxation else 0,
        incompatibility_pairs=len(decomposition.selection_cuts.pairs),
        incompatibility_cliques=len(decomposition.selection_cuts.cliques),
        decomposition_status=decomposition.status_name,
        decomposition_iterations=decomposition.iteration_count,
        decomposition_learned_cuts=(
            len(decomposition.learned_infeasibility_cores) + len(decomposition.learned_reward_cuts)
        ),
        decomposition_subproblem_wall_time_seconds=decomposition.subproblem_wall_time_seconds,
        decomposition_proof_closed=decomposition_closed,
    )
    return SolveResult(
        selected_contract_ids=search.incumbent.selected_contract_ids if search.incumbent else (),
        route=simulation.steps if simulation else (),
        total_reward_units=simulation.total_reward_units if simulation else 0,
        finish_seconds=simulation.finish_seconds if simulation else 0,
        certificate=certificate,
        travel_legs=simulation.travel_legs if simulation else (),
    )


def solve_exact(
    prepared: PreparedProblem,
    graph: UniverseGraph,
    *,
    config: SolverConfig | None = None,
    progress: Callable[[str], None] | None = None,
) -> SolveResult:
    """Prove reward with a smaller master first, then use the complete event model if needed."""
    config = config or SolverConfig()
    if progress:
        progress("Proving reward with the system master and exact route checks")
    incumbent = construct_incumbent(prepared, graph)
    decomposition = prove_with_decomposition(prepared, graph, config, incumbent=incumbent)
    search = _SearchResult(incumbent, decomposition.upper_bound_units, "DECOMPOSITION_OPTIMAL")
    if decomposition.incumbent is not None:
        search.consider(decomposition.incumbent)
    if (
        search.incumbent is not None
        and search.upper_bound_units is not None
        and search.upper_bound_units < search.incumbent.simulation.total_reward_units
    ):
        raise RuntimeError("master bound contradicts a verified incumbent")
    if decomposition.proven_infeasible:
        search.proven_infeasible = True
        search.solver_status = "DECOMPOSITION_INFEASIBLE"
        search.upper_bound_units = None
    elif search.reward_proven:
        if config.minimize_finish_time_after_proof:
            assert search.incumbent is not None
            refinement = refine_selection(
                prepared, graph, search.incumbent.selected_contract_ids, config
            )
            decomposition = replace(
                decomposition,
                subproblem_wall_time_seconds=(
                    decomposition.subproblem_wall_time_seconds + refinement.wall_time_seconds
                ),
                subproblem_branches=decomposition.subproblem_branches + refinement.branches,
                subproblem_conflicts=decomposition.subproblem_conflicts + refinement.conflicts,
            )
            if refinement.simulation is not None:
                search.consider(
                    VerifiedRoute(refinement.selected_contract_ids, refinement.simulation)
                )
    else:
        if progress:
            progress("Searching the complete pickup and delivery model")
        search = _search_complete_model(
            prepared, graph, config, decomposition, search.incumbent, progress
        )
        if (
            search.incumbent is not None
            and not search.reward_proven
            and decomposition.relaxation is not None
        ):
            # Stronger complete hints can consume more of CP-SAT's bounded presolve. Keep its
            # search seed stable; independently improve the final route when reward stays open.
            search.consider(
                diversify_incumbent(
                    prepared,
                    graph,
                    search.incumbent,
                    reward_ceiling=search.upper_bound_units,
                    time_budget_seconds=INCUMBENT_DIVERSIFICATION_SECONDS,
                )
            )
        return _certify(prepared, search, decomposition, decomposition_closed=False)
    return _certify(prepared, search, decomposition, decomposition_closed=True)
