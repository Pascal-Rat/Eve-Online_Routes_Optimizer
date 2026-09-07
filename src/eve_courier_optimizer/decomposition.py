"""Prove reward bounds with a system master and exact checks of its contract selections."""

from __future__ import annotations

import time
from dataclasses import dataclass, replace

from ortools.sat.python import cp_model

from .batch_search import solve_batches
from .bounds import (
    SelectionCuts,
    SubsetRewardCut,
    SystemRelaxationBound,
    add_proven_infeasible_selection_cut,
    add_subset_reward_cut,
    build_selection_cuts,
    build_system_relaxation_master,
    hint_system_relaxation_master,
    solve_system_relaxation_master,
)
from .event_model import EventModel
from .planning import PreparedProblem
from .sde import UniverseGraph
from .search_config import SolverConfig
from .subset_search import solve_subset
from .verification import SimulationResult, VerifiedRoute, simulate_and_verify

_BOUND_STRENGTHENING_MIN_CONTRACTS = 20


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


@dataclass(frozen=True, slots=True)
class DecompositionResult:
    selection_cuts: SelectionCuts
    relaxation: SystemRelaxationBound | None = None
    simulation: SimulationResult | None = None
    selected_contract_ids: tuple[int, ...] = ()
    proven_infeasible: bool = False
    status_name: str | None = None
    iteration_count: int = 0
    learned_infeasibility_cores: tuple[tuple[int, ...], ...] = ()
    subproblem_wall_time_seconds: float = 0.0
    subproblem_branches: int = 0
    subproblem_conflicts: int = 0
    learned_reward_cuts: tuple[SubsetRewardCut, ...] = ()

    @property
    def incumbent(self) -> VerifiedRoute | None:
        if self.simulation is None:
            return None
        return VerifiedRoute(self.selected_contract_ids, self.simulation)

    @property
    def upper_bound_units(self) -> int | None:
        return self.relaxation.upper_bound_units if self.relaxation else None


def _build_dense_selection_cuts(prepared: PreparedProblem) -> SelectionCuts:
    if len(prepared.problem.contracts) < _BOUND_STRENGTHENING_MIN_CONTRACTS:
        return SelectionCuts((), ())
    return build_selection_cuts(prepared)


def _restrict_to_contract_selection(
    prepared: PreparedProblem,
    selected_contract_ids: tuple[int, ...],
) -> PreparedProblem:
    """Keep only one optional contract set while preserving every mandatory route constraint.

    Optional courier feasibility is downward-closed. Removing optional pickup/delivery pairs can
    only remove service time, cargo, collateral, and parcel load. The jump matrix is a metric
    closure, so shortcutting deleted actions never lengthens travel. Mandatory waypoints, active
    shipments and the required finish remain in the problem. Therefore, if a reduced set C is
    infeasible, every full-universe selection containing C is infeasible too.
    """

    selected_contract_id_set = frozenset(selected_contract_ids)
    known_contract_ids = {contract.contract_id for contract in prepared.problem.contracts}
    unknown_contract_ids = tuple(sorted(selected_contract_id_set - known_contract_ids))
    if unknown_contract_ids:
        raise ValueError(
            f"reduced exact selection contains unknown contract IDs: {unknown_contract_ids}"
        )
    selected_contracts = tuple(
        contract
        for contract in prepared.problem.contracts
        if contract.contract_id in selected_contract_id_set
    )
    selected_contract_scores = tuple(
        score for score in prepared.scores if score.contract.contract_id in selected_contract_id_set
    )
    return PreparedProblem(
        problem=replace(prepared.problem, contracts=selected_contracts),
        jump_matrix=prepared.jump_matrix,
        scores=selected_contract_scores,
    )


def _solve_reduced_exact_oracle(
    prepared: PreparedProblem,
    graph: UniverseGraph,
    selected_contract_ids: tuple[int, ...],
    config: SolverConfig,
    *,
    max_time_seconds: float,
) -> SelectionCheck:
    """Test one master selection exactly and return a sufficient infeasibility core when needed."""

    reduced_problem = _restrict_to_contract_selection(prepared, selected_contract_ids)
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
    """Optionally minimize finish time after the decomposition has already proved max reward."""

    reduced_problem = _restrict_to_contract_selection(prepared, selected_contract_ids)
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


def prove_with_decomposition(
    prepared: PreparedProblem,
    graph: UniverseGraph,
    config: SolverConfig,
    *,
    incumbent: VerifiedRoute | None = None,
) -> DecompositionResult:
    """Try to prove the answer with a smaller model before building the full route model.

    The *master* model chooses contracts and routes between systems, but deliberately ignores the
    exact order of pickup and delivery actions within those systems. The exact route model then
    checks the master's proposed contract set. When that set is impossible, CP-SAT returns a
    smaller conflicting set of contracts. Adding that conflict to the master prevents it from
    making the same kind of impossible choice again.

    A feasible exact route whose reward equals the master's upper bound is globally optimal. If
    this loop runs out of time, ``solve_exact`` falls back to the complete route model and reuses
    every bound and conflict learned here.
    """

    selection_cuts = _build_dense_selection_cuts(prepared)
    if len(prepared.problem.contracts) < _BOUND_STRENGTHENING_MIN_CONTRACTS:
        return DecompositionResult(
            selection_cuts=selection_cuts,
        )
    if config.relaxation_time_seconds <= 0:
        return DecompositionResult(
            selection_cuts=selection_cuts,
            status_name="disabled",
        )

    master_model = build_system_relaxation_master(prepared, selection_cuts=selection_cuts)
    if incumbent is not None:
        ids, simulation = incumbent.selected_contract_ids, incumbent.simulation
        hint_system_relaxation_master(
            master_model,
            ids,
            tuple(leg.to_system_id for leg in simulation.travel_legs),
            simulation.total_reward_units,
        )
    if config.decomposition_time_seconds == 0:
        system_relaxation = solve_system_relaxation_master(
            master_model,
            max_time_seconds=config.relaxation_time_seconds,
            random_seed=config.random_seed,
        )
        return DecompositionResult(
            relaxation=system_relaxation,
            selection_cuts=selection_cuts,
            status_name="bound_only",
        )

    decomposition_deadline = time.perf_counter() + config.decomposition_time_seconds
    learned_infeasibility_cores: list[tuple[int, ...]] = []
    learned_reward_cuts: list[SubsetRewardCut] = []
    latest_master_result: SystemRelaxationBound | None = None
    master_wall_time_seconds = 0.0
    master_branches = 0
    master_conflicts = 0
    subproblem_wall_time_seconds = 0.0
    subproblem_branches = 0
    subproblem_conflicts = 0
    iteration_count = 0
    status_name = "budget_exhausted"
    proven_infeasible = False
    verified_simulation = incumbent.simulation if incumbent else None
    selected_contract_ids = incumbent.selected_contract_ids if incumbent else ()
    best_master_bound: int | None = None

    batch_result = solve_batches(
        prepared,
        max_time_seconds=min(
            config.decomposition_subproblem_time_seconds, config.decomposition_time_seconds
        ),
        random_seed=config.random_seed,
    )
    if batch_result is not None:
        subproblem_wall_time_seconds += batch_result.wall_time_seconds
        subproblem_branches += batch_result.cp_branches
        subproblem_conflicts += batch_result.cp_conflicts
        if batch_result.objective_units is not None:
            simulation = simulate_and_verify(
                prepared.problem, graph, batch_result.visits, batch_result.selected_contract_ids
            )
            if (
                not simulation.report.valid
                or simulation.total_reward_units != batch_result.objective_units
            ):
                raise RuntimeError("batch search failed independent full-problem verification")
            if verified_simulation is None or (
                simulation.total_reward_units,
                -simulation.finish_seconds,
            ) > (verified_simulation.total_reward_units, -verified_simulation.finish_seconds):
                verified_simulation = simulation
                selected_contract_ids = batch_result.selected_contract_ids
        if batch_result.upper_bound_units is not None:
            best_master_bound = batch_result.upper_bound_units
            latest_master_result = SystemRelaxationBound(
                "BATCH_OPTIMAL" if batch_result.complete else "BATCH_FEASIBLE",
                best_master_bound,
                batch_result.objective_units,
                0.0,
                0,
                0,
                master_model.routed_systems,
                batch_result.selected_contract_ids,
            )
            if (
                verified_simulation is not None
                and best_master_bound < verified_simulation.total_reward_units
            ):
                raise RuntimeError("batch bound contradicts a verified incumbent")
            if (
                verified_simulation is not None
                and best_master_bound == verified_simulation.total_reward_units
            ):
                return DecompositionResult(
                    relaxation=latest_master_result,
                    selection_cuts=selection_cuts,
                    simulation=verified_simulation,
                    selected_contract_ids=selected_contract_ids,
                    status_name="batch_bound_matched",
                    subproblem_wall_time_seconds=subproblem_wall_time_seconds,
                    subproblem_branches=subproblem_branches,
                    subproblem_conflicts=subproblem_conflicts,
                )
            cut = SubsetRewardCut(
                tuple((i.contract_id, i.reward_units) for i in prepared.problem.contracts),
                best_master_bound,
            )
            add_subset_reward_cut(master_model.model, master_model.contract_is_selected, cut)
            learned_reward_cuts.append(cut)
        if verified_simulation is not None:
            hint_system_relaxation_master(
                master_model,
                selected_contract_ids,
                tuple(leg.to_system_id for leg in verified_simulation.travel_legs),
                verified_simulation.total_reward_units,
            )

    for _ in range(config.decomposition_max_iterations):
        remaining_time_seconds = decomposition_deadline - time.perf_counter()
        if remaining_time_seconds <= 0.001:
            break
        iteration_count += 1

        master_result = solve_system_relaxation_master(
            master_model,
            max_time_seconds=min(config.relaxation_time_seconds, remaining_time_seconds),
            random_seed=config.random_seed,
        )
        master_wall_time_seconds += master_result.wall_time_seconds
        master_branches += master_result.branches
        master_conflicts += master_result.conflicts
        if master_result.upper_bound_units is not None:
            best_master_bound = (
                master_result.upper_bound_units
                if best_master_bound is None
                else min(best_master_bound, master_result.upper_bound_units)
            )
        latest_master_result = replace(master_result, upper_bound_units=best_master_bound)
        if master_result.status_name == "INFEASIBLE":
            proven_infeasible = True
            status_name = "master_infeasible"
            break
        if (
            verified_simulation is not None
            and best_master_bound == verified_simulation.total_reward_units
        ):
            status_name = "bound_matched"
            break
        if master_result.status_name not in {"OPTIMAL", "FEASIBLE"}:
            status_name = f"master_{master_result.status_name.lower()}"
            break

        remaining_time_seconds = decomposition_deadline - time.perf_counter()
        if remaining_time_seconds <= 0.001:
            status_name = "budget_exhausted"
            break
        oracle_budget = min(config.decomposition_subproblem_time_seconds, remaining_time_seconds)
        subset = _restrict_to_contract_selection(prepared, master_result.selected_contract_ids)
        subset_result = solve_subset(subset, max_time_seconds=oracle_budget)
        if subset_result is not None:
            subproblem_wall_time_seconds += subset_result.wall_time_seconds
            if subset_result.objective_units is not None:
                simulation = simulate_and_verify(
                    prepared.problem,
                    graph,
                    subset_result.visits,
                    subset_result.selected_contract_ids,
                )
                if (
                    not simulation.report.valid
                    or simulation.total_reward_units != subset_result.objective_units
                ):
                    raise RuntimeError("subset search failed independent full-problem verification")
                previous_reward = (
                    verified_simulation.total_reward_units if verified_simulation else -1
                )
                if verified_simulation is None or (
                    simulation.total_reward_units,
                    -simulation.finish_seconds,
                ) > (verified_simulation.total_reward_units, -verified_simulation.finish_seconds):
                    verified_simulation = simulation
                    selected_contract_ids = subset_result.selected_contract_ids
                if best_master_bound == verified_simulation.total_reward_units:
                    status_name = "bound_matched"
                    break
            if subset_result.complete:
                if subset_result.upper_bound_units is None:
                    proven_infeasible = True
                    status_name = "exact_base_infeasible"
                    break
                if subset_result.upper_bound_units < sum(
                    i.reward_units for i in subset.problem.contracts
                ):
                    cut = SubsetRewardCut(
                        tuple((i.contract_id, i.reward_units) for i in subset.problem.contracts),
                        subset_result.upper_bound_units,
                    )
                    if cut in learned_reward_cuts:
                        raise RuntimeError("decomposition produced a duplicate subset reward cut")
                    add_subset_reward_cut(
                        master_model.model, master_model.contract_is_selected, cut
                    )
                    learned_reward_cuts.append(cut)
                    core = subset_result.infeasible_core_ids
                    if core:
                        if core in learned_infeasibility_cores:
                            raise RuntimeError("decomposition produced a duplicate subset core")
                        add_proven_infeasible_selection_cut(master_model, core)
                        learned_infeasibility_cores.append(core)
                    status_name = "subset_bound_learned"
                else:
                    status_name = "incumbent_found"
                    if (
                        verified_simulation is not None
                        and verified_simulation.total_reward_units <= previous_reward
                    ):
                        break
                if verified_simulation is not None:
                    hint_system_relaxation_master(
                        master_model,
                        selected_contract_ids,
                        tuple(leg.to_system_id for leg in verified_simulation.travel_legs),
                        verified_simulation.total_reward_units,
                    )
                continue
            oracle_budget = min(
                oracle_budget - subset_result.wall_time_seconds,
                decomposition_deadline - time.perf_counter(),
            )
            if oracle_budget <= 0.001:
                status_name = "subset_unknown"
                break
        exact_route_result = _solve_reduced_exact_oracle(
            prepared,
            graph,
            master_result.selected_contract_ids,
            config,
            max_time_seconds=oracle_budget,
        )
        subproblem_wall_time_seconds += exact_route_result.wall_time_seconds
        subproblem_branches += exact_route_result.branches
        subproblem_conflicts += exact_route_result.conflicts
        if exact_route_result.status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            if exact_route_result.simulation is None or master_result.objective_units is None:
                raise RuntimeError("feasible reduced exact oracle is missing its verified solution")
            if exact_route_result.simulation.total_reward_units != master_result.objective_units:
                raise RuntimeError(
                    "master objective disagrees with the verified reduced exact route"
                )
            previous_reward = verified_simulation.total_reward_units if verified_simulation else -1
            if verified_simulation is None or (
                exact_route_result.simulation.total_reward_units,
                -exact_route_result.simulation.finish_seconds,
            ) > (verified_simulation.total_reward_units, -verified_simulation.finish_seconds):
                verified_simulation = exact_route_result.simulation
                selected_contract_ids = exact_route_result.selected_contract_ids
            if best_master_bound != verified_simulation.total_reward_units:
                status_name = "incumbent_found"
                if verified_simulation.total_reward_units <= previous_reward:
                    break  # A repeated reward does not justify restarting the same master again.
                hint_system_relaxation_master(
                    master_model,
                    selected_contract_ids,
                    tuple(leg.to_system_id for leg in verified_simulation.travel_legs),
                    verified_simulation.total_reward_units,
                )
                continue
            status_name = "bound_matched"
            break
        if exact_route_result.status != cp_model.INFEASIBLE:
            status_name = f"oracle_{exact_route_result.status_name.lower()}"
            break
        infeasible_contract_ids = exact_route_result.infeasible_core_ids
        if not infeasible_contract_ids:
            proven_infeasible = True
            status_name = "exact_base_infeasible"
            break
        if infeasible_contract_ids in learned_infeasibility_cores:
            raise RuntimeError("logic-based decomposition produced a duplicate infeasibility core")
        add_proven_infeasible_selection_cut(master_model, infeasible_contract_ids)
        learned_infeasibility_cores.append(infeasible_contract_ids)
        status_name = "core_learned"
    else:
        status_name = "iteration_limit"

    if latest_master_result is not None:
        latest_master_result = replace(
            latest_master_result,
            wall_time_seconds=master_wall_time_seconds,
            branches=master_branches,
            conflicts=master_conflicts,
        )
    return DecompositionResult(
        relaxation=latest_master_result,
        selection_cuts=selection_cuts,
        simulation=verified_simulation,
        selected_contract_ids=selected_contract_ids,
        proven_infeasible=proven_infeasible,
        status_name=status_name,
        iteration_count=iteration_count,
        learned_infeasibility_cores=tuple(learned_infeasibility_cores),
        subproblem_wall_time_seconds=subproblem_wall_time_seconds,
        subproblem_branches=subproblem_branches,
        subproblem_conflicts=subproblem_conflicts,
        learned_reward_cuts=tuple(learned_reward_cuts),
    )
