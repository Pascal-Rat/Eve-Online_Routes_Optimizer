"""Search contract selections with a relaxed system route and exact route checks."""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, replace
from enum import Enum, auto

from ortools.sat.python import cp_model

from eve_courier_optimizer.domain import PlannedVisit
from eve_courier_optimizer.optimization.models import selection_bounds, system_tour
from eve_courier_optimizer.optimization.search import (
    fixed_contract_route,
    haul_batches,
    subset_search,
)
from eve_courier_optimizer.optimization.solver_config import SolverConfig
from eve_courier_optimizer.routing.route_problem import RouteProblem
from eve_courier_optimizer.routing.universe import UniverseGraph
from eve_courier_optimizer.verification.route_replay import VerifiedRoute

MIN_CONTRACTS_FOR_SELECTION_SEARCH = 20


@dataclass(frozen=True, slots=True)
class SelectionProof:
    selection_cuts: selection_bounds.SelectionCuts
    master_result: system_tour.SystemRewardBound | None = None
    incumbent: VerifiedRoute | None = None
    proven_infeasible: bool = False
    status_name: str | None = None
    iteration_count: int = 0
    learned_infeasibility_cores: tuple[tuple[int, ...], ...] = ()
    learned_reward_cuts: tuple[selection_bounds.SubsetRewardCut, ...] = ()
    subproblem_wall_time_seconds: float = 0.0
    subproblem_branches: int = 0
    subproblem_conflicts: int = 0

    @property
    def upper_bound_units(self) -> int | None:
        return self.master_result.upper_bound_units if self.master_result else None


class _RoundDecision(Enum):
    CONTINUE = auto()
    STOP = auto()


class ContractSelectionSearch:
    """Own the incumbent, reward ceiling and learned constraints for one selection search.

    The master chooses contracts while relaxing pickup/delivery order. Exact checks either supply
    a verified route or prove a conflict. Equality between a verified reward and the master ceiling
    closes the proof; unfinished searches pass their evidence to the complete event model.
    """

    def __init__(
        self,
        problem: RouteProblem,
        graph: UniverseGraph,
        config: SolverConfig,
        *,
        incumbent: VerifiedRoute | None = None,
        deadline: float = math.inf,
    ) -> None:
        self.problem = problem
        self.graph = graph
        self.config = config
        self.deadline = deadline
        self.selection_cuts = selection_bounds.SelectionCuts((), ())
        self.incumbent = incumbent
        self.best_upper_bound: int | None = None
        self.latest_master: system_tour.SystemRewardBound | None = None
        self.learned_cores: list[tuple[int, ...]] = []
        self.learned_reward_cuts: list[selection_bounds.SubsetRewardCut] = []
        self.master_wall_time = 0.0
        self.master_branches = 0
        self.master_conflicts = 0
        self.route_check_wall_time = 0.0
        self.route_check_branches = 0
        self.route_check_conflicts = 0
        self.iterations = 0
        self.status: str | None = None
        self.proven_infeasible = False

    def run(self) -> SelectionProof:
        if len(self.problem.contracts) < MIN_CONTRACTS_FOR_SELECTION_SEARCH:
            return self._proof()
        phase_seconds = (
            self.config.decomposition_time_seconds or self.config.relaxation_time_seconds
        )
        started = time.perf_counter()
        # Keep time for the complete model when the total allowance is short. Standalone
        # selection searches (no caller deadline) retain their configured phase limit.
        phase_seconds = min(phase_seconds, max(0.0, (self.deadline - started) / 2))
        self.deadline = min(self.deadline, started + phase_seconds)
        if time.perf_counter() >= self.deadline:
            self.status = "budget_exhausted"
            return self._proof()
        self.selection_cuts = selection_bounds.build_selection_cuts(self.problem)
        if self.config.relaxation_time_seconds <= 0:
            self.status = "disabled"
            return self._proof()

        master = system_tour.SystemTourModel(self.problem, selection_cuts=self.selection_cuts)
        self._hint_master(master)
        remaining = self.deadline - time.perf_counter()
        if remaining <= 0:
            self.status = "budget_exhausted"
            return self._proof()
        if self.config.decomposition_time_seconds == 0:
            self._record_master(
                master.solve(
                    max_time_seconds=min(self.config.relaxation_time_seconds, remaining),
                    random_seed=self.config.random_seed,
                )
            )
            self.status = "bound_only"
            return self._proof()

        deadline = self.deadline
        self.status = "budget_exhausted"
        self._search_haul_batches(master)
        if self.reward_proven:
            self.status = "batch_bound_matched"
            return self._proof()
        self._search_master_selections(master, deadline)
        return self._proof()

    @property
    def reward_proven(self) -> bool:
        return (
            self.incumbent is not None
            and self.best_upper_bound == self.incumbent.simulation.total_reward_units
        )

    def _search_master_selections(
        self, master: system_tour.SystemTourModel, deadline: float
    ) -> None:
        for _ in range(self.config.decomposition_max_iterations):
            remaining = deadline - time.perf_counter()
            if remaining <= 0.001:
                break
            self.iterations += 1
            proposal = master.solve(
                max_time_seconds=min(self.config.relaxation_time_seconds, remaining),
                random_seed=self.config.random_seed,
            )
            self._record_master(proposal)
            if proposal.status_name == "INFEASIBLE":
                self.proven_infeasible = True
                self.status = "master_infeasible"
                break
            if self.reward_proven:
                self.status = "bound_matched"
                break
            if proposal.status_name not in {"OPTIMAL", "FEASIBLE"}:
                self.status = f"master_{proposal.status_name.lower()}"
                break
            remaining = deadline - time.perf_counter()
            if remaining <= 0.001:
                self.status = "budget_exhausted"
                break
            budget = min(self.config.decomposition_subproblem_time_seconds, remaining)
            if self._check_selection(master, proposal, deadline, budget) is _RoundDecision.STOP:
                break
        else:
            self.status = "iteration_limit"

    def _search_haul_batches(self, master: system_tour.SystemTourModel) -> None:
        remaining = self.deadline - time.perf_counter()
        if remaining <= 0:
            return
        result = haul_batches.solve_batches(
            self.problem,
            max_time_seconds=min(
                self.config.decomposition_subproblem_time_seconds,
                remaining,
            ),
            random_seed=self.config.random_seed,
        )
        if result is None:
            return
        self.route_check_wall_time += result.wall_time_seconds
        self.route_check_branches += result.cp_branches
        self.route_check_conflicts += result.cp_conflicts
        if result.objective_units is not None:
            self._accept_route(result.visits, result.selected_contract_ids, result.objective_units)
        if result.upper_bound_units is not None:
            self.best_upper_bound = result.upper_bound_units
            batch_status = "BATCH_OPTIMAL" if result.complete else "BATCH_FEASIBLE"
            if result.objective_units is None:
                batch_status = "BATCH_UNKNOWN"
            self.latest_master = system_tour.SystemRewardBound(
                batch_status,
                self.best_upper_bound,
                result.objective_units,
                0.0,
                0,
                0,
                master.routed_systems,
                result.selected_contract_ids,
            )
            if (
                self.incumbent is not None
                and self.best_upper_bound < self.incumbent.simulation.total_reward_units
            ):
                raise RuntimeError("batch bound contradicts a verified incumbent")
            if self.reward_proven:
                return
            self._learn_reward_ceiling(
                master,
                selection_bounds.SubsetRewardCut(
                    tuple((c.contract_id, c.reward_units) for c in self.problem.contracts),
                    self.best_upper_bound,
                ),
            )
        self._hint_master(master)

    def _check_selection(
        self,
        master: system_tour.SystemTourModel,
        proposal: system_tour.SystemRewardBound,
        deadline: float,
        budget: float,
    ) -> _RoundDecision:
        subset = self.problem.restrict_contracts(proposal.selected_contract_ids)
        previous_reward = self.incumbent.simulation.total_reward_units if self.incumbent else -1
        result = subset_search.solve_subset(subset, max_time_seconds=budget)
        if result is not None:
            self.route_check_wall_time += result.wall_time_seconds
            if result.objective_units is not None:
                self._accept_route(
                    result.visits, result.selected_contract_ids, result.objective_units
                )
                if self.reward_proven:
                    self.status = "bound_matched"
                    return _RoundDecision.STOP
            if result.complete:
                if result.upper_bound_units is None:
                    self.proven_infeasible = True
                    self.status = "exact_base_infeasible"
                    return _RoundDecision.STOP
                if result.upper_bound_units < sum(c.reward_units for c in subset.contracts):
                    self._learn_reward_ceiling(
                        master,
                        selection_bounds.SubsetRewardCut(
                            tuple((c.contract_id, c.reward_units) for c in subset.contracts),
                            result.upper_bound_units,
                        ),
                    )
                    if result.infeasible_core_ids:
                        self._learn_conflict(master, result.infeasible_core_ids)
                    self.status = "subset_bound_learned"
                else:
                    self.status = "incumbent_found"
                    if (
                        self.incumbent is not None
                        and self.incumbent.simulation.total_reward_units <= previous_reward
                    ):
                        return _RoundDecision.STOP
                self._hint_master(master)
                return _RoundDecision.CONTINUE
            budget = min(budget - result.wall_time_seconds, deadline - time.perf_counter())
            if budget <= 0.001:
                self.status = "subset_unknown"
                return _RoundDecision.STOP

        check = fixed_contract_route.check_selection(
            self.problem,
            self.graph,
            proposal.selected_contract_ids,
            self.config,
            max_time_seconds=budget,
        )
        self.route_check_wall_time += check.wall_time_seconds
        self.route_check_branches += check.branches
        self.route_check_conflicts += check.conflicts
        if check.status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
            if check.simulation is None or proposal.objective_units is None:
                raise RuntimeError("feasible route check is missing its verified solution")
            if check.simulation.total_reward_units != proposal.objective_units:
                raise RuntimeError("master objective disagrees with the verified route")
            previous_reward = self.incumbent.simulation.total_reward_units if self.incumbent else -1
            candidate = VerifiedRoute(check.selected_contract_ids, check.simulation, self.problem)
            if self.incumbent is None or candidate.quality > self.incumbent.quality:
                self.incumbent = candidate
            if self.reward_proven:
                self.status = "bound_matched"
                return _RoundDecision.STOP
            self.status = "incumbent_found"
            if self.incumbent.simulation.total_reward_units <= previous_reward:
                # A repeated reward cannot justify restarting the same master.
                return _RoundDecision.STOP
            self._hint_master(master)
            return _RoundDecision.CONTINUE
        if check.status != cp_model.INFEASIBLE:
            self.status = f"oracle_{check.status_name.lower()}"
            return _RoundDecision.STOP
        if not check.infeasible_core_ids:
            self.proven_infeasible = True
            self.status = "exact_base_infeasible"
            return _RoundDecision.STOP
        self._learn_conflict(master, check.infeasible_core_ids)
        self.status = "core_learned"
        return _RoundDecision.CONTINUE

    def _accept_route(
        self, visits: tuple[PlannedVisit, ...], selected_ids: tuple[int, ...], reward: int
    ) -> None:
        candidate = VerifiedRoute.verify(self.problem, self.graph, visits, selected_ids)
        if candidate.simulation.total_reward_units != reward:
            raise RuntimeError("exact search reward disagrees with independent route replay")
        if self.incumbent is None or candidate.quality > self.incumbent.quality:
            self.incumbent = candidate

    def _hint_master(self, master: system_tour.SystemTourModel) -> None:
        if self.incumbent is not None:
            master.install_incumbent(self.incumbent)

    def _record_master(self, result: system_tour.SystemRewardBound) -> None:
        self.master_wall_time += result.wall_time_seconds
        self.master_branches += result.branches
        self.master_conflicts += result.conflicts
        if result.upper_bound_units is not None:
            self.best_upper_bound = (
                result.upper_bound_units
                if self.best_upper_bound is None
                else min(self.best_upper_bound, result.upper_bound_units)
            )
        self.latest_master = replace(result, upper_bound_units=self.best_upper_bound)

    def _learn_reward_ceiling(
        self, master: system_tour.SystemTourModel, cut: selection_bounds.SubsetRewardCut
    ) -> None:
        if cut in self.learned_reward_cuts:
            raise RuntimeError("selection search produced a duplicate reward cut")
        selection_bounds.add_subset_reward_cut(master.model, master.contract_is_selected, cut)
        self.learned_reward_cuts.append(cut)

    def _learn_conflict(
        self, master: system_tour.SystemTourModel, contract_ids: tuple[int, ...]
    ) -> None:
        if contract_ids in self.learned_cores:
            raise RuntimeError("selection search produced a duplicate infeasibility core")
        master.exclude_infeasible_selection(contract_ids)
        self.learned_cores.append(contract_ids)

    def _proof(self) -> SelectionProof:
        bound = self.latest_master
        if bound is not None:
            bound = replace(
                bound,
                wall_time_seconds=self.master_wall_time,
                branches=self.master_branches,
                conflicts=self.master_conflicts,
            )
        return SelectionProof(
            selection_cuts=self.selection_cuts,
            master_result=bound,
            incumbent=self.incumbent,
            proven_infeasible=self.proven_infeasible,
            status_name=self.status,
            iteration_count=self.iterations,
            learned_infeasibility_cores=tuple(self.learned_cores),
            learned_reward_cuts=tuple(self.learned_reward_cuts),
            subproblem_wall_time_seconds=self.route_check_wall_time,
            subproblem_branches=self.route_check_branches,
            subproblem_conflicts=self.route_check_conflicts,
        )
