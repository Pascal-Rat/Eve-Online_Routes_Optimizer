"""Durable local planning session: snapshots, proposed routes, and live obligations."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from eve_courier_optimizer import __version__
from eve_courier_optimizer.application.courier_trip import CourierTrip
from eve_courier_optimizer.application.plan_file import solve_result_to_dict, write_solve_result
from eve_courier_optimizer.application.planner import CourierPlanner, RoutePlan
from eve_courier_optimizer.application.trip_file import read_trip, write_trip
from eve_courier_optimizer.domain import (
    CollateralMode,
    ContractSnapshot,
    parse_esi_datetime,
)
from eve_courier_optimizer.eve.esi import EsiClient, utc_now
from eve_courier_optimizer.eve.snapshot_file import read_snapshot, write_snapshot
from eve_courier_optimizer.eve.zkill import ZkillClient
from eve_courier_optimizer.routing.route_problem import RouteProblem
from eve_courier_optimizer.routing.universe import UniverseGraph
from eve_courier_optimizer.web import requests, responses

JsonObject = dict[str, Any]


def default_workspace_path() -> Path:
    """Return the durable, credential-free working directory used by the local UI."""

    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "EveCourierRouteOptimizer"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "EveCourierRouteOptimizer"
    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg_data_home) if xdg_data_home else Path.home() / ".local" / "share"
    return base / "eve-courier-route-optimizer"


class PlanningWorkspace:
    """The local UI's saved snapshot, proposed plan, and active courier trip."""

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
        self.trip_path = workspace / "execution.json"
        self.snapshot: ContractSnapshot | None = None
        self.plan: RoutePlan | None = None
        self.trip: CourierTrip | None = None
        self.plan_payload: JsonObject | None = None
        self._restore_saved_files()

    def _restore_saved_files(self) -> None:
        if self.snapshot_path.exists():
            snapshot = read_snapshot(self.snapshot_path)
            if snapshot.sde_build_number == self.graph.metadata.build_number:
                self.snapshot = snapshot
        if self.trip_path.exists():
            self.trip = read_trip(self.trip_path)
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
                self.plan_payload = responses.decorate_plan(
                    cast(JsonObject, raw), self.graph, self.snapshot, self.trip
                )

    def discard_plan(self) -> None:
        self.plan_path.unlink(missing_ok=True)
        self.plan = None
        self.plan_payload = None

    def _require_snapshot(self) -> ContractSnapshot:
        if self.snapshot is None:
            raise ValueError("scan at least one region before ranking or solving")
        return self.snapshot

    def save_plan(self, plan: RoutePlan) -> JsonObject:
        problem, result = plan.problem, plan.result
        write_solve_result(self.plan_path, result, problem)
        self.plan = plan
        self.plan_payload = responses.decorate_plan(
            solve_result_to_dict(result, problem),
            self.graph,
            self.snapshot,
            self.trip,
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
            "snapshot": responses.snapshot_summary(self.snapshot, self.graph),
            "plan": self.plan_payload,
            "execution": responses.trip_response(self.trip, self.graph, self.clock()),
            "plan_armable": self.plan is not None
            and self.plan.result.certificate.feasibility_verified,
            "artifacts": {
                "snapshot": self.snapshot_path.exists(),
                "plan": self.plan_path.exists(),
                "execution": self.trip_path.exists(),
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
        if self.trip is not None:
            raise ValueError("an execution session already exists; use Replan")
        request = requests.ScanRequest.from_json(body, self.graph)
        snapshot = self.planner.scan(
            request.region_ids,
            include_threat_intel=request.include_threat_intel,
            threat_window_seconds=request.threat_window_seconds,
            threat_gate_radius_m=request.threat_gate_radius_m,
            threat_region_ids=request.threat_region_ids,
        )
        self.discard_plan()
        write_snapshot(self.snapshot_path, snapshot)
        self.snapshot = snapshot
        return {"snapshot": responses.snapshot_summary(self.snapshot, self.graph)}

    def rank(self, body: dict[str, object]) -> JsonObject:
        if self.trip is not None:
            raise ValueError("an execution session already exists; use Replan")
        snapshot = self._require_snapshot()
        request = requests.PlanRequest.from_json(body, snapshot, self.graph, self.clock())
        problem = RouteProblem.from_snapshot(
            snapshot,
            self.graph,
            request.constraints,
            max_candidates=request.max_candidates,
        )
        return responses.ranked_contracts(problem, self.graph)

    def solve(self, body: dict[str, object]) -> JsonObject:
        snapshot = self._require_snapshot()
        if self.trip is not None:
            raise ValueError(
                "an execution session already exists; use Replan or reset the session first"
            )
        request = requests.PlanRequest.from_json(body, snapshot, self.graph, self.clock())
        plan = self.planner.solve(
            snapshot,
            request.constraints,
            max_candidates=request.max_candidates,
            solver_config=requests.read_solver_config(body),
        )
        return {"plan": self.save_plan(plan)}

    def start_execution(self, body: dict[str, object]) -> JsonObject:
        if self.plan is None:
            raise ValueError("solve a route in this server session before starting execution")
        if not self.plan.result.certificate.feasibility_verified:
            raise ValueError(
                "the current solver result has no independently verified feasible route"
            )
        constraints = self.plan.problem.constraints
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
            previous=self.trip,
        )
        write_trip(self.trip_path, state)
        self.trip = state
        # A solved route may only be armed once. Subsequent state changes must flow through
        # explicit pickup/delivery transitions and a fresh replan, never by replaying stale state.
        self.plan = None
        return {"execution": responses.trip_response(self.trip, self.graph, self.clock())}

    def record_action(self, body: dict[str, object]) -> JsonObject:
        if self.trip is None:
            raise ValueError("start an execution session before recording an action")
        action = str(body.get("action", ""))
        raw_at = str(body.get("at", "now"))
        at = self.clock() if raw_at.casefold() == "now" else parse_esi_datetime(raw_at)
        if action == "route_system":
            try:
                system_id = int(str(body.get("system_id", "")))
            except ValueError as error:
                raise ValueError("system_id must be an integer") from error
            state = self.trip.reach_system(system_id, at)
        elif action in {"pickup", "delivery"}:
            try:
                contract_id = int(str(body.get("contract_id", "")))
            except ValueError as error:
                raise ValueError("contract_id must be an integer") from error
            if action == "pickup":
                state = self.trip.pick_up(self.snapshot, self.graph, contract_id, at)
            else:
                state = self.trip.deliver(contract_id, at)
        else:
            raise ValueError("action must be 'pickup', 'delivery', or 'route_system'")
        write_trip(self.trip_path, state)
        self.trip = state
        self.plan = None
        return {"execution": responses.trip_response(self.trip, self.graph, self.clock())}

    def replan(self, body: dict[str, object]) -> JsonObject:
        if self.trip is None:
            raise ValueError("start an execution session before replanning")
        snapshot = self._require_snapshot()
        request = requests.ReplanRequest.from_json(body)
        if request.refresh_snapshot:
            snapshot = self.planner.refresh_for_trip(snapshot, self.trip, at=self.clock())
            write_snapshot(self.snapshot_path, snapshot)
            self.snapshot = snapshot
        plan = self.planner.replan(
            snapshot,
            self.trip,
            max_candidates=request.max_candidates,
            solver_config=request.solver_config,
            at=self.clock(),
        )
        return {
            "snapshot": responses.snapshot_summary(self.snapshot, self.graph),
            "plan": self.save_plan(plan),
            "execution": responses.trip_response(self.trip, self.graph, self.clock()),
        }

    def reset_execution(self) -> JsonObject:
        self.trip_path.unlink(missing_ok=True)
        self.trip = None
        self.plan = None
        return {"execution": None}

    def extend_horizon(self, body: dict[str, object]) -> JsonObject:
        if self.trip is None:
            raise ValueError("start an execution session before extending its horizon")
        minutes = requests.FormFields(body).optional_integer("minutes", minimum=1)
        if minutes is None:
            raise ValueError("minutes is required")
        state = self.trip.extend_horizon(
            additional_seconds=minutes * 60, at=max(self.clock(), self.trip.current_time)
        )
        write_trip(self.trip_path, state)
        self.trip = state
        self.discard_plan()
        return {
            "execution": responses.trip_response(self.trip, self.graph, self.clock()),
            "plan": None,
        }
