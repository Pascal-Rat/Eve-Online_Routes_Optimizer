"""The plan interchange contract, shared by runtime validation and browser type generation."""

from __future__ import annotations

import math
import types
from collections.abc import Hashable
from functools import lru_cache
from typing import (
    Literal,
    NotRequired,
    TypedDict,
    Union,
    cast,
    get_args,
    get_origin,
    get_type_hints,
    is_typeddict,
)


class NamedSystem(TypedDict):
    id: int
    name: str


class PathSystem(TypedDict):
    system_id: int
    name: str
    security_status: float | None
    security_band: str | None


class PlanSummary(TypedDict):
    selected_contract_ids: list[int]
    total_reward_units: int
    total_reward_isk: str
    finish_seconds: int | None


class BoundStrengthening(TypedDict):
    system_relaxation_status: str | None
    system_relaxation_bound_units: int | None
    system_relaxation_bound_isk: str | None
    system_relaxation_wall_time_seconds: float
    system_relaxation_systems: int
    incompatibility_pairs: int
    incompatibility_cliques: int
    decomposition_status: str | None
    decomposition_iterations: int
    decomposition_learned_cuts: int
    decomposition_subproblem_wall_time_seconds: float
    decomposition_proof_closed: bool


class CertificatePayload(TypedDict):
    status: Literal["proven_optimal", "proven_infeasible", "feasible_not_proven", "unknown"]
    solver_status: str
    objective_units: int | None
    objective_isk: str | None
    best_bound_units: int | None
    best_bound_isk: str | None
    absolute_gap_units: int | None
    relative_gap: float | None
    problem_sha256: str
    solver_name: str
    solver_version: str
    wall_time_seconds: float
    branches: int
    conflicts: int
    scope_untruncated: bool
    feasibility_verified: bool
    independent_reference_verified: bool
    bound_strengthening: BoundStrengthening
    claim: str


class PlanScope(TypedDict):
    snapshot_fetched_at: str
    snapshot_compatibility_date: str
    sde_build_number: int
    scanned_region_ids: list[int]
    public_couriers_seen: int
    eligible_contracts: int
    policy_exclusions: dict[str, int]
    safe_reductions: dict[str, int]
    heuristic_reductions: dict[str, int]


class PlanModel(TypedDict):
    start_system_id: int
    cargo_capacity_m3: str
    collateral_budget_isk: str
    horizon_seconds: int
    snapshot_time: str
    collateral_mode: Literal["locked", "rolling"]
    return_to_start: bool
    required_system_ids: list[int]
    finish_system_id: int | None
    terminal_system_id: int | None
    max_simultaneous_contracts: int | None
    seconds_per_jump: int
    service_seconds: int
    minimum_security: float | None
    allowed_security_bands: list[str] | None
    avoided_system_ids: list[int]
    gank_avoided_system_ids: list[int]
    gank_ship_kill_threshold: int | None
    gank_activity_fetched_at: str | None
    threat_avoided_system_ids: list[int]
    threat_categories: list[str]
    threat_min_events: int | None
    threat_intel_fetched_at: str | None
    threat_window_seconds: int | None
    threat_gate_radius_m: int | None
    threat_coverage_region_ids: list[int]
    threat_incomplete_region_ids: list[int]
    start_system_name: NotRequired[str | None]
    finish_system_name: NotRequired[str | None]
    avoided_systems: NotRequired[list[NamedSystem]]
    required_systems: NotRequired[list[NamedSystem]]


class RouteStepPayload(TypedDict):
    sequence: int
    action: Literal["pickup", "delivery"]
    contract_id: int
    system_id: int
    location_id: int
    arrival_seconds: int
    completion_seconds: int
    cargo_after_units: int
    cargo_after_m3: str
    collateral_after_units: int
    collateral_after_isk: str
    cumulative_reward_units: int
    cumulative_reward_isk: str
    jump_path: list[int]
    system_name: NotRequired[str | None]
    jump_count: NotRequired[int]
    jump_path_systems: NotRequired[list[PathSystem]]
    title: NotRequired[str]
    reward_isk: NotRequired[str]
    volume_m3: NotRequired[str]
    mandatory: NotRequired[bool]


class TravelLegPayload(TypedDict):
    sequence: int
    kind: Literal["pickup", "delivery", "waypoint", "finish"]
    from_system_id: int
    to_system_id: int
    arrival_seconds: int
    completion_seconds: int
    contract_id: int | None
    jump_path: list[int]
    from_system_name: NotRequired[str | None]
    to_system_name: NotRequired[str | None]
    jump_count: NotRequired[int]
    jump_path_systems: NotRequired[list[PathSystem]]


class PlanPayload(TypedDict):
    schema_version: Literal[3]
    summary: PlanSummary
    certificate: CertificatePayload
    scope: PlanScope
    model: PlanModel
    route: list[RouteStepPayload]
    travel_legs: list[TravelLegPayload]


@lru_cache
def contract_fields(schema: Hashable) -> dict[str, object]:
    return cast(dict[str, object], get_type_hints(cast(type[object], schema), include_extras=True))


def validate_contract(value: object, annotation: object, path: str = "response") -> None:
    """Validate the closed set of JSON type forms used by these explicit contracts.

    Additional object keys are allowed for additive display metadata. Schema versions remain
    literals, and bool never satisfies an integer field. Unsupported annotations are code errors.
    """
    origin = get_origin(annotation)
    arguments = cast(tuple[object, ...], get_args(annotation))
    if origin is NotRequired:
        validate_contract(value, arguments[0], path)
        return
    if origin in (Union, types.UnionType):
        for option in arguments:
            try:
                validate_contract(value, option, path)
                return
            except ValueError:
                continue
        raise ValueError(f"{path} does not match its declared type")
    if origin is Literal:
        if not any(type(value) is type(option) and value == option for option in arguments):
            raise ValueError(f"{path} has an unsupported value")
        return
    if is_typeddict(annotation):
        if not isinstance(value, dict):
            raise ValueError(f"{path} must be an object")
        mapping = cast(dict[str, object], value)
        for key, field in contract_fields(annotation).items():
            if key not in mapping and get_origin(field) is NotRequired:
                continue
            if key not in mapping:
                raise ValueError(f"{path}.{key} is required")
            validate_contract(mapping[key], field, f"{path}.{key}")
        return
    if origin is list:
        if not isinstance(value, list):
            raise ValueError(f"{path} must be a list")
        for item in cast(list[object], value):
            validate_contract(item, arguments[0], f"{path}[]")
        return
    if origin is dict:
        if not isinstance(value, dict):
            raise ValueError(f"{path} must be an object")
        for raw_key, item in cast(dict[object, object], value).items():
            validate_contract(raw_key, arguments[0], f"{path} key")
            validate_contract(item, arguments[1], f"{path}.{raw_key}")
        return
    if annotation is float:
        if type(value) not in (int, float) or not math.isfinite(cast(float, value)):
            raise ValueError(f"{path} must be a finite number")
    elif isinstance(annotation, type) and annotation in (int, str, bool, type(None)):
        if type(value) is not annotation:
            raise ValueError(f"{path} has an invalid primitive type")
    else:
        raise TypeError(f"unsupported JSON contract annotation: {annotation!r}")


def validate_saved_plan(value: object) -> PlanPayload:
    validate_contract(value, PlanPayload, "plan")
    return cast(PlanPayload, value)
