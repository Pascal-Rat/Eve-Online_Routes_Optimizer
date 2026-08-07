"""Loopback-only HTTP application for the iteration-5 local control deck."""

from __future__ import annotations

import json
import math
import mimetypes
import os
import sys
import webbrowser
from datetime import UTC, datetime
from decimal import ROUND_FLOOR, Decimal
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

from . import __version__
from .domain import (
    CollateralMode,
    PlanningConstraints,
    PublicCourierContract,
    SecurityBand,
    SecurityPolicy,
    SolveResult,
    ThreatCategory,
    TravelTimeModel,
    cargo_capacity_to_units,
    isk_to_units,
    isk_units_to_decimal,
    parse_esi_datetime,
    parse_human_isk,
    security_band,
    volume_units_to_decimal,
)
from .esi import EsiClient, EsiResponseCache
from .execution import (
    ExecutionState,
    execution_state_to_dict,
    initial_execution_state,
    read_execution_state,
    record_delivery,
    record_pickup,
    record_route_system,
    write_execution_state,
)
from .planning import PreparedProblem, prepare_problem, rank_single_contracts
from .reporting import solve_result_to_dict, write_solve_result
from .sde import UniverseGraph
from .service import PlannerService
from .snapshot import ContractSnapshot, read_snapshot, write_snapshot
from .solver import SolverConfig, solve_exact
from .threat_intel import (
    DEFAULT_THREAT_WINDOW_SECONDS,
    ZkillClient,
    threat_avoided_systems,
)

JsonObject = dict[str, Any]
_LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost"})
_DOWNLOAD_FILES = frozenset({"snapshot.json", "plan.json", "execution.json"})
_MAX_REQUEST_BYTES = 1_048_576


def default_web_workspace() -> Path:
    """Return the durable, credential-free working directory used by the local UI."""

    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "EveCourierRouteOptimizer"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "EveCourierRouteOptimizer"
    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg_data_home) if xdg_data_home else Path.home() / ".local" / "share"
    return base / "eve-courier-route-optimizer"


def _positive_decimal(value: object, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except Exception as error:
        raise ValueError(f"{label} must be a number") from error
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(f"{label} must be a positive finite number")
    return parsed


def _nonnegative_decimal(value: object, label: str) -> Decimal:
    try:
        parsed = Decimal(str(value))
    except Exception as error:
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


def _duration_seconds(body: JsonObject) -> int:
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


class LocalWebApplication:
    """Stateful application facade used by the HTTP handler and direct tests.

    Snapshots, plans, and execution state are persisted as the same versioned JSON artifacts used by
    the CLI. Browser refreshes therefore do not silently discard accepted courier commitments.
    """

    def __init__(
        self,
        graph: UniverseGraph,
        esi: EsiClient,
        workspace: Path,
        zkill: ZkillClient | None = None,
    ) -> None:
        self.graph = graph
        self.esi = esi
        self.zkill = zkill
        self.service = PlannerService(graph, esi, zkill)
        self.workspace = workspace
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.snapshot_path = workspace / "snapshot.json"
        self.plan_path = workspace / "plan.json"
        self.execution_path = workspace / "execution.json"
        self.snapshot: ContractSnapshot | None = None
        self.prepared: PreparedProblem | None = None
        self.result: SolveResult | None = None
        self.execution: ExecutionState | None = None
        self.plan_payload: JsonObject | None = None
        self._load_existing_artifacts()

    def _load_existing_artifacts(self) -> None:
        if self.snapshot_path.exists():
            snapshot = read_snapshot(self.snapshot_path)
            if snapshot.sde_build_number == self.graph.metadata.build_number:
                self.snapshot = snapshot
        if self.execution_path.exists():
            self.execution = read_execution_state(self.execution_path)
        if self.plan_path.exists():
            raw = json.loads(self.plan_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self.plan_payload = self._decorate_saved_plan(cast(JsonObject, raw))

    def _resolve_system(self, value: object) -> int:
        text = str(value).strip()
        if not text:
            raise ValueError("start system is required")
        try:
            system_id = int(text)
        except ValueError:
            matches = [
                system.system_id
                for system in self.graph.systems.values()
                if system.name.casefold() == text.casefold()
            ]
            if len(matches) != 1:
                raise ValueError(f"could not resolve unique system {text!r}") from None
            return matches[0]
        if system_id not in self.graph.systems:
            raise ValueError(f"unknown system ID {system_id}")
        return system_id

    def _resolve_region(self, value: object) -> int:
        text = str(value).strip()
        if not text:
            raise ValueError("region cannot be empty")
        try:
            region_id = int(text)
        except ValueError:
            matches = [
                region.region_id
                for region in self.graph.regions.values()
                if region.name.casefold() == text.casefold()
            ]
            if len(matches) != 1:
                raise ValueError(f"could not resolve unique region {text!r}") from None
            return matches[0]
        if region_id not in self.graph.regions:
            raise ValueError(f"unknown region ID {region_id}")
        return region_id

    @staticmethod
    def _allowed_security_bands(body: JsonObject) -> frozenset[SecurityBand]:
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

    def _reachable_threat_regions(
        self,
        *,
        start_system_id: int,
        security: SecurityPolicy,
        horizon_seconds: int,
        seconds_per_jump: int,
    ) -> tuple[int, ...]:
        """Return a proof-safe pre-threat regional transit envelope."""

        if seconds_per_jump <= 0:
            raise ValueError("seconds_per_jump must be positive")
        # Observed activity/threat avoids can change on refresh, so they must not shrink the next
        # observation envelope. Manual avoids and security-band policy are stable operator choices.
        clean_policy = SecurityPolicy(
            minimum_security=security.minimum_security,
            avoided_system_ids=security.avoided_system_ids,
            allowed_bands=security.allowed_bands,
        )
        regions = self.graph.reachable_region_ids(
            start_system_id,
            clean_policy,
            max_jumps=horizon_seconds // seconds_per_jump,
        )
        if not regions:
            raise ValueError("start system is outside the selected security policy")
        return tuple(sorted(regions))

    def _threat_regions_from_scan_body(self, body: JsonObject) -> tuple[int, ...] | None:
        if body.get("threat_scope_to_plan") is not True:
            return None
        start_system_id = self._resolve_system(body.get("start", ""))
        bands = self._allowed_security_bands(body)
        seconds_per_jump = _nonnegative_int(body.get("seconds_per_jump", 60), "seconds_per_jump")
        return self._reachable_threat_regions(
            start_system_id=start_system_id,
            security=SecurityPolicy(minimum_security=None, allowed_bands=bands),
            horizon_seconds=_duration_seconds(body),
            seconds_per_jump=seconds_per_jump,
        )

    def _constraints(self, body: JsonObject, snapshot: ContractSnapshot) -> PlanningConstraints:
        allowed_bands = self._allowed_security_bands(body)
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
        return_to_start = body.get("return_to_start", True) is True
        raw_finish = body.get("finish_system")
        finish_system_id = (
            None
            if raw_finish is None or str(raw_finish).strip() == ""
            else self._resolve_system(raw_finish)
        )
        seconds_per_jump = int(str(body.get("seconds_per_jump", 60)))
        service_seconds = int(str(body.get("service_seconds", 30)))
        start_system_id = self._resolve_system(body.get("start", ""))
        gank_awareness = body.get("gank_awareness", False) is True
        gank_threshold: int | None = None
        gank_avoids: frozenset[int] = frozenset()
        gank_activity_time: datetime | None = None
        threat_categories: frozenset[ThreatCategory] = frozenset()
        threat_min_events: int | None = None
        threat_avoids: frozenset[int] = frozenset()
        if gank_awareness and body.get("threat_categories") is not None:
            raw_categories = body.get("threat_categories")
            if not isinstance(raw_categories, list):
                raise ValueError("threat_categories must be a list")
            try:
                threat_categories = frozenset(
                    ThreatCategory(str(value)) for value in raw_categories
                )
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
            if snapshot.threat_intel_fetched_at is None:
                raise ValueError(
                    "gate-threat awareness requires zKill intel; enable it and scan or "
                    "refresh first"
                )
            threat_avoids = threat_avoided_systems(
                snapshot.gate_threat_events,
                threat_categories,
                minimum_events=threat_min_events,
                exempt_system_ids=frozenset({start_system_id}),
            )
        elif gank_awareness:
            # Backward-compatible API path for v1 snapshots/clients. The localhost UI uses the
            # zKill category path above.
            gank_threshold = _optional_positive_int(
                body.get("gank_ship_kill_threshold", 10),
                "gank_ship_kill_threshold",
            )
            if gank_threshold is None:
                raise ValueError("gank_ship_kill_threshold is required when gank awareness is on")
            if snapshot.system_kills_fetched_at is None:
                raise ValueError(
                    "gank awareness requires system-kill activity; scan or refresh first"
                )
            # Aggregate ship kills are an activity proxy, not a suicide-gank classifier. The start
            # system is intentionally exempt so a pilot can always depart the declared origin.
            gank_avoids = frozenset(
                item.system_id
                for item in snapshot.system_kill_activity
                if item.ship_kills >= gank_threshold and item.system_id != start_system_id
            )
            gank_activity_time = snapshot.system_kills_fetched_at
        return PlanningConstraints(
            start_system_id=start_system_id,
            cargo_capacity_units=cargo_capacity_to_units(
                _nonnegative_decimal(body.get("cargo_m3", ""), "cargo_m3")
            ),
            collateral_budget_units=isk_to_units(
                parse_human_isk(
                    body.get("collateral_isk", ""),
                    unit=str(body.get("collateral_unit", "auto")),
                )
            ),
            horizon_seconds=_duration_seconds(body),
            snapshot_time=snapshot.fetched_at,
            collateral_mode=mode,
            travel=TravelTimeModel(seconds_per_jump, service_seconds),
            security=SecurityPolicy(
                minimum_security=None,
                avoided_system_ids=frozenset(self._resolve_system(value) for value in avoids),
                allowed_bands=allowed_bands,
                gank_avoided_system_ids=gank_avoids,
                gank_ship_kill_threshold=gank_threshold,
                gank_activity_fetched_at=gank_activity_time,
                threat_avoided_system_ids=threat_avoids,
                threat_categories=threat_categories,
                threat_min_events=threat_min_events,
                threat_intel_fetched_at=(
                    snapshot.threat_intel_fetched_at if threat_categories else None
                ),
                threat_window_seconds=(
                    snapshot.threat_window_seconds if threat_categories else None
                ),
                threat_gate_radius_m=(
                    snapshot.threat_gate_radius_m if threat_categories else None
                ),
                threat_coverage_region_ids=(
                    frozenset(snapshot.threat_coverage_region_ids)
                    if threat_categories
                    else frozenset()
                ),
                threat_incomplete_region_ids=(
                    frozenset(snapshot.threat_incomplete_region_ids)
                    if threat_categories
                    else frozenset()
                ),
            ),
            return_to_start=return_to_start,
            required_system_ids=frozenset(self._resolve_system(value) for value in required),
            finish_system_id=finish_system_id,
            max_simultaneous_contracts=_optional_nonnegative_int(
                body.get("max_simultaneous_contracts"),
                "max_simultaneous_contracts",
            ),
        )

    @staticmethod
    def _solver_config(body: JsonObject) -> SolverConfig:
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

    @staticmethod
    def _max_candidates(body: JsonObject) -> int | None:
        return _optional_positive_int(body.get("max_candidates"), "max_candidates")

    def _require_snapshot(self) -> ContractSnapshot:
        if self.snapshot is None:
            raise ValueError("scan at least one region before ranking or solving")
        return self.snapshot

    def _snapshot_summary(self) -> JsonObject | None:
        if self.snapshot is None:
            return None
        age_seconds = max(
            0,
            int((datetime.now(UTC) - self.snapshot.fetched_at).total_seconds()),
        )
        return {
            "fetched_at": self.snapshot.fetched_at.isoformat(),
            "age_seconds": age_seconds,
            "compatibility_date": self.snapshot.compatibility_date,
            "sde_build_number": self.snapshot.sde_build_number,
            "region_ids": list(self.snapshot.region_ids),
            "region_names": [
                self.graph.regions[region_id].name
                for region_id in self.snapshot.region_ids
                if region_id in self.graph.regions
            ],
            "contracts": len(self.snapshot.contracts),
            "system_kills_fetched_at": (
                self.snapshot.system_kills_fetched_at.isoformat()
                if self.snapshot.system_kills_fetched_at is not None
                else None
            ),
            "system_kill_systems": len(self.snapshot.system_kill_activity),
            "threat_intel_fetched_at": (
                self.snapshot.threat_intel_fetched_at.isoformat()
                if self.snapshot.threat_intel_fetched_at is not None
                else None
            ),
            "threat_window_seconds": self.snapshot.threat_window_seconds,
            "threat_gate_radius_m": self.snapshot.threat_gate_radius_m,
            "threat_coverage_region_ids": list(self.snapshot.threat_coverage_region_ids),
            "threat_incomplete_region_ids": list(self.snapshot.threat_incomplete_region_ids),
            "threat_killmails_seen": self.snapshot.threat_killmails_seen,
            "gate_threat_events": len(self.snapshot.gate_threat_events),
        }

    def _execution_payload(self) -> JsonObject | None:
        if self.execution is None:
            return None
        payload = execution_state_to_dict(self.execution)
        system = self.graph.systems.get(self.execution.current_system_id)
        terminal = (
            self.graph.systems.get(self.execution.terminal_system_id)
            if self.execution.terminal_system_id is not None
            else None
        )
        cargo_units = sum(
            shipment.contract.contract.volume_units
            for shipment in self.execution.active_shipments
            if shipment.picked
        )
        collateral_units = sum(
            shipment.contract.contract.collateral_units
            for shipment in self.execution.active_shipments
        )
        payload.update(
            {
                "current_system_name": system.name if system is not None else None,
                "active_count": len(self.execution.active_shipments),
                "completed_count": len(self.execution.completed_contract_ids),
                "cargo_in_use_m3": str(volume_units_to_decimal(cargo_units)),
                "collateral_locked_isk": str(isk_units_to_decimal(collateral_units)),
                "terminal_system_name": terminal.name if terminal is not None else None,
                "remaining_required_systems": [
                    {
                        "system_id": system_id,
                        "name": self.graph.systems[system_id].name,
                    }
                    for system_id in sorted(self.execution.remaining_required_system_ids)
                    if system_id in self.graph.systems
                ],
                "can_end_safely": not self.execution.active_shipments,
            }
        )
        return payload

    def _scope_payload(self, prepared: PreparedProblem) -> JsonObject:
        scope = prepared.problem.scope
        return {
            "public_couriers_seen": scope.public_couriers_seen,
            "eligible_contracts": scope.eligible_contracts,
            "policy_exclusions": dict(scope.policy_exclusions),
            "safe_reductions": dict(scope.safe_reductions),
            "heuristic_reductions": dict(scope.heuristic_reductions),
            "scope_untruncated": scope.is_untruncated,
        }

    def _decorate_saved_plan(self, payload: JsonObject) -> JsonObject:
        model = payload.get("model")
        if isinstance(model, dict):
            saved_model = cast(JsonObject, model)
            start_id = int(saved_model.get("start_system_id", 0))
            start = self.graph.systems.get(start_id)
            saved_model["start_system_name"] = start.name if start is not None else None
            finish_id = saved_model.get("finish_system_id")
            finish = (
                self.graph.systems.get(int(finish_id)) if finish_id is not None else None
            )
            saved_model["finish_system_name"] = finish.name if finish is not None else None
            for ids_key, systems_key in (
                ("avoided_system_ids", "avoided_systems"),
                ("required_system_ids", "required_systems"),
            ):
                raw_ids = saved_model.get(ids_key)
                if not isinstance(raw_ids, list):
                    continue
                saved_model[systems_key] = [
                    {
                        "id": system_id,
                        "name": self.graph.systems[system_id].name,
                    }
                    for raw_id in raw_ids
                    if (system_id := int(raw_id)) in self.graph.systems
                ]

        route = payload.get("route")
        if not isinstance(route, list):
            return payload
        public = (
            {contract.contract_id: contract for contract in self.snapshot.contracts}
            if self.snapshot is not None
            else {}
        )
        active = (
            {
                shipment.contract.contract.contract_id: shipment.contract.contract
                for shipment in self.execution.active_shipments
            }
            if self.execution is not None
            else {}
        )
        for raw_step in route:
            if not isinstance(raw_step, dict):
                continue
            step = cast(JsonObject, raw_step)
            contract_id = int(step.get("contract_id", 0))
            contract = public.get(contract_id) or active.get(contract_id)
            self._decorate_route_step(step, contract, mandatory=contract_id in active)
        self._decorate_travel_legs(payload)
        return payload

    def _decorate_jump_path(self, payload: JsonObject) -> None:
        raw_path = payload.get("jump_path")
        if not isinstance(raw_path, list):
            return
        payload["jump_count"] = max(0, len(raw_path) - 1)
        path_systems: list[JsonObject] = []
        for raw_system_id in raw_path:
            system_id = int(raw_system_id)
            path_system = self.graph.systems.get(system_id)
            path_systems.append(
                {
                    "system_id": system_id,
                    "name": path_system.name if path_system is not None else str(system_id),
                    "security_status": (
                        path_system.security_status if path_system is not None else None
                    ),
                    "security_band": (
                        security_band(path_system.security_status).value
                        if path_system is not None
                        else None
                    ),
                }
            )
        payload["jump_path_systems"] = path_systems

    def _decorate_travel_legs(self, payload: JsonObject) -> None:
        raw_legs = payload.get("travel_legs")
        if not isinstance(raw_legs, list):
            return
        for raw_leg in raw_legs:
            if not isinstance(raw_leg, dict):
                continue
            leg = cast(JsonObject, raw_leg)
            from_system = self.graph.systems.get(int(leg.get("from_system_id", 0)))
            to_system = self.graph.systems.get(int(leg.get("to_system_id", 0)))
            leg["from_system_name"] = from_system.name if from_system is not None else None
            leg["to_system_name"] = to_system.name if to_system is not None else None
            self._decorate_jump_path(leg)

    def _decorate_route_step(
        self,
        step: JsonObject,
        contract: PublicCourierContract | None,
        *,
        mandatory: bool,
    ) -> None:
        """Add human-readable pilot guidance without changing canonical plan semantics."""

        system = self.graph.systems.get(int(step.get("system_id", 0)))
        step["system_name"] = system.name if system is not None else None
        self._decorate_jump_path(step)
        if contract is not None:
            step["title"] = contract.title
            step["reward_isk"] = str(isk_units_to_decimal(contract.reward_units))
            step["volume_m3"] = str(volume_units_to_decimal(contract.volume_units))
            step["mandatory"] = mandatory

    def _plan_payload(self, prepared: PreparedProblem, result: SolveResult) -> JsonObject:
        payload = solve_result_to_dict(result, prepared.problem)
        optional = {
            item.contract.contract_id: item.contract for item in prepared.problem.contracts
        }
        active = {
            item.contract.contract.contract_id: item.contract.contract
            for item in prepared.problem.active_shipments
        }
        raw_route = cast(list[Any], payload["route"])
        for raw_step in raw_route:
            step = cast(JsonObject, raw_step)
            contract_id = int(step["contract_id"])
            contract = optional.get(contract_id) or active.get(contract_id)
            self._decorate_route_step(step, contract, mandatory=contract_id in active)
        self._decorate_travel_legs(payload)
        return payload

    def _store_plan(self, prepared: PreparedProblem, result: SolveResult) -> JsonObject:
        write_solve_result(self.plan_path, result, prepared.problem)
        self.prepared = prepared
        self.result = result
        self.plan_payload = self._plan_payload(prepared, result)
        return self.plan_payload

    def status(self) -> JsonObject:
        return {
            "app_version": __version__,
            "sde": {
                "build_number": self.graph.metadata.build_number,
                "release_date": self.graph.metadata.release_date,
                "systems": len(self.graph.systems),
                "regions": len(self.graph.regions),
                "empire_regions": len(self.graph.empire_region_ids()),
                "npc_stations": len(self.graph.station_systems),
            },
            "snapshot": self._snapshot_summary(),
            "plan": self.plan_payload,
            "execution": self._execution_payload(),
            "artifacts": {
                "snapshot": self.snapshot_path.exists(),
                "plan": self.plan_path.exists(),
                "execution": self.execution_path.exists(),
            },
        }

    def region_matches(self, query: str) -> JsonObject:
        needle = query.casefold().strip()
        matches = [
            {"id": region.region_id, "name": region.name}
            for region in sorted(self.graph.regions.values(), key=lambda item: item.name)
            if not needle or needle in region.name.casefold()
        ][:50]
        return {"items": matches}

    def system_matches(self, query: str) -> JsonObject:
        needle = query.casefold().strip()
        if len(needle) < 2:
            return {"items": []}
        matches = [
            {
                "id": system.system_id,
                "name": system.name,
                "security_status": system.security_status,
            }
            for system in sorted(self.graph.systems.values(), key=lambda item: item.name)
            if needle in system.name.casefold()
        ][:30]
        return {"items": matches}

    def scan(self, body: JsonObject) -> JsonObject:
        region_scope = str(body.get("region_scope", "selected"))
        if region_scope == "all":
            region_ids = tuple(sorted(self.graph.regions))
        elif region_scope == "security":
            region_ids = tuple(
                sorted(
                    self.graph.region_ids_for_security_bands(
                        self._allowed_security_bands(body)
                    )
                )
            )
        elif region_scope == "empire":
            compatible = self.graph.region_ids_for_security_bands(
                self._allowed_security_bands(body)
            )
            region_ids = tuple(sorted(self.graph.empire_region_ids() & compatible))
            if not region_ids:
                raise ValueError(
                    "NPC Empire scope contains no regions in the selected security bands"
                )
        elif region_scope == "selected":
            raw_regions = body.get("regions")
            if not isinstance(raw_regions, list) or not raw_regions:
                raise ValueError("regions must contain at least one region name or ID")
            region_ids = tuple(self._resolve_region(value) for value in raw_regions)
        else:
            raise ValueError("region_scope must be selected, security, empire, or all")
        include_threat = body.get("include_threat_intel", False) is True
        window_hours = _nonnegative_int(
            body.get("threat_window_hours", DEFAULT_THREAT_WINDOW_SECONDS // 3_600),
            "threat_window_hours",
        )
        if include_threat and (window_hours <= 0 or window_hours > 168):
            raise ValueError("threat_window_hours must be between 1 and 168")
        radius_km = _nonnegative_int(
            body.get("threat_gate_radius_km", 250),
            "threat_gate_radius_km",
        )
        snapshot = self.service.scan(
            region_ids,
            include_threat_intel=include_threat,
            threat_window_seconds=window_hours * 3_600,
            threat_gate_radius_m=radius_km * 1_000,
            threat_region_ids=(
                self._threat_regions_from_scan_body(body) if include_threat else None
            ),
        )
        write_snapshot(self.snapshot_path, snapshot)
        self.snapshot = snapshot
        self.prepared = None
        self.result = None
        self.plan_payload = None
        return {"snapshot": self._snapshot_summary()}

    def rank(self, body: JsonObject) -> JsonObject:
        snapshot = self._require_snapshot()
        constraints = self._constraints(body, snapshot)
        prepared = prepare_problem(
            snapshot,
            self.graph,
            constraints,
            max_candidates=self._max_candidates(body),
        )
        scores = rank_single_contracts(prepared)
        items: list[JsonObject] = []
        for score in scores[:50]:
            contract = score.contract.contract
            origin = self.graph.systems[score.contract.origin_system_id]
            destination = self.graph.systems[score.contract.destination_system_id]
            items.append(
                {
                    "contract_id": contract.contract_id,
                    "title": contract.title,
                    "origin": origin.name,
                    "destination": destination.name,
                    "reward_isk": str(isk_units_to_decimal(contract.reward_units)),
                    "volume_m3": str(volume_units_to_decimal(contract.volume_units)),
                    "collateral_isk": str(isk_units_to_decimal(contract.collateral_units)),
                    "solo_jumps": score.solo_jumps,
                    "solo_seconds": score.solo_seconds,
                    "reward_per_hour_isk": score.reward_per_hour_isk,
                    "reward_per_jump_isk": score.reward_per_jump_isk,
                    "reward_to_collateral": score.reward_to_collateral,
                }
            )
        return {"scope": self._scope_payload(prepared), "items": items}

    def solve(self, body: JsonObject) -> JsonObject:
        snapshot = self._require_snapshot()
        if self.execution is not None:
            raise ValueError(
                "an execution session already exists; use Replan or reset the session first"
            )
        constraints = self._constraints(body, snapshot)
        prepared = prepare_problem(
            snapshot,
            self.graph,
            constraints,
            max_candidates=self._max_candidates(body),
        )
        result = solve_exact(prepared, self.graph, config=self._solver_config(body))
        return {"plan": self._store_plan(prepared, result)}

    def start_execution(self, body: JsonObject) -> JsonObject:
        if self.prepared is None or self.result is None:
            raise ValueError("solve a route in this server session before starting execution")
        if not self.result.certificate.feasibility_verified:
            raise ValueError(
                "the current solver result has no independently verified feasible route"
            )
        constraints = self.prepared.problem.constraints
        if (
            constraints.collateral_mode is CollateralMode.LOCKED
            and self.result.selected_contract_ids
            and body.get("confirm_locked_acceptance") is not True
        ):
            raise ValueError(
                "locked mode requires confirmation that every selected contract was accepted in EVE"
            )
        state = initial_execution_state(
            constraints,
            self.prepared.problem.contracts,
            self.prepared.problem.active_shipments,
            self.result,
            completed_contract_ids=(
                self.execution.completed_contract_ids if self.execution is not None else ()
            ),
        )
        write_execution_state(self.execution_path, state)
        self.execution = state
        # A solved route may only be armed once. Subsequent state changes must flow through
        # explicit pickup/delivery transitions and a fresh replan, never by replaying stale state.
        self.prepared = None
        self.result = None
        return {"execution": self._execution_payload()}

    def record_action(self, body: JsonObject) -> JsonObject:
        if self.execution is None:
            raise ValueError("start an execution session before recording an action")
        action = str(body.get("action", ""))
        raw_at = str(body.get("at", "now"))
        at = datetime.now(UTC) if raw_at.casefold() == "now" else parse_esi_datetime(raw_at)
        if action == "route_system":
            try:
                system_id = int(str(body.get("system_id", "")))
            except ValueError as error:
                raise ValueError("system_id must be an integer") from error
            state = record_route_system(self.execution, system_id, at)
        elif action in {"pickup", "delivery"}:
            try:
                contract_id = int(str(body.get("contract_id", "")))
            except ValueError as error:
                raise ValueError("contract_id must be an integer") from error
            if action == "pickup":
                snapshot = self._require_snapshot()
                state = record_pickup(
                    self.execution,
                    snapshot,
                    self.graph,
                    contract_id,
                    at,
                )
            else:
                state = record_delivery(self.execution, contract_id, at)
        else:
            raise ValueError("action must be 'pickup', 'delivery', or 'route_system'")
        write_execution_state(self.execution_path, state)
        self.execution = state
        self.prepared = None
        self.result = None
        return {"execution": self._execution_payload()}

    def replan(self, body: JsonObject) -> JsonObject:
        if self.execution is None:
            raise ValueError("start an execution session before replanning")
        snapshot = self._require_snapshot()
        if body.get("refresh") is True:
            threat_enabled = bool(self.execution.security.threat_categories)
            remaining_seconds = max(
                0,
                int(
                    (self.execution.session_deadline - self.execution.current_time).total_seconds()
                ),
            )
            threat_regions = (
                self._reachable_threat_regions(
                    start_system_id=self.execution.current_system_id,
                    security=self.execution.security,
                    horizon_seconds=remaining_seconds,
                    seconds_per_jump=self.execution.travel.seconds_per_jump,
                )
                if threat_enabled
                else None
            )
            snapshot = self.service.scan(
                snapshot.region_ids,
                include_threat_intel=threat_enabled,
                threat_window_seconds=(
                    self.execution.security.threat_window_seconds
                    or DEFAULT_THREAT_WINDOW_SECONDS
                ),
                threat_gate_radius_m=(
                    self.execution.security.threat_gate_radius_m or 250_000
                ),
                threat_region_ids=threat_regions,
            )
            write_snapshot(self.snapshot_path, snapshot)
            self.snapshot = snapshot
        prepared, result = self.service.replan(
            snapshot,
            self.execution,
            max_candidates=self._max_candidates(body),
            solver_config=self._solver_config(body),
        )
        return {
            "snapshot": self._snapshot_summary(),
            "plan": self._store_plan(prepared, result),
            "execution": self._execution_payload(),
        }

    def reset_execution(self) -> JsonObject:
        self.execution = None
        self.execution_path.unlink(missing_ok=True)
        return {"execution": None}

    def asset(self, name: str) -> tuple[bytes, str]:
        if name not in {"index.html", "styles.css", "app.js"}:
            raise FileNotFoundError(name)
        resource = files("eve_courier_optimizer").joinpath("web", name)
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        return resource.read_bytes(), content_type


def _handler_type(app: LocalWebApplication) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = f"EveCourierLocal/{__version__}"

        def log_message(self, format: str, *args: object) -> None:
            print(f"web: {format % args}", file=sys.stderr)

        def _local_request_allowed(self) -> bool:
            host = self.headers.get("Host", "").split(":", maxsplit=1)[0].casefold()
            if host not in _LOCAL_HOSTS:
                return False
            origin = self.headers.get("Origin")
            if origin:
                origin_host = urlsplit(origin).hostname
                if origin_host is None or origin_host.casefold() not in _LOCAL_HOSTS:
                    return False
            return True

        def _common_headers(self, *, cache: str = "no-store") -> None:
            self.send_header("Cache-Control", cache)
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("X-Frame-Options", "SAMEORIGIN")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self' data:; frame-ancestors 'self'",
            )

        def _send_json(self, payload: JsonObject, status: HTTPStatus = HTTPStatus.OK) -> None:
            encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self._common_headers()
            self.end_headers()
            self.wfile.write(encoded)

        def _send_error_json(self, status: HTTPStatus, message: str) -> None:
            self._send_json({"error": message}, status)

        def _body(self) -> JsonObject:
            content_type = self.headers.get("Content-Type", "")
            if not content_type.startswith("application/json"):
                raise ValueError("POST requests must use application/json")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as error:
                raise ValueError("invalid Content-Length") from error
            if length <= 0 or length > _MAX_REQUEST_BYTES:
                raise ValueError("request body must be between 1 byte and 1 MiB")
            decoded = json.loads(self.rfile.read(length))
            if not isinstance(decoded, dict):
                raise ValueError("JSON request body must be an object")
            return cast(JsonObject, decoded)

        def _download(self, filename: str) -> None:
            if filename not in _DOWNLOAD_FILES:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            path = app.workspace / filename
            if not path.exists():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            data = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(data)))
            self._common_headers()
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
            if not self._local_request_allowed():
                self._send_error_json(HTTPStatus.FORBIDDEN, "local requests only")
                return
            parsed = urlsplit(self.path)
            if parsed.path == "/api/status":
                self._send_json(app.status())
                return
            if parsed.path == "/api/regions":
                query = parse_qs(parsed.query).get("q", [""])[0]
                self._send_json(app.region_matches(query))
                return
            if parsed.path == "/api/systems":
                query = parse_qs(parsed.query).get("q", [""])[0]
                self._send_json(app.system_matches(query))
                return
            if parsed.path.startswith("/download/"):
                self._download(parsed.path.removeprefix("/download/"))
                return
            asset_name = "index.html" if parsed.path in {"/", "/index.html"} else parsed.path[1:]
            try:
                data, content_type = app.asset(asset_name)
            except FileNotFoundError:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self._common_headers(cache="no-cache")
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
            if not self._local_request_allowed():
                self._send_error_json(HTTPStatus.FORBIDDEN, "local requests only")
                return
            try:
                body = self._body()
                routes = {
                    "/api/scan": app.scan,
                    "/api/rank": app.rank,
                    "/api/solve": app.solve,
                    "/api/execution/start": app.start_execution,
                    "/api/action": app.record_action,
                    "/api/replan": app.replan,
                }
                if self.path == "/api/execution/reset":
                    payload = app.reset_execution()
                else:
                    action = routes.get(self.path)
                    if action is None:
                        self._send_error_json(HTTPStatus.NOT_FOUND, "unknown API route")
                        return
                    payload = action(body)
                self._send_json(payload)
            except json.JSONDecodeError:
                self._send_error_json(HTTPStatus.BAD_REQUEST, "request body is not valid JSON")
            except (OSError, RuntimeError, ValueError) as error:
                self._send_error_json(HTTPStatus.BAD_REQUEST, str(error))

    return Handler


def create_http_server(app: LocalWebApplication, *, port: int = 8765) -> HTTPServer:
    """Create, but do not run, a loopback-only server. ``port=0`` is useful in tests."""

    if port < 0 or port > 65_535:
        raise ValueError("port must be between 0 and 65535")
    return HTTPServer(("127.0.0.1", port), _handler_type(app))


def run_local_web_ui(
    graph: UniverseGraph,
    *,
    port: int = 8765,
    workspace: Path | None = None,
    open_browser: bool = True,
) -> int:
    """Run the local control deck until interrupted."""

    if port <= 0 or port > 65_535:
        raise ValueError("port must be between 1 and 65535")
    root = workspace or default_web_workspace()
    client = EsiClient(cache=EsiResponseCache(root / "esi-cache.sqlite3"))
    zkill = ZkillClient(cache=EsiResponseCache(root / "zkill-cache.sqlite3"))
    app = LocalWebApplication(graph, client, root, zkill)
    server = create_http_server(app, port=port)
    url = f"http://127.0.0.1:{port}/"
    print(f"local web UI: {url}")
    print(f"session files: {root}")
    print("Press Ctrl+C to stop.")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
