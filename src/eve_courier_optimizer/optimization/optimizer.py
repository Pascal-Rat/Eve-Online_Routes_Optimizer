"""Optimize reward, retain verified incumbents, and assemble one auditable proof result."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from ortools.sat.python import cp_model

from eve_courier_optimizer.domain import CollateralMode, SolveResult
from eve_courier_optimizer.optimization import proof_certificate
from eve_courier_optimizer.optimization.models import pickup_delivery
from eve_courier_optimizer.optimization.models.selection_bounds import (
    RewardBoundRecorder,
    add_subset_reward_cut,
    integer_upper_bound,
)
from eve_courier_optimizer.optimization.search import (
    contract_selection,
    fixed_contract_route,
    route_insertion,
)
from eve_courier_optimizer.optimization.search.route_insertion import build_greedy_route_hint
from eve_courier_optimizer.optimization.solver_config import (
    INCUMBENT_DIVERSIFICATION_SECONDS,
    SearchBudget,
    SolverConfig,
)
from eve_courier_optimizer.routing.route_problem import RouteProblem
from eve_courier_optimizer.routing.universe import UniverseGraph
from eve_courier_optimizer.verification.exhaustive_optimum import solve_exhaustively
from eve_courier_optimizer.verification.route_replay import VerifiedRoute


class RouteOptimizer:
    """Coordinate selection proofs, complete event search and certification for one problem."""

    def __init__(
        self,
        problem: RouteProblem,
        graph: UniverseGraph,
        *,
        config: SolverConfig | None = None,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self.problem = problem
        self.graph = graph
        self.config = config or SolverConfig()
        self.progress = progress

    def solve(self) -> SolveResult:
        budget = SearchBudget(self.config.max_time_seconds)
        if self.progress:
            self.progress("Proving reward with the system master and exact route checks")
        incumbent = route_insertion.construct_incumbent(
            self.problem, self.graph, deadline=budget.deadline
        )
        proof = contract_selection.ContractSelectionSearch(
            self.problem, self.graph, self.config, incumbent=incumbent, deadline=budget.deadline
        ).run()
        trivial_bound = self.problem.committed_reward_units + sum(
            item.reward_units for item in self.problem.contracts
        )
        search = proof_certificate.SearchEvidence(
            incumbent,
            proof.upper_bound_units if proof.upper_bound_units is not None else trivial_bound,
            "DECOMPOSITION_OPTIMAL"
            if proof.upper_bound_units is not None
            else "TRIVIAL_BOUND_MATCHED",
        )
        if proof.incumbent is not None:
            search.consider(proof.incumbent)
        if (
            search.incumbent is not None
            and search.upper_bound_units is not None
            and search.upper_bound_units < search.incumbent.simulation.total_reward_units
        ):
            raise RuntimeError("master bound contradicts a verified incumbent")
        if proof.proven_infeasible:
            search.proven_infeasible = True
            search.solver_status = "DECOMPOSITION_INFEASIBLE"
            search.upper_bound_units = None
        elif search.reward_proven:
            proof = self._refine_selection(search, proof, budget)
        else:
            if budget.expired():
                search.solver_status = "TIME_LIMIT"
                return proof_certificate.certify(
                    self.problem, search, proof, selection_closed=False
                )
            if (
                search.incumbent is not None
                and not search.reward_proven
                and proof.master_result is not None
                and not budget.expired()
            ):
                # Improve the verified seed before exact search spends the remaining budget.
                # The full solver can never replace it with a lower-quality route.
                search.consider(
                    route_insertion.diversify_incumbent(
                        self.problem,
                        self.graph,
                        search.incumbent,
                        reward_ceiling=search.upper_bound_units,
                        time_budget_seconds=min(
                            INCUMBENT_DIVERSIFICATION_SECONDS, budget.remaining() / 4
                        ),
                    )
                )
            if not search.reward_proven and not budget.expired():
                if self.progress:
                    self.progress("Searching the complete pickup and delivery model")
                search = self._search_complete_model(proof, search.incumbent, budget)
            elif not search.reward_proven:
                search.solver_status = "TIME_LIMIT"
            else:
                proof = self._refine_selection(search, proof, budget)
            self._check_reference(search, budget)
            return proof_certificate.certify(self.problem, search, proof, selection_closed=False)
        self._check_reference(search, budget)
        return proof_certificate.certify(
            self.problem,
            search,
            proof,
            selection_closed=proof.proven_infeasible or proof.upper_bound_units is not None,
        )

    def _refine_selection(
        self,
        search: proof_certificate.SearchEvidence,
        proof: contract_selection.SelectionProof,
        budget: SearchBudget,
    ) -> contract_selection.SelectionProof:
        if not self.config.minimize_finish_time_after_proof or budget.expired():
            return proof
        assert search.incumbent is not None and search.reward_proven
        refinement = fixed_contract_route.refine_selection(
            self.problem,
            self.graph,
            search.incumbent.selected_contract_ids,
            self.config,
            deadline=budget.deadline,
        )
        if refinement.simulation is not None:
            search.consider(VerifiedRoute(refinement.selected_contract_ids, refinement.simulation))
        return replace(
            proof,
            subproblem_wall_time_seconds=(
                proof.subproblem_wall_time_seconds + refinement.wall_time_seconds
            ),
            subproblem_branches=proof.subproblem_branches + refinement.branches,
            subproblem_conflicts=proof.subproblem_conflicts + refinement.conflicts,
        )

    def _search_complete_model(
        self,
        proof: contract_selection.SelectionProof,
        incumbent: VerifiedRoute | None,
        budget: SearchBudget,
    ) -> proof_certificate.SearchEvidence:
        route = pickup_delivery.PickupDeliveryModel(
            self.problem,
            selection_hint=() if incumbent else build_greedy_route_hint(self.problem),
            selection_cuts=proof.selection_cuts,
        )
        bound = proof.upper_bound_units
        if bound is None:
            bound = self.problem.committed_reward_units + sum(
                item.reward_units for item in self.problem.contracts
            )
        route.model.add(route.total_reward_units <= bound)
        for core in proof.learned_infeasibility_cores:
            route.model.add(sum(route.contract_is_selected[cid] for cid in core) <= len(core) - 1)
        for cut in proof.learned_reward_cuts:
            add_subset_reward_cut(route.model, route.contract_is_selected, cut)
        if incumbent is not None:
            route.hint(incumbent.simulation.visits, incumbent.selected_contract_ids)
        validation_error = route.model.validate()
        if validation_error:
            raise ValueError(f"invalid or numerically unsafe CP-SAT model: {validation_error}")

        if budget.expired():
            return proof_certificate.SearchEvidence(incumbent, bound, "TIME_LIMIT")
        solver = self.config.solver(seconds=budget.remaining())
        bounds = RewardBoundRecorder()
        solver.best_bound_callback = bounds
        status = solver.solve(route.model)
        result = proof_certificate.SearchEvidence(incumbent, bound, solver.status_name(status))
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
            candidate = VerifiedRoute.verify(self.problem, self.graph, visits, ids)
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
            result.upper_bound_units = min(bound, solver_bound)
        elif status == cp_model.UNKNOWN:
            if bounds.upper_bound_units is not None:
                result.upper_bound_units = min(bound, bounds.upper_bound_units)
            if incumbent is not None:
                result.solver_status += "_WITH_INCUMBENT"

        if (
            result.reward_proven
            and self.config.minimize_finish_time_after_proof
            and not budget.expired()
        ):
            if self.progress:
                self.progress("Reward proven; refining route duration")
            assert result.incumbent is not None
            route.model.add(route.total_reward_units == result.upper_bound_units)
            route.model.minimize(route.finish_time_seconds)
            duration_solver = self.config.solver(
                seconds=budget.remaining(self.config.secondary_time_seconds)
            )
            duration_status = duration_solver.solve(route.model)
            result.record(duration_solver)
            if duration_status in (cp_model.OPTIMAL, cp_model.FEASIBLE):
                visits, ids = route.extract(duration_solver)
                candidate = VerifiedRoute.verify(self.problem, self.graph, visits, ids)
                if candidate.simulation.total_reward_units != result.upper_bound_units:
                    raise RuntimeError("duration refinement changed the proven reward")
                result.consider(candidate)
            elif duration_status in (cp_model.INFEASIBLE, cp_model.MODEL_INVALID):
                raise RuntimeError("duration refinement rejected a verified feasible reward")

        return result

    def _check_reference(
        self, result: proof_certificate.SearchEvidence, budget: SearchBudget
    ) -> None:
        if (
            result.reward_proven
            and self.problem.constraints.collateral_mode is CollateralMode.LOCKED
            and not self.problem.active_shipments
            and not self.problem.constraints.required_system_ids
            and len(self.problem.contracts) <= self.config.independent_reference_limit
            and not budget.expired()
        ):
            reference = solve_exhaustively(
                self.problem,
                contract_limit=self.config.independent_reference_limit,
                deadline=budget.deadline,
            )
            if reference.complete and reference.objective_units != result.upper_bound_units:
                raise RuntimeError(
                    "CP-SAT optimum disagrees with independent exhaustive reference solver: "
                    f"{result.upper_bound_units} != {reference.objective_units}"
                )
            result.reference_verified = reference.complete
