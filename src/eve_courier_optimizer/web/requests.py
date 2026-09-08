"""Decode browser requests before they enter the planner or change the saved trip."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_FLOOR, Decimal, InvalidOperation
from typing import cast

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
from eve_courier_optimizer.eve.zkill import DEFAULT_THREAT_WINDOW_SECONDS
from eve_courier_optimizer.jsonio import json_bool
from eve_courier_optimizer.optimization import SolverConfig
from eve_courier_optimizer.routing.security import (
    observed_security_policy,
    reachable_threat_regions,
)
from eve_courier_optimizer.routing.universe import UniverseGraph


@dataclass(frozen=True, slots=True)
class ScanRequest:
    region_ids: tuple[int, ...]
    include_threat_intel: bool
    threat_window_seconds: int
    threat_gate_radius_m: int
    threat_region_ids: tuple[int, ...] | None

    @classmethod
    def from_json(cls, body: dict[str, object], graph: UniverseGraph) -> ScanRequest:
        fields = FormFields(body)
        scope = str(body.get("region_scope", "selected"))
        if scope == "all":
            region_ids = tuple(sorted(graph.regions))
        elif scope in {"security", "empire"}:
            compatible = graph.region_ids_for_security_bands(_security_bands(body))
            if scope == "empire":
                compatible &= graph.empire_region_ids()
                if not compatible:
                    raise ValueError(
                        "NPC Empire scope contains no regions in the selected security bands"
                    )
            region_ids = tuple(sorted(compatible))
        elif scope == "selected":
            raw_regions = body.get("regions")
            if not isinstance(raw_regions, list) or not raw_regions:
                raise ValueError("regions must contain at least one region name or ID")
            region_ids = tuple(
                graph.resolve_region(value) for value in cast(list[object], raw_regions)
            )
        else:
            raise ValueError("region_scope must be selected, security, empire, or all")
        include_threat = fields.boolean("include_threat_intel", default=False)
        window_hours = fields.integer(
            "threat_window_hours", default=DEFAULT_THREAT_WINDOW_SECONDS // 3_600
        )
        if include_threat and not 1 <= window_hours <= 168:
            raise ValueError("threat_window_hours must be between 1 and 168")
        radius_km = fields.integer("threat_gate_radius_km", default=250)
        threat_regions = None
        if include_threat and fields.boolean("threat_scope_to_plan", default=False):
            threat_regions = reachable_threat_regions(
                graph,
                start_system_id=graph.resolve_system(body.get("start", "")),
                security=SecurityPolicy(minimum_security=None, allowed_bands=_security_bands(body)),
                horizon_seconds=_duration_seconds(fields),
                seconds_per_jump=fields.integer("seconds_per_jump", default=60),
            )
        return cls(
            region_ids, include_threat, window_hours * 3_600, radius_km * 1_000, threat_regions
        )


@dataclass(frozen=True, slots=True)
class PlanRequest:
    constraints: PlanningConstraints
    max_candidates: int | None

    @classmethod
    def from_json(
        cls,
        body: dict[str, object],
        snapshot: ContractSnapshot,
        graph: UniverseGraph,
        now: datetime,
    ) -> PlanRequest:
        fields = FormFields(body)
        start = graph.resolve_system(body.get("start", ""))
        raw_finish = body.get("finish_system")
        finish = (
            None
            if raw_finish is None or str(raw_finish).strip() == ""
            else graph.resolve_system(raw_finish)
        )
        try:
            collateral_mode = CollateralMode(str(body.get("collateral_mode", "locked")))
        except ValueError as error:
            raise ValueError("invalid collateral mode") from error
        constraints = PlanningConstraints(
            start_system_id=start,
            cargo_capacity_units=cargo_capacity_to_units(fields.decimal("cargo_m3")),
            collateral_budget_units=isk_to_units(
                parse_human_isk(
                    body.get("collateral_isk", ""), unit=str(body.get("collateral_unit", "auto"))
                )
            ),
            horizon_seconds=_duration_seconds(fields),
            snapshot_time=max(snapshot.fetched_at, now),
            collateral_mode=collateral_mode,
            travel=TravelTimeModel(
                fields.integer("seconds_per_jump", default=60, minimum=1),
                fields.integer("service_seconds", default=30),
            ),
            security=_route_security(fields, snapshot, graph, start),
            return_to_start=fields.boolean("return_to_start", default=True),
            required_system_ids=frozenset(
                graph.resolve_system(value) for value in fields.system_names("required_systems")
            ),
            finish_system_id=finish,
            max_simultaneous_contracts=fields.optional_integer("max_simultaneous_contracts"),
        )
        return cls(constraints, fields.optional_integer("max_candidates", minimum=1))


@dataclass(frozen=True, slots=True)
class ReplanRequest:
    refresh_snapshot: bool
    max_candidates: int | None
    solver_config: SolverConfig

    @classmethod
    def from_json(cls, body: dict[str, object]) -> ReplanRequest:
        fields = FormFields(body)
        return cls(
            fields.boolean("refresh", default=False),
            fields.optional_integer("max_candidates", minimum=1),
            read_solver_config(body),
        )


def read_solver_config(body: dict[str, object]) -> SolverConfig:
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
        # Desktop users get a bounded duration-refinement tail after a reward proof closes.
        secondary_time_seconds=min(5.0, time_limit),
    )


class FormFields:
    """Read JSON form values, accepting both browser strings and JSON numbers.

    Booleans remain strict: the string "false" must never enable an option.
    """

    def __init__(self, body: dict[str, object]) -> None:
        self.body = body

    def boolean(self, name: str, *, default: bool) -> bool:
        return json_bool(self.body.get(name, default), name)

    def integer(self, name: str, *, default: int | None = None, minimum: int = 0) -> int:
        try:
            value = int(str(self.body.get(name, default)))
        except ValueError as error:
            raise ValueError(f"{name} must be an integer") from error
        if value < minimum:
            requirement = "must be positive" if minimum == 1 else "cannot be negative"
            raise ValueError(f"{name} {requirement}")
        return value

    def optional_integer(self, name: str, *, minimum: int = 0) -> int | None:
        if self.body.get(name) in (None, ""):
            return None
        return self.integer(name, minimum=minimum)

    def decimal(self, name: str, *, default: int | None = None, positive: bool = False) -> Decimal:
        try:
            value = Decimal(str(self.body.get(name, default)))
        except InvalidOperation as error:
            raise ValueError(f"{name} must be a number") from error
        if not value.is_finite() or value < 0 or (positive and value == 0):
            requirement = "positive" if positive else "non-negative"
            raise ValueError(f"{name} must be a {requirement} finite number")
        return value

    def system_names(self, name: str) -> list[str]:
        raw = self.body.get(name, [])
        if isinstance(raw, str):
            return [value.strip() for value in raw.split(",") if value.strip()]
        if isinstance(raw, list):
            return [str(value) for value in cast(list[object], raw)]
        raise ValueError(f"{name} must be a list or comma-separated string")


def _duration_seconds(fields: FormFields) -> int:
    if "duration_hours" in fields.body or "duration_minutes" in fields.body:
        hours = fields.integer("duration_hours", default=0)
        minutes = fields.integer("duration_minutes", default=0)
        if minutes > 59:
            raise ValueError("duration_minutes must be between 0 and 59")
        seconds = hours * 3_600 + minutes * 60
        if seconds <= 0:
            raise ValueError("time budget must be greater than zero")
        return seconds
    # The public API also accepts decimal hours; the UI sends separate hour/minute fields.
    decimal_hours = fields.decimal("hours", default=3, positive=True)
    return int((decimal_hours * 3_600).to_integral_value(rounding=ROUND_FLOOR))


def _security_bands(body: dict[str, object]) -> frozenset[SecurityBand]:
    raw = body.get("security_bands")
    if raw is None:
        names = {"highsec": frozenset({SecurityBand.HIGH}), "any": frozenset(SecurityBand)}
        try:
            return names[str(body.get("security", "highsec"))]
        except KeyError as error:
            raise ValueError("security must be 'highsec' or 'any'") from error
    if not isinstance(raw, list):
        raise ValueError("security_bands must be a list")
    try:
        bands = frozenset(SecurityBand(str(value)) for value in cast(list[object], raw))
    except ValueError as error:
        raise ValueError("security_bands may contain only high, low, and null") from error
    if not bands:
        raise ValueError("select at least one security band")
    return bands


def _route_security(
    fields: FormFields, snapshot: ContractSnapshot, graph: UniverseGraph, start: int
) -> SecurityPolicy:
    categories: frozenset[ThreatCategory] = frozenset()
    minimum_events = None
    activity_threshold = None
    body = fields.body
    if fields.boolean("gank_awareness", default=False):
        if body.get("threat_categories") is not None:
            raw = body["threat_categories"]
            if not isinstance(raw, list):
                raise ValueError("threat_categories must be a list")
            try:
                categories = frozenset(
                    ThreatCategory(str(value)) for value in cast(list[object], raw)
                )
            except ValueError as error:
                raise ValueError("threat_categories contains an unknown category") from error
            if not categories:
                raise ValueError("select at least one gate-threat category")
            minimum_events = fields.integer("threat_min_events", default=1, minimum=1)
        else:
            # Older saved clients use aggregate ESI activity instead of gate classifications.
            activity_threshold = fields.integer("gank_ship_kill_threshold", default=10, minimum=1)
    return observed_security_policy(
        snapshot,
        minimum_security=None,
        allowed_bands=_security_bands(body),
        avoided_system_ids=frozenset(
            graph.resolve_system(value) for value in fields.system_names("avoid_systems")
        ),
        activity_threshold=activity_threshold,
        threat_categories=categories,
        threat_min_events=minimum_events,
        exempt_system_ids=frozenset({start}),
    )
