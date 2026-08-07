"""Stable JSON result format shared by the CLI and localhost UI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .domain import RouteProblem, SolveResult, isk_units_to_decimal, volume_units_to_decimal


def solve_result_to_dict(result: SolveResult, problem: RouteProblem) -> dict[str, Any]:
    certificate = result.certificate
    return {
        "schema_version": 3,
        "summary": {
            "selected_contract_ids": list(result.selected_contract_ids),
            "total_reward_units": result.total_reward_units,
            "total_reward_isk": str(isk_units_to_decimal(result.total_reward_units)),
            "finish_seconds": result.finish_seconds,
        },
        "certificate": {
            "status": certificate.status.value,
            "solver_status": certificate.solver_status,
            "objective_units": certificate.objective_units,
            "objective_isk": (
                str(isk_units_to_decimal(certificate.objective_units))
                if certificate.objective_units is not None
                else None
            ),
            "best_bound_units": certificate.best_bound_units,
            "best_bound_isk": (
                str(isk_units_to_decimal(certificate.best_bound_units))
                if certificate.best_bound_units is not None
                else None
            ),
            "absolute_gap_units": certificate.absolute_gap_units,
            "relative_gap": certificate.relative_gap,
            "problem_sha256": certificate.problem_sha256,
            "solver_name": certificate.solver_name,
            "solver_version": certificate.solver_version,
            "wall_time_seconds": certificate.wall_time_seconds,
            "branches": certificate.branches,
            "conflicts": certificate.conflicts,
            "scope_untruncated": certificate.scope_untruncated,
            "feasibility_verified": certificate.feasibility_verified,
            "independent_reference_verified": certificate.independent_reference_verified,
            "bound_strengthening": {
                "system_relaxation_status": certificate.system_relaxation_status,
                "system_relaxation_bound_units": certificate.system_relaxation_bound_units,
                "system_relaxation_bound_isk": (
                    str(isk_units_to_decimal(certificate.system_relaxation_bound_units))
                    if certificate.system_relaxation_bound_units is not None
                    else None
                ),
                "system_relaxation_wall_time_seconds": (
                    certificate.system_relaxation_wall_time_seconds
                ),
                "system_relaxation_systems": certificate.system_relaxation_systems,
                "incompatibility_pairs": certificate.incompatibility_pairs,
                "incompatibility_cliques": certificate.incompatibility_cliques,
                "decomposition_status": certificate.decomposition_status,
                "decomposition_iterations": certificate.decomposition_iterations,
                "decomposition_learned_cuts": certificate.decomposition_learned_cuts,
                "decomposition_subproblem_wall_time_seconds": (
                    certificate.decomposition_subproblem_wall_time_seconds
                ),
                "decomposition_proof_closed": certificate.decomposition_proof_closed,
            },
            "claim": certificate.claim,
        },
        "scope": {
            "snapshot_fetched_at": problem.scope.snapshot_fetched_at.isoformat(),
            "snapshot_compatibility_date": problem.scope.snapshot_compatibility_date,
            "sde_build_number": problem.scope.sde_build_number,
            "scanned_region_ids": list(problem.scope.scanned_region_ids),
            "public_couriers_seen": problem.scope.public_couriers_seen,
            "eligible_contracts": problem.scope.eligible_contracts,
            "policy_exclusions": dict(problem.scope.policy_exclusions),
            "safe_reductions": dict(problem.scope.safe_reductions),
            "heuristic_reductions": dict(problem.scope.heuristic_reductions),
        },
        "model": {
            "start_system_id": problem.constraints.start_system_id,
            "cargo_capacity_m3": str(
                volume_units_to_decimal(problem.constraints.cargo_capacity_units)
            ),
            "collateral_budget_isk": str(
                isk_units_to_decimal(problem.constraints.collateral_budget_units)
            ),
            "horizon_seconds": problem.constraints.horizon_seconds,
            "snapshot_time": problem.constraints.snapshot_time.isoformat(),
            "collateral_mode": problem.constraints.collateral_mode.value,
            "return_to_start": problem.constraints.return_to_start,
            "required_system_ids": sorted(problem.constraints.required_system_ids),
            "finish_system_id": problem.constraints.finish_system_id,
            "terminal_system_id": problem.constraints.terminal_system_id,
            "max_simultaneous_contracts": problem.constraints.max_simultaneous_contracts,
            "seconds_per_jump": problem.constraints.travel.seconds_per_jump,
            "service_seconds": problem.constraints.travel.service_seconds,
            "minimum_security": problem.constraints.security.minimum_security,
            "avoided_system_ids": sorted(problem.constraints.security.avoided_system_ids),
            "allowed_security_bands": (
                sorted(band.value for band in problem.constraints.security.allowed_bands)
                if problem.constraints.security.allowed_bands is not None
                else None
            ),
            "gank_avoided_system_ids": sorted(problem.constraints.security.gank_avoided_system_ids),
            "gank_ship_kill_threshold": problem.constraints.security.gank_ship_kill_threshold,
            "gank_activity_fetched_at": (
                problem.constraints.security.gank_activity_fetched_at.isoformat()
                if problem.constraints.security.gank_activity_fetched_at is not None
                else None
            ),
            "threat_avoided_system_ids": sorted(
                problem.constraints.security.threat_avoided_system_ids
            ),
            "threat_categories": sorted(
                category.value for category in problem.constraints.security.threat_categories
            ),
            "threat_min_events": problem.constraints.security.threat_min_events,
            "threat_intel_fetched_at": (
                problem.constraints.security.threat_intel_fetched_at.isoformat()
                if problem.constraints.security.threat_intel_fetched_at is not None
                else None
            ),
            "threat_window_seconds": problem.constraints.security.threat_window_seconds,
            "threat_gate_radius_m": problem.constraints.security.threat_gate_radius_m,
            "threat_coverage_region_ids": sorted(
                problem.constraints.security.threat_coverage_region_ids
            ),
            "threat_incomplete_region_ids": sorted(
                problem.constraints.security.threat_incomplete_region_ids
            ),
        },
        "route": [
            {
                "sequence": step.sequence,
                "action": step.action.value,
                "contract_id": step.contract_id,
                "system_id": step.system_id,
                "location_id": step.location_id,
                "arrival_seconds": step.arrival_seconds,
                "completion_seconds": step.completion_seconds,
                "cargo_after_units": step.cargo_after_units,
                "cargo_after_m3": str(volume_units_to_decimal(step.cargo_after_units)),
                "collateral_after_units": step.collateral_after_units,
                "collateral_after_isk": str(isk_units_to_decimal(step.collateral_after_units)),
                "cumulative_reward_units": step.cumulative_reward_units,
                "cumulative_reward_isk": str(isk_units_to_decimal(step.cumulative_reward_units)),
                "jump_path": list(step.jump_path),
            }
            for step in result.route
        ],
        "travel_legs": [
            {
                "sequence": leg.sequence,
                "kind": leg.kind.value,
                "from_system_id": leg.from_system_id,
                "to_system_id": leg.to_system_id,
                "arrival_seconds": leg.arrival_seconds,
                "completion_seconds": leg.completion_seconds,
                "contract_id": leg.contract_id,
                "jump_path": list(leg.jump_path),
            }
            for leg in result.travel_legs
        ],
    }


def write_solve_result(path: Path, result: SolveResult, problem: RouteProblem) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(solve_result_to_dict(result, problem), indent=2, sort_keys=True) + "\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(serialized, encoding="utf-8")
    temporary.replace(path)
