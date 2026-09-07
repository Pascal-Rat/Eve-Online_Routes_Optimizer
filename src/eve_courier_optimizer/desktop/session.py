"""Durable local planning session: snapshots, proposed routes, and live obligations."""

from __future__ import annotations

import json
import math
import os
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from eve_courier_optimizer import __version__
from eve_courier_optimizer.application.execution import (
    ExecutionState,
    extend_execution_horizon,
    read_execution_state,
    record_delivery,
    record_pickup,
    record_route_system,
    write_execution_state,
)
from eve_courier_optimizer.application.plan_output import solve_result_to_dict, write_solve_result
from eve_courier_optimizer.application.planner import CourierPlanner, RoutePlan
from eve_courier_optimizer.domain import (
    CollateralMode,
    ContractSnapshot,
    isk_units_to_decimal,
    parse_esi_datetime,
    volume_units_to_decimal,
)
from eve_courier_optimizer.eve.esi import EsiClient, utc_now
from eve_courier_optimizer.eve.snapshot import read_snapshot, write_snapshot
from eve_courier_optimizer.eve.zkill import DEFAULT_THREAT_WINDOW_SECONDS, ZkillClient
from eve_courier_optimizer.jsonio import json_bool
from eve_courier_optimizer.routing.policy import reachable_threat_regions
from eve_courier_optimizer.routing.preparation import rank_single_contracts
from eve_courier_optimizer.routing.universe import UniverseGraph

from . import options, presentation

JsonObject = dict[str, Any]


def default_web_workspace() -> Path:
    """Return the durable, credential-free working directory used by the local UI."""

    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "EveCourierRouteOptimizer"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "EveCourierRouteOptimizer"
    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg_data_home) if xdg_data_home else Path.home() / ".local" / "share"
    return base / "eve-courier-route-optimizer"


class PlanningSession:
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
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.graph = graph
        self.esi = esi
        self.zkill = zkill
        self.planner = CourierPlanner(graph, esi, zkill)
        self.clock = clock
        self.workspace = workspace
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.snapshot_path = workspace / "snapshot.json"
        self.plan_path = workspace / "plan.json"
        self.execution_path = workspace / "execution.json"
        self.snapshot: ContractSnapshot | None = None
        self.plan: RoutePlan | None = None
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
            if (
                isinstance(raw, dict)
                and raw.get("schema_version") == 3
                and isinstance(raw.get("scope"), dict)
                and self.snapshot is not None
                and raw.get("scope", {}).get("snapshot_fetched_at")
                == self.snapshot.fetched_at.isoformat()
                and raw.get("scope", {}).get("sde_build_number") == self.graph.metadata.build_number
            ):
                self.plan_payload = presentation.decorate_plan(
                    cast(JsonObject, raw), self.graph, self.snapshot, self.execution
                )

    def _invalidate_plan(self) -> None:
        self.plan_path.unlink(missing_ok=True)
        self.plan = None
        self.plan_payload = None

    def _require_snapshot(self) -> ContractSnapshot:
        if self.snapshot is None:
            raise ValueError("scan at least one region before ranking or solving")
        return self.snapshot

    def store_plan(self, plan: RoutePlan) -> JsonObject:
        prepared, result = plan.prepared, plan.result
        write_solve_result(self.plan_path, result, prepared.problem)
        self.plan = plan
        self.plan_payload = presentation.decorate_plan(
            solve_result_to_dict(result, prepared.problem),
            self.graph,
            self.snapshot,
            self.execution,
        )
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
            "snapshot": presentation.snapshot_summary(self.snapshot, self.graph),
            "plan": self.plan_payload,
            "execution": presentation.execution_payload(self.execution, self.graph, self.clock()),
            "plan_armable": self.plan is not None
            and self.plan.result.certificate.feasibility_verified,
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

    def scan(self, body: dict[str, object]) -> JsonObject:
        if self.execution is not None:
            raise ValueError("an execution session already exists; use Replan")
        region_scope = str(body.get("region_scope", "selected"))
        if region_scope == "all":
            region_ids = tuple(sorted(self.graph.regions))
        elif region_scope == "security":
            region_ids = tuple(
                sorted(
                    self.graph.region_ids_for_security_bands(options.allowed_security_bands(body))
                )
            )
        elif region_scope == "empire":
            compatible = self.graph.region_ids_for_security_bands(
                options.allowed_security_bands(body)
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
            region_ids = tuple(self.graph.resolve_region(value) for value in raw_regions)
        else:
            raise ValueError("region_scope must be selected, security, empire, or all")
        include_threat = json_bool(body.get("include_threat_intel", False), "include_threat_intel")
        window_hours = options._nonnegative_int(
            body.get("threat_window_hours", DEFAULT_THREAT_WINDOW_SECONDS // 3_600),
            "threat_window_hours",
        )
        if include_threat and (window_hours <= 0 or window_hours > 168):
            raise ValueError("threat_window_hours must be between 1 and 168")
        radius_km = options._nonnegative_int(
            body.get("threat_gate_radius_km", 250),
            "threat_gate_radius_km",
        )
        snapshot = self.planner.scan(
            region_ids,
            include_threat_intel=include_threat,
            threat_window_seconds=window_hours * 3_600,
            threat_gate_radius_m=radius_km * 1_000,
            threat_region_ids=(
                options.threat_regions_from_scan_body(body, self.graph) if include_threat else None
            ),
        )
        self._invalidate_plan()
        write_snapshot(self.snapshot_path, snapshot)
        self.snapshot = snapshot
        return {"snapshot": presentation.snapshot_summary(self.snapshot, self.graph)}

    def rank(self, body: dict[str, object]) -> JsonObject:
        if self.execution is not None:
            raise ValueError("an execution session already exists; use Replan")
        snapshot = self._require_snapshot()
        constraints = options.constraints(body, snapshot, self.graph, self.clock())
        prepared = self.planner.prepare(
            snapshot,
            constraints,
            max_candidates=options.max_candidates(body),
        )
        scores = rank_single_contracts(prepared)
        items: list[JsonObject] = []
        for score in scores[:50]:
            contract = score.contract
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
                    "reward_per_hour_isk": (
                        score.reward_per_hour_isk
                        if math.isfinite(score.reward_per_hour_isk)
                        else None
                    ),
                    "reward_per_jump_isk": (
                        score.reward_per_jump_isk
                        if math.isfinite(score.reward_per_jump_isk)
                        else None
                    ),
                    "reward_to_collateral": (
                        score.reward_to_collateral
                        if math.isfinite(score.reward_to_collateral)
                        else None
                    ),
                }
            )
        return {"scope": presentation.scope_payload(prepared), "items": items}

    def solve(self, body: dict[str, object]) -> JsonObject:
        snapshot = self._require_snapshot()
        if self.execution is not None:
            raise ValueError(
                "an execution session already exists; use Replan or reset the session first"
            )
        constraints = options.constraints(body, snapshot, self.graph, self.clock())
        plan = self.planner.solve(
            snapshot,
            constraints,
            max_candidates=options.max_candidates(body),
            solver_config=options.solver_config(body),
        )
        return {"plan": self.store_plan(plan)}

    def start_execution(self, body: dict[str, object]) -> JsonObject:
        if self.plan is None:
            raise ValueError("solve a route in this server session before starting execution")
        if not self.plan.result.certificate.feasibility_verified:
            raise ValueError(
                "the current solver result has no independently verified feasible route"
            )
        constraints = self.plan.prepared.problem.constraints
        if (
            constraints.collateral_mode is CollateralMode.LOCKED
            and self.plan.result.selected_contract_ids
            and body.get("confirm_locked_acceptance") is not True
        ):
            raise ValueError(
                "locked mode requires confirmation that every selected contract was accepted in EVE"
            )
        state = self.planner.arm(
            self.plan,
            at=max(self.clock(), constraints.snapshot_time),
            previous=self.execution,
        )
        write_execution_state(self.execution_path, state)
        self.execution = state
        # A solved route may only be armed once. Subsequent state changes must flow through
        # explicit pickup/delivery transitions and a fresh replan, never by replaying stale state.
        self.plan = None
        return {
            "execution": presentation.execution_payload(self.execution, self.graph, self.clock())
        }

    def record_action(self, body: dict[str, object]) -> JsonObject:
        if self.execution is None:
            raise ValueError("start an execution session before recording an action")
        action = str(body.get("action", ""))
        raw_at = str(body.get("at", "now"))
        at = self.clock() if raw_at.casefold() == "now" else parse_esi_datetime(raw_at)
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
                state = record_pickup(
                    self.execution,
                    self.snapshot,
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
        self.plan = None
        return {
            "execution": presentation.execution_payload(self.execution, self.graph, self.clock())
        }

    def replan(self, body: dict[str, object]) -> JsonObject:
        if self.execution is None:
            raise ValueError("start an execution session before replanning")
        snapshot = self._require_snapshot()
        if json_bool(body.get("refresh", False), "refresh"):
            threat_enabled = bool(self.execution.security.threat_categories)
            remaining_seconds = max(
                0,
                int(
                    (
                        self.execution.session_deadline
                        - max(
                            self.clock(),
                            self.execution.current_time,
                        )
                    ).total_seconds()
                ),
            )
            threat_regions = (
                reachable_threat_regions(
                    self.graph,
                    start_system_id=self.execution.current_system_id,
                    security=self.execution.security,
                    horizon_seconds=remaining_seconds,
                    seconds_per_jump=self.execution.travel.seconds_per_jump,
                )
                if threat_enabled
                else None
            )
            snapshot = self.planner.scan(
                snapshot.region_ids,
                include_threat_intel=threat_enabled,
                threat_window_seconds=(
                    self.execution.security.threat_window_seconds or DEFAULT_THREAT_WINDOW_SECONDS
                ),
                threat_gate_radius_m=(
                    self.execution.security.threat_gate_radius_m
                    if self.execution.security.threat_gate_radius_m is not None
                    else 250_000
                ),
                threat_region_ids=threat_regions,
            )
            write_snapshot(self.snapshot_path, snapshot)
            self.snapshot = snapshot
        plan = self.planner.replan(
            snapshot,
            self.execution,
            max_candidates=options.max_candidates(body),
            solver_config=options.solver_config(body),
            at=self.clock(),
        )
        return {
            "snapshot": presentation.snapshot_summary(self.snapshot, self.graph),
            "plan": self.store_plan(plan),
            "execution": presentation.execution_payload(self.execution, self.graph, self.clock()),
        }

    def reset_execution(self) -> JsonObject:
        self.execution_path.unlink(missing_ok=True)
        self.execution = None
        self.plan = None
        return {"execution": None}

    def extend_horizon(self, body: dict[str, object]) -> JsonObject:
        if self.execution is None:
            raise ValueError("start an execution session before extending its horizon")
        minutes = options._optional_positive_int(body.get("minutes"), "minutes")
        if minutes is None:
            raise ValueError("minutes is required")
        state = extend_execution_horizon(
            self.execution,
            additional_seconds=minutes * 60,
            at=max(self.clock(), self.execution.current_time),
        )
        write_execution_state(self.execution_path, state)
        self.execution = state
        self._invalidate_plan()
        return {
            "execution": presentation.execution_payload(self.execution, self.graph, self.clock()),
            "plan": None,
        }
