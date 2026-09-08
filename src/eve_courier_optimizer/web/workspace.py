"""Durable local planning session: snapshots, proposed routes, and live obligations."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, overload
from uuid import uuid4

from eve_courier_optimizer import __version__
from eve_courier_optimizer.application.courier_trip import CourierTrip
from eve_courier_optimizer.application.file_lock import WorkspaceConflict
from eve_courier_optimizer.application.plan_contract import PlanPayload
from eve_courier_optimizer.application.plan_file import solve_result_to_dict
from eve_courier_optimizer.application.planner import CourierPlanner, RoutePlan
from eve_courier_optimizer.application.trip_file import trip_to_dict
from eve_courier_optimizer.domain import (
    CollateralMode,
    ContractSnapshot,
    parse_esi_datetime,
)
from eve_courier_optimizer.eve.esi import EsiClient, utc_now
from eve_courier_optimizer.eve.snapshot_file import snapshot_to_dict
from eve_courier_optimizer.eve.zkill import ZkillClient
from eve_courier_optimizer.routing.universe import UniverseGraph
from eve_courier_optimizer.web import requests, responses
from eve_courier_optimizer.web.contracts import (
    ExecutionResponse,
    OperationResponse,
    PlanResponse,
    RankResponse,
    ScanResponse,
    StatusResponse,
    Suggestion,
    Suggestions,
    TransitionIdentity,
)
from eve_courier_optimizer.web.operations import (
    Operation,
    OperationResult,
    Ranking,
    ScanObservation,
    compute_operation,
)
from eve_courier_optimizer.web.workspace_store import WorkspaceStore


def default_workspace_path() -> Path:
    """Return the durable, credential-free working directory used by the local UI."""

    if sys.platform == "win32" and os.environ.get("LOCALAPPDATA"):
        return Path(os.environ["LOCALAPPDATA"]) / "EveCourierRouteOptimizer"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "EveCourierRouteOptimizer"
    xdg_data_home = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg_data_home) if xdg_data_home else Path.home() / ".local" / "share"
    return base / "eve-courier-route-optimizer"


@dataclass(frozen=True, slots=True)
class _Proposal:
    id: str
    revision: int
    plan: RoutePlan


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
        self.planner = CourierPlanner(graph, esi, zkill, clock=clock)
        self.clock = clock
        self.workspace = workspace.resolve()
        self.store = WorkspaceStore(self.workspace)
        self._state = self.store.load()
        self._proposal: _Proposal | None = None
        self._plan_payload = self._decorate(self._state.plan, self._state.observation, self.trip)

    @property
    def zkill(self) -> ZkillClient | None:
        return self.planner.zkill

    @property
    def revision(self) -> int:
        return self._state.revision

    @property
    def observation(self) -> ContractSnapshot | None:
        """Acquisition metadata remains available across an SDE upgrade for a safe refresh."""
        return self._state.observation

    @property
    def snapshot(self) -> ContractSnapshot | None:
        observation = self.observation
        return (
            observation
            if observation is not None
            and observation.sde_build_number == self.graph.metadata.build_number
            else None
        )

    @property
    def trip(self) -> CourierTrip | None:
        return self._state.trip

    @property
    def plan(self) -> RoutePlan | None:
        return self._proposal.plan if self._proposal is not None else None

    @property
    def proposal_id(self) -> str | None:
        return self._proposal.id if self._proposal is not None else None

    @property
    def plan_payload(self) -> PlanPayload | None:
        return deepcopy(self._plan_payload)

    def _decorate(
        self,
        raw: PlanPayload | None,
        observation: ContractSnapshot | None,
        trip: CourierTrip | None,
    ) -> PlanPayload | None:
        if (
            raw is None
            or observation is None
            or observation.sde_build_number != self.graph.metadata.build_number
        ):
            return None
        scope = raw["scope"]
        if (
            scope.get("snapshot_fetched_at") != observation.fetched_at.isoformat()
            or scope.get("sde_build_number") != self.graph.metadata.build_number
        ):
            return None
        return responses.decorate_plan(deepcopy(raw), self.graph, observation, trip)

    def _commit(
        self,
        *,
        observation: ContractSnapshot | None,
        trip: CourierTrip | None,
        raw_plan: PlanPayload | None,
        proposal: RoutePlan | None = None,
    ) -> None:
        # Finish all fallible serialization/formatting before the single durable commit.
        display = self._decorate(raw_plan, observation, trip)
        proposed = (
            _Proposal(uuid4().hex, self.revision + 1, proposal) if proposal is not None else None
        )
        state = self.store.commit(
            expected_revision=self.revision,
            observation=observation,
            trip=trip,
            plan=raw_plan,
        )
        self._state = state
        self._plan_payload = display
        self._proposal = proposed

    def _transition_identity(self) -> TransitionIdentity:
        return {"revision": self.revision, "proposal_id": self.proposal_id}

    def require_revision(self, body: dict[str, object], *, required: bool = False) -> None:
        expected = body.get("expected_revision")
        if (required or expected is not None) and (
            type(expected) is not int or expected != self.revision
        ):
            raise WorkspaceConflict("the workspace changed; reload and review the current proposal")

    def publish(self, result: OperationResult, *, expected_revision: int) -> OperationResponse:
        if expected_revision != self.revision:
            raise WorkspaceConflict(
                "the workspace changed while this operation ran; reload and retry"
            )
        if isinstance(result, Ranking):
            return {
                **responses.ranked_contracts(result.problem, self.graph),
                **self._transition_identity(),
            }
        if isinstance(result, ScanObservation):
            if self.trip is not None:
                raise ValueError("an execution session already exists; use Replan")
            self._commit(observation=result.snapshot, trip=self.trip, raw_plan=None)
            return {
                "snapshot": responses.snapshot_summary(self.snapshot, self.graph),
                **self._transition_identity(),
            }
        self._commit(
            observation=result.snapshot,
            trip=self.trip,
            raw_plan=solve_result_to_dict(result.plan.result, result.plan.problem),
            proposal=result.plan,
        )
        displayed = self.plan_payload
        assert displayed is not None
        return {
            "snapshot": responses.snapshot_summary(self.snapshot, self.graph),
            "plan": displayed,
            "execution": responses.trip_response(self.trip, self.graph, self.clock()),
            **self._transition_identity(),
        }

    @overload
    def _operate(
        self, operation: Literal[Operation.SCAN], body: dict[str, object]
    ) -> ScanResponse: ...

    @overload
    def _operate(
        self, operation: Literal[Operation.RANK], body: dict[str, object]
    ) -> RankResponse: ...

    @overload
    def _operate(
        self, operation: Literal[Operation.SOLVE], body: dict[str, object]
    ) -> PlanResponse: ...

    @overload
    def _operate(
        self, operation: Literal[Operation.REPLAN], body: dict[str, object]
    ) -> PlanResponse: ...

    def _operate(self, operation: Operation, body: dict[str, object]) -> OperationResponse:
        self.require_revision(body)
        revision = self.revision
        result = compute_operation(
            operation,
            self.planner,
            body,
            observation=self.observation,
            trip=self.trip,
            at=self.clock(),
        )
        return self.publish(result, expected_revision=revision)

    def artifact(self, filename: str) -> object | None:
        return {
            "snapshot.json": snapshot_to_dict(self.observation)
            if self.observation is not None
            else None,
            "plan.json": deepcopy(self._state.plan),
            "execution.json": trip_to_dict(self.trip) if self.trip is not None else None,
        }.get(filename)

    def status(self) -> StatusResponse:
        latest = self.store.load()
        if latest.revision != self.revision:
            display = self._decorate(latest.plan, latest.observation, latest.trip)
            self._state = latest
            self._proposal = None
            self._plan_payload = display
        return {
            "app_version": __version__,
            **self._transition_identity(),
            "warnings": list(self._state.warnings),
            "snapshot_requires_refresh": self.observation is not None and self.snapshot is None,
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
                "snapshot": self.observation is not None,
                "plan": self._state.plan is not None,
                "execution": self.trip is not None,
            },
        }

    def region_matches(self, query: str) -> Suggestions:
        needle = query.casefold().strip()
        matches: list[Suggestion] = [
            Suggestion(id=region.region_id, name=region.name)
            for region in sorted(self.graph.regions.values(), key=lambda item: item.name)
            if not needle or needle in region.name.casefold()
        ][:50]
        return {"items": matches}

    def system_matches(self, query: str) -> Suggestions:
        needle = query.casefold().strip()
        if len(needle) < 2:
            return {"items": []}
        matches: list[Suggestion] = [
            Suggestion(
                id=system.system_id,
                name=system.name,
                security_status=system.security_status,
            )
            for system in sorted(self.graph.systems.values(), key=lambda item: item.name)
            if needle in system.name.casefold()
        ][:30]
        return {"items": matches}

    def scan(self, body: dict[str, object]) -> ScanResponse:
        return self._operate(Operation.SCAN, body)

    def rank(self, body: dict[str, object]) -> RankResponse:
        return self._operate(Operation.RANK, body)

    def solve(self, body: dict[str, object]) -> PlanResponse:
        return self._operate(Operation.SOLVE, body)

    def start_execution(self, body: dict[str, object]) -> ExecutionResponse:
        self.require_revision(body, required=True)
        if self._proposal is None or body.get("proposal_id") != self._proposal.id:
            raise WorkspaceConflict(
                "this proposal is no longer current; reload and review before applying it"
            )
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
        self._commit(observation=self.observation, trip=state, raw_plan=self._state.plan)
        return {
            "execution": responses.trip_response(self.trip, self.graph, self.clock()),
            "plan": self.plan_payload,
            **self._transition_identity(),
        }

    def record_action(self, body: dict[str, object]) -> ExecutionResponse:
        self.require_revision(body)
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
        self._commit(observation=self.observation, trip=state, raw_plan=self._state.plan)
        return {
            "execution": responses.trip_response(self.trip, self.graph, self.clock()),
            "plan": self.plan_payload,
            **self._transition_identity(),
        }

    def replan(self, body: dict[str, object]) -> PlanResponse:
        return self._operate(Operation.REPLAN, body)

    def reset_execution(self, body: dict[str, object] | None = None) -> ExecutionResponse:
        self.require_revision(body or {})
        self._commit(observation=self.observation, trip=None, raw_plan=None)
        return {"execution": None, "plan": None, **self._transition_identity()}

    def extend_horizon(self, body: dict[str, object]) -> ExecutionResponse:
        self.require_revision(body)
        if self.trip is None:
            raise ValueError("start an execution session before extending its horizon")
        minutes = requests.FormFields(body).optional_integer("minutes", minimum=1)
        if minutes is None:
            raise ValueError("minutes is required")
        state = self.trip.extend_horizon(
            additional_seconds=minutes * 60, at=max(self.clock(), self.trip.current_time)
        )
        self._commit(observation=self.observation, trip=state, raw_plan=None)
        return {
            "execution": responses.trip_response(self.trip, self.graph, self.clock()),
            "plan": None,
            **self._transition_identity(),
        }
