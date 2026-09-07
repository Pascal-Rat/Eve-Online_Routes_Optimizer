"""Canonical problem fingerprints and human-readable proof claims."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from importlib.metadata import version as package_version
from typing import Any

from ortools.sat.python import cp_model

from eve_courier_optimizer.domain import (
    OptimalityCertificate,
    ProofStatus,
    RouteProblem,
    SolveResult,
)
from eve_courier_optimizer.routing.policy import security_policy_to_dict
from eve_courier_optimizer.routing.preparation import PreparedProblem
from eve_courier_optimizer.routing.replay import VerifiedRoute

from .selection import SelectionProof


def canonical_problem_sha256(
    problem: RouteProblem,
    jump_matrix: dict[tuple[int, int], int],
) -> str:
    constraints = problem.constraints
    payload: dict[str, Any] = {
        "constraints": {
            "start_system_id": constraints.start_system_id,
            "cargo_capacity_units": constraints.cargo_capacity_units,
            "collateral_budget_units": constraints.collateral_budget_units,
            "horizon_seconds": constraints.horizon_seconds,
            "snapshot_time": constraints.snapshot_time.isoformat(),
            "collateral_mode": constraints.collateral_mode.value,
            "return_to_start": constraints.return_to_start,
            "required_system_ids": sorted(constraints.required_system_ids),
            "finish_system_id": constraints.finish_system_id,
            "terminal_system_id": constraints.terminal_system_id,
            "max_simultaneous_contracts": constraints.max_simultaneous_contracts,
            "seconds_per_jump": constraints.travel.seconds_per_jump,
            "service_seconds": constraints.travel.service_seconds,
            **security_policy_to_dict(constraints.security, bands_key="allowed_security_bands"),
        },
        "contracts": [
            {
                "id": item.contract_id,
                "origin_location": item.origin_location_id,
                "destination_location": item.destination_location_id,
                "origin_system": item.origin_system_id,
                "destination_system": item.destination_system_id,
                "volume": item.volume_units,
                "collateral": item.collateral_units,
                "reward": item.reward_units,
                "expires": item.date_expired.isoformat(),
                "days": item.days_to_complete,
            }
            for item in sorted(problem.contracts, key=lambda item: item.contract_id)
        ],
        "active_shipments": [
            {
                "id": item.contract.contract_id,
                "origin_location": item.contract.origin_location_id,
                "destination_location": item.contract.destination_location_id,
                "origin_system": item.contract.origin_system_id,
                "destination_system": item.contract.destination_system_id,
                "volume": item.contract.volume_units,
                "collateral": item.contract.collateral_units,
                "reward": item.contract.reward_units,
                "deadline": item.deadline.isoformat(),
                "picked": item.picked,
            }
            for item in sorted(
                problem.active_shipments,
                key=lambda item: item.contract.contract_id,
            )
        ],
        "scope": {
            "snapshot_fetched_at": problem.scope.snapshot_fetched_at.isoformat(),
            "snapshot_compatibility_date": problem.scope.snapshot_compatibility_date,
            "sde_build_number": problem.scope.sde_build_number,
            "regions": list(problem.scope.scanned_region_ids),
            "seen": problem.scope.public_couriers_seen,
            "eligible": problem.scope.eligible_contracts,
            "policy_exclusions": list(problem.scope.policy_exclusions),
            "safe_reductions": list(problem.scope.safe_reductions),
            "heuristic_reductions": list(problem.scope.heuristic_reductions),
        },
        "jump_matrix": [
            [source, destination, jumps]
            for (source, destination), jumps in sorted(jump_matrix.items())
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def optimality_claim(problem: RouteProblem) -> str:
    base = (
        "Objective is total courier reward under the recorded contract snapshot, SDE stargate "
        "graph, declared routing/route-shape policy, integer cargo/collateral/parcel resource "
        "model, and deterministic travel-time model."
    )
    if problem.scope.is_untruncated:
        return (
            "PROVEN OPTIMAL means no feasible route with greater reward exists among all contracts "
            "remaining after the declared endpoint/routing policy exclusions and mathematically "
            f"proof-preserving reductions. {base}"
        )
    return (
        "The solver may prove optimality only inside a heuristically truncated candidate set; it "
        "must NOT be interpreted as a global optimum over every otherwise eligible contract. "
        f"{base}"
    )


@dataclass(slots=True)
class SearchEvidence:
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


def certify(
    prepared: PreparedProblem,
    search: SearchEvidence,
    proof: SelectionProof,
    *,
    selection_closed: bool,
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
    relaxation = proof.master_result
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
            search.wall_time_seconds + master_seconds + proof.subproblem_wall_time_seconds
        ),
        branches=search.branches
        + (relaxation.branches if relaxation else 0)
        + proof.subproblem_branches,
        conflicts=search.conflicts
        + (relaxation.conflicts if relaxation else 0)
        + proof.subproblem_conflicts,
        scope_untruncated=prepared.problem.scope.is_untruncated,
        feasibility_verified=simulation is not None,
        independent_reference_verified=search.reference_verified,
        claim=optimality_claim(prepared.problem),
        system_relaxation_status=relaxation.status_name if relaxation else None,
        system_relaxation_bound_units=proof.upper_bound_units,
        system_relaxation_wall_time_seconds=master_seconds,
        system_relaxation_systems=relaxation.routed_systems if relaxation else 0,
        incompatibility_pairs=len(proof.selection_cuts.pairs),
        incompatibility_cliques=len(proof.selection_cuts.cliques),
        decomposition_status=proof.status_name,
        decomposition_iterations=proof.iteration_count,
        decomposition_learned_cuts=(
            len(proof.learned_infeasibility_cores) + len(proof.learned_reward_cuts)
        ),
        decomposition_subproblem_wall_time_seconds=proof.subproblem_wall_time_seconds,
        decomposition_proof_closed=selection_closed,
    )
    return SolveResult(
        selected_contract_ids=search.incumbent.selected_contract_ids if search.incumbent else (),
        route=simulation.steps if simulation else (),
        total_reward_units=simulation.total_reward_units if simulation else 0,
        finish_seconds=simulation.finish_seconds if simulation else 0,
        certificate=certificate,
        travel_legs=simulation.travel_legs if simulation else (),
    )
