"""Canonical problem fingerprints and human-readable proof claims."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from .domain import RouteProblem
from .route_policy import security_policy_to_dict


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
