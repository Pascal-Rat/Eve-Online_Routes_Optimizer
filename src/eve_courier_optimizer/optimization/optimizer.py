"""Optimize reward, retain verified incumbents, and assemble one auditable proof result."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace

from ortools.sat.python import cp_model

from eve_courier_optimizer.domain import CollateralMode, SolveResult
from eve_courier_optimizer.optimization import proof_certificate
from eve_courier_optimizer.optimization.models import pickup_delivery
from eve_courier_optimizer.optimization.models.selection_bounds import (
    add_selection_cuts,
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
        if self.progress:
            self.progress("Proving reward with the system master and exact route checks")
        incumbent = route_insertion.construct_incumbent(self.problem, self.graph)
        proof = contract_selection.ContractSelectionSearch(
            self.problem, self.graph, self.config, incumbent=incumbent
        ).run()
        search = proof_certificate.SearchEvidence(
            incumbent, proof.upper_bound_units, "DECOMPOSITION_OPTIMAL"
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
            if self.config.minimize_finish_time_after_proof:
                assert search.incumbent is not None
                refinement = fixed_contract_route.refine_selection(
                    self.problem, self.graph, search.incumbent.selected_contract_ids, self.config
                )
                proof = replace(
                    proof,
                    subproblem_wall_time_seconds=(
                        proof.subproblem_wall_time_seconds + refinement.wall_time_seconds
                    ),
                    subproblem_branches=proof.subproblem_branches + refinement.branches,
                    subproblem_conflicts=proof.subproblem_conflicts + refinement.conflicts,
                )
                if refinement.simulation is not None:
                    search.consider(
                        VerifiedRoute(refinement.selected_contract_ids, refinement.simulation)
                    )
        else:
            if self.progress:
                self.progress("Searching the complete pickup and delivery model")
            search = self._search_complete_model(proof, search.incumbent)
            if (
                search.incumbent is not None
                and not search.reward_proven
                and proof.master_result is not None
            ):
                # Stronger complete hints can consume more of CP-SAT's bounded presolve. Keep its
                # seed stable; independently improve the final route when reward stays open.
                search.consider(
                    route_insertion.diversify_incumbent(
                        self.problem,
                        self.graph,
                        search.incumbent,
                        reward_ceiling=search.upper_bound_units,
                        time_budget_seconds=INCUMBENT_DIVERSIFICATION_SECONDS,
                    )
                )
            return proof_certificate.certify(self.problem, search, proof, selection_closed=False)
        return proof_certificate.certify(self.problem, search, proof, selection_closed=True)

    def _search_complete_model(
        self, proof: contract_selection.SelectionProof, incumbent: VerifiedRoute | None
    ) -> proof_certificate.SearchEvidence:
        route = pickup_delivery.PickupDeliveryModel(
            self.problem, selection_hint=build_greedy_route_hint(self.problem)
        )
        bound = proof.upper_bound_units
        if bound is not None:
            route.model.add(route.total_reward_units <= bound)
        add_selection_cuts(route.model, route.contract_is_selected, proof.selection_cuts)
        for core in proof.learned_infeasibility_cores:
            route.model.add(sum(route.contract_is_selected[cid] for cid in core) <= len(core) - 1)
        for cut in proof.learned_reward_cuts:
            add_subset_reward_cut(route.model, route.contract_is_selected, cut)
        if incumbent is not None:
            route.hint(incumbent.simulation.visits, incumbent.selected_contract_ids)
        validation_error = route.model.validate()
        if validation_error:
            raise ValueError(f"invalid or numerically unsafe CP-SAT model: {validation_error}")

        solver = self.config.solver()
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
            result.upper_bound_units = solver_bound if bound is None else min(bound, solver_bound)
        elif incumbent is not None:
            result.solver_status += "_WITH_INCUMBENT"
            if bound is None:
                result.upper_bound_units = self.problem.committed_reward_units + sum(
                    contract.reward_units for contract in self.problem.contracts
                )

        if result.reward_proven and self.config.minimize_finish_time_after_proof:
            if self.progress:
                self.progress("Reward proven; refining route duration")
            assert result.incumbent is not None
            route.model.add(route.total_reward_units == result.upper_bound_units)
            route.model.minimize(route.finish_time_seconds)
            duration_solver = self.config.solver(seconds=self.config.secondary_time_seconds)
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

        if (
            status == cp_model.OPTIMAL
            and self.problem.constraints.collateral_mode is CollateralMode.LOCKED
            and not self.problem.active_shipments
            and not self.problem.constraints.required_system_ids
            and len(self.problem.contracts) <= self.config.independent_reference_limit
        ):
            reference = solve_exhaustively(
                self.problem, contract_limit=self.config.independent_reference_limit
            )
            if reference.objective_units != result.upper_bound_units:
                raise RuntimeError(
                    "CP-SAT optimum disagrees with independent exhaustive reference solver: "
                    f"{result.upper_bound_units} != {reference.objective_units}"
                )
            result.reference_verified = True
        return result
