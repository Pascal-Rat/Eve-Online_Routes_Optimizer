"""Parse local HTTP planning options into explicit domain constraints."""

from __future__ import annotations

import math
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal, InvalidOperation

from eve_courier_optimizer.domain import (
    CollateralMode,
    ContractSnapshot,
    PlanningConstraints,
    SecurityBand,
    SecurityPolicy,
    ThreatCategory,
    TravelTimeModel,
    cargo_capacity_to_units,
    isk_to_units,
    parse_human_isk,
)
from eve_courier_optimizer.jsonio import json_bool
from eve_courier_optimizer.optimization import SolverConfig
from eve_courier_optimizer.routing.policy import observed_security_policy, reachable_threat_regions
from eve_courier_optimizer.routing.universe import UniverseGraph


def _positive_decimal(value: object, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError(f"{label} must be a number") from error
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"{label} must be a positive finite number")
    return parsed


def _nonnegative_decimal(value: object, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except InvalidOperation as error:
        raise ValueError(f"{label} must be a number") from error
    if not parsed.is_finite() or parsed < 0:
        raise ValueError(f"{label} must be a non-negative finite number")
    return parsed


def _optional_positive_int(value: object, label: str) -> int | None:
    if value is None or value == "":
        return None
    try:
        parsed = int(str(value))
    except ValueError as error:
        raise ValueError(f"{label} must be an integer") from error
    if parsed <= 0:
        raise ValueError(f"{label} must be positive")
    return parsed


def _nonnegative_int(value: object, label: str) -> int:
    try:
        parsed = int(str(value))
    except ValueError as error:
        raise ValueError(f"{label} must be an integer") from error
    if parsed < 0:
        raise ValueError(f"{label} cannot be negative")
    return parsed


def _optional_nonnegative_int(value: object, label: str) -> int | None:
    if value is None or value == "":
        return None
    return _nonnegative_int(value, label)


def _duration_seconds(body: dict[str, object]) -> int:
    if "duration_hours" in body or "duration_minutes" in body:
        hours = _nonnegative_int(body.get("duration_hours", 0), "duration_hours")
        minutes = _nonnegative_int(body.get("duration_minutes", 0), "duration_minutes")
        if minutes > 59:
            raise ValueError("duration_minutes must be between 0 and 59")
        seconds = hours * 3600 + minutes * 60
        if seconds <= 0:
            raise ValueError("time budget must be greater than zero")
        return seconds
    # Backward-compatible API path for v1 clients that sent decimal hours.
    decimal_hours = _positive_decimal(body.get("hours", 3), "hours")
    return int((decimal_hours * 3600).to_integral_value(rounding=ROUND_FLOOR))


def allowed_security_bands(body: dict[str, object]) -> frozenset[SecurityBand]:
    raw_bands = body.get("security_bands")
    if raw_bands is None:
        security_value = str(body.get("security", "highsec"))
        legacy = {
            "highsec": frozenset({SecurityBand.HIGH}),
            "any": frozenset(SecurityBand),
        }
        try:
            allowed_bands = legacy[security_value]
        except KeyError as error:
            raise ValueError("security must be 'highsec' or 'any'") from error
        return allowed_bands
    if not isinstance(raw_bands, list):
        raise ValueError("security_bands must be a list")
    try:
        allowed_bands = frozenset(SecurityBand(str(value)) for value in raw_bands)
    except ValueError as error:
        raise ValueError("security_bands may contain only high, low, and null") from error
    if not allowed_bands:
        raise ValueError("select at least one security band")
    return allowed_bands


def threat_regions_from_scan_body(
    body: dict[str, object], graph: UniverseGraph
) -> tuple[int, ...] | None:
    if not json_bool(body.get("threat_scope_to_plan", False), "threat_scope_to_plan"):
        return None
    start_system_id = graph.resolve_system(body.get("start", ""))
    bands = allowed_security_bands(body)
    seconds_per_jump = _nonnegative_int(body.get("seconds_per_jump", 60), "seconds_per_jump")
    return reachable_threat_regions(
        graph,
        start_system_id=start_system_id,
        security=SecurityPolicy(minimum_security=None, allowed_bands=bands),
        horizon_seconds=_duration_seconds(body),
        seconds_per_jump=seconds_per_jump,
    )


def constraints(
    body: dict[str, object], snapshot: ContractSnapshot, graph: UniverseGraph, now: datetime
) -> PlanningConstraints:
    allowed_bands = allowed_security_bands(body)
    mode_value = str(body.get("collateral_mode", CollateralMode.LOCKED.value))
    try:
        mode = CollateralMode(mode_value)
    except ValueError as error:
        raise ValueError("invalid collateral mode") from error
    raw_avoids = body.get("avoid_systems", [])
    if isinstance(raw_avoids, str):
        avoids = [item.strip() for item in raw_avoids.split(",") if item.strip()]
    elif isinstance(raw_avoids, list):
        avoids = [str(item) for item in raw_avoids]
    else:
        raise ValueError("avoid_systems must be a list or comma-separated string")
    raw_required = body.get("required_systems", [])
    if isinstance(raw_required, str):
        required = [item.strip() for item in raw_required.split(",") if item.strip()]
    elif isinstance(raw_required, list):
        required = [str(item) for item in raw_required]
    else:
        raise ValueError("required_systems must be a list or comma-separated string")
    return_to_start = json_bool(body.get("return_to_start", True), "return_to_start")
    raw_finish = body.get("finish_system")
    finish_system_id = (
        None
        if raw_finish is None or str(raw_finish).strip() == ""
        else graph.resolve_system(raw_finish)
    )
    seconds_per_jump = int(str(body.get("seconds_per_jump", 60)))
    service_seconds = int(str(body.get("service_seconds", 30)))
    start_system_id = graph.resolve_system(body.get("start", ""))
    gank_awareness = json_bool(body.get("gank_awareness", False), "gank_awareness")
    gank_threshold: int | None = None
    threat_categories: frozenset[ThreatCategory] = frozenset()
    threat_min_events: int | None = None
    if gank_awareness and body.get("threat_categories") is not None:
        raw_categories = body.get("threat_categories")
        if not isinstance(raw_categories, list):
            raise ValueError("threat_categories must be a list")
        try:
            threat_categories = frozenset(ThreatCategory(str(value)) for value in raw_categories)
        except ValueError as error:
            raise ValueError("threat_categories contains an unknown category") from error
        if not threat_categories:
            raise ValueError("select at least one gate-threat category")
        threat_min_events = _optional_positive_int(
            body.get("threat_min_events", 1),
            "threat_min_events",
        )
        if threat_min_events is None:
            raise ValueError("threat_min_events is required when threat awareness is on")
    elif gank_awareness:
        # Backward-compatible API path for v1 snapshots/clients. The localhost UI uses the
        # zKill category path above.
        gank_threshold = _optional_positive_int(
            body.get("gank_ship_kill_threshold", 10),
            "gank_ship_kill_threshold",
        )
        if gank_threshold is None:
            raise ValueError("gank_ship_kill_threshold is required when gank awareness is on")
    return PlanningConstraints(
        start_system_id=start_system_id,
        cargo_capacity_units=cargo_capacity_to_units(
            _nonnegative_decimal(body.get("cargo_m3", ""), "cargo_m3")
        ),
        collateral_budget_units=isk_to_units(
            parse_human_isk(
                str(body.get("collateral_isk", "")),
                unit=str(body.get("collateral_unit", "auto")),
            )
        ),
        horizon_seconds=_duration_seconds(body),
        snapshot_time=max(snapshot.fetched_at, now),
        collateral_mode=mode,
        travel=TravelTimeModel(seconds_per_jump, service_seconds),
        security=observed_security_policy(
            snapshot,
            minimum_security=None,
            avoided_system_ids=frozenset(graph.resolve_system(value) for value in avoids),
            allowed_bands=allowed_bands,
            activity_threshold=gank_threshold,
            threat_categories=threat_categories,
            threat_min_events=threat_min_events,
            exempt_system_ids=frozenset({start_system_id}),
        ),
        return_to_start=return_to_start,
        required_system_ids=frozenset(graph.resolve_system(value) for value in required),
        finish_system_id=finish_system_id,
        max_simultaneous_contracts=_optional_nonnegative_int(
            body.get("max_simultaneous_contracts"),
            "max_simultaneous_contracts",
        ),
    )


def solver_config(body: dict[str, object]) -> SolverConfig:
    try:
        time_limit = float(str(body.get("time_limit", 60)))
        workers = int(str(body.get("workers", 4)))
    except ValueError as error:
        raise ValueError("time_limit and workers must be numeric") from error
    if not math.isfinite(time_limit) or time_limit <= 0:
        raise ValueError("time_limit must be a positive finite number")
    return SolverConfig(
        max_time_seconds=time_limit,
        num_workers=workers,
        # Once maximum reward is proven, spend only a small bounded tail finding a faster route
        # among reward ties. The v1 default could silently add another 30 seconds.
        secondary_time_seconds=min(5.0, time_limit),
    )


def max_candidates(body: dict[str, object]) -> int | None:
    return _optional_positive_int(body.get("max_candidates"), "max_candidates")
