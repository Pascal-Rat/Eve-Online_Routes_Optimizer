"""Explicit response shapes for the localhost UI and its asynchronous jobs."""

from __future__ import annotations

from typing import Literal, NotRequired, TypedDict

from eve_courier_optimizer.application.plan_contract import PlanPayload


class SnapshotSummary(TypedDict):
    fetched_at: str
    age_seconds: int
    compatibility_date: str
    sde_build_number: int
    region_ids: list[int]
    region_names: list[str]
    contracts: int
    system_kills_fetched_at: str | None
    system_kill_systems: int
    threat_intel_fetched_at: str | None
    threat_window_seconds: int | None
    threat_gate_radius_m: int | None
    threat_coverage_region_ids: list[int]
    threat_incomplete_region_ids: list[int]
    threat_killmails_seen: int
    gate_threat_events: int


class PublicContractPayload(TypedDict):
    contract_id: int
    origin_location_id: int
    destination_location_id: int
    volume_units: int
    collateral_units: int
    reward_units: int
    date_expired: str
    days_to_complete: int
    title: str
    date_issued: str | None


class ActiveShipmentPayload(TypedDict):
    contract: PublicContractPayload
    origin_system_id: int
    destination_system_id: int
    origin_system_name: str
    destination_system_name: str
    deadline: str
    picked: bool


class TravelPayload(TypedDict):
    seconds_per_jump: int
    service_seconds: int


class ExecutionSecurity(TypedDict):
    minimum_security: float | None
    avoided_system_ids: list[int]
    allowed_bands: list[str] | None
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


class RequiredSystemPayload(TypedDict):
    system_id: int
    name: str


class ExecutionPayload(TypedDict):
    schema_version: Literal[3]
    current_time: str
    session_deadline: str
    current_system_id: int
    cargo_capacity_units: int
    collateral_budget_units: int
    collateral_mode: Literal["locked", "rolling"]
    terminal_system_id: int | None
    remaining_required_system_ids: list[int]
    max_simultaneous_contracts: int | None
    travel: TravelPayload
    security: ExecutionSecurity
    completed_contract_ids: list[int]
    active_shipments: list[ActiveShipmentPayload]
    current_system_name: str | None
    active_count: int
    completed_count: int
    cargo_in_use_m3: str
    collateral_locked_isk: str
    terminal_system_name: str | None
    remaining_required_systems: list[RequiredSystemPayload]
    can_end_safely: bool
    horizon_expired: bool


class RankScope(TypedDict):
    public_couriers_seen: int
    eligible_contracts: int
    policy_exclusions: dict[str, int]
    safe_reductions: dict[str, int]
    heuristic_reductions: dict[str, int]
    scope_untruncated: bool


class RankItem(TypedDict):
    contract_id: int
    title: str
    origin: str
    destination: str
    reward_isk: str
    volume_m3: str
    collateral_isk: str
    solo_jumps: int
    solo_seconds: int
    reward_per_hour_isk: float | None
    reward_per_jump_isk: float | None
    reward_to_collateral: float | None


class RankingPayload(TypedDict):
    scope: RankScope
    items: list[RankItem]


class TransitionIdentity(TypedDict):
    revision: int
    proposal_id: str | None


class ScanResponse(TransitionIdentity):
    snapshot: SnapshotSummary | None


class PlanResponse(TransitionIdentity):
    snapshot: SnapshotSummary | None
    plan: PlanPayload
    execution: ExecutionPayload | None


class RankResponse(TransitionIdentity, RankingPayload):
    pass


class ExecutionResponse(TransitionIdentity):
    execution: ExecutionPayload | None
    plan: NotRequired[PlanPayload | None]


OperationResponse = ScanResponse | PlanResponse | RankResponse
MutationResponse = OperationResponse | ExecutionResponse


class SdeSummary(TypedDict):
    build_number: int
    release_date: str
    systems: int
    regions: int
    empire_regions: int
    npc_stations: int


class ArtifactsPayload(TypedDict):
    snapshot: bool
    plan: bool
    execution: bool


class StatusResponse(TransitionIdentity):
    app_version: str
    warnings: list[str]
    snapshot_requires_refresh: bool
    sde: SdeSummary
    snapshot: SnapshotSummary | None
    plan: PlanPayload | None
    execution: ExecutionPayload | None
    plan_armable: bool
    artifacts: ArtifactsPayload


class JobFields(TypedDict):
    id: str
    operation: Literal["scan", "rank", "solve", "replan"]
    progress: str
    elapsed_seconds: float


class RunningJob(JobFields):
    status: Literal["running"]


class CompletedJob(JobFields):
    status: Literal["completed"]
    result: OperationResponse


class FailedJob(JobFields):
    status: Literal["failed"]
    error: str


class CancelledJob(JobFields):
    status: Literal["cancelled"]


JobPayload = RunningJob | CompletedJob | FailedJob | CancelledJob


class StatusWithJob(StatusResponse):
    job: JobPayload | None


class JobEnvelope(TypedDict):
    job: JobPayload


class Suggestion(TypedDict):
    id: int
    name: str
    security_status: NotRequired[float]


class Suggestions(TypedDict):
    items: list[Suggestion]
