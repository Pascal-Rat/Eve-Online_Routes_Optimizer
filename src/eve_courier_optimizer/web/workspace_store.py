"""Atomically persist the snapshot, disposable proposal, and authoritative execution together."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from eve_courier_optimizer.application.courier_trip import CourierTrip
from eve_courier_optimizer.application.file_lock import WorkspaceConflict, directory_write_lock
from eve_courier_optimizer.application.plan_contract import PlanPayload
from eve_courier_optimizer.application.plan_file import validate_saved_plan
from eve_courier_optimizer.application.trip_file import read_trip, trip_from_dict, trip_to_dict
from eve_courier_optimizer.domain import ContractSnapshot
from eve_courier_optimizer.eve.snapshot_file import (
    read_snapshot,
    snapshot_from_dict,
    snapshot_to_dict,
)
from eve_courier_optimizer.jsonio import json_int, json_object, read_json_object, write_json


@dataclass(frozen=True, slots=True)
class StoredWorkspace:
    revision: int = 0
    observation: ContractSnapshot | None = None
    trip: CourierTrip | None = None
    plan: PlanPayload | None = None
    warnings: tuple[str, ...] = ()


def _display_plan(value: object) -> tuple[PlanPayload | None, tuple[str, ...]]:
    if value is None:
        return None, ()
    try:
        return validate_saved_plan(value), ()
    except ValueError:
        return None, (
            "The saved proposal is invalid and was ignored. Solve or replan to replace it.",
        )


class WorkspaceStore:
    """One atomic JSON replacement is the commit point; a process lock protects revision checks."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory.resolve()
        self.path = self.directory / "workspace.json"

    def load(self) -> StoredWorkspace:
        with directory_write_lock(self.directory):
            if self.path.exists():
                return self._read()
            # Import legacy artifacts once, while standalone CLI writers are excluded. Leave the
            # originals intact as migration backups; all future reads use workspace.json.
            snapshot_path = self.directory / "snapshot.json"
            trip_path = self.directory / "execution.json"
            plan_path = self.directory / "plan.json"
            observation = read_snapshot(snapshot_path) if snapshot_path.exists() else None
            trip = read_trip(trip_path) if trip_path.exists() else None
            plan: PlanPayload | None = None
            warnings: tuple[str, ...] = ()
            if plan_path.exists():
                try:
                    raw = json.loads(plan_path.read_text(encoding="utf-8"))
                except (ValueError, UnicodeError):
                    raw = "invalid plan JSON"
                plan, warnings = _display_plan(raw)
            state = StoredWorkspace(0, observation, trip, plan, warnings)
            self._write(state)
            return state

    def _read(self) -> StoredWorkspace:
        payload = read_json_object(self.path)
        if json_int(payload.get("schema_version"), "workspace schema") != 1:
            raise ValueError("unsupported workspace schema")
        revision = json_int(payload.get("revision"), "workspace revision")
        if revision < 0:
            raise ValueError("workspace revision cannot be negative")
        if not {"snapshot", "execution", "plan"}.issubset(payload):
            raise ValueError("workspace snapshot, execution and plan fields are required")
        snapshot, execution = payload["snapshot"], payload["execution"]
        # Authoritative data errors must remain visible; only a disposable proposal is recoverable.
        observation = (
            snapshot_from_dict(json_object(snapshot, "snapshot")) if snapshot is not None else None
        )
        trip = (
            trip_from_dict(json_object(execution, "execution")) if execution is not None else None
        )
        plan, warnings = _display_plan(payload.get("plan"))
        return StoredWorkspace(revision, observation, trip, plan, warnings)

    def _write(self, state: StoredWorkspace) -> None:
        write_json(
            self.path,
            {
                "schema_version": 1,
                "revision": state.revision,
                "snapshot": snapshot_to_dict(state.observation)
                if state.observation is not None
                else None,
                "execution": trip_to_dict(state.trip) if state.trip is not None else None,
                "plan": state.plan,
            },
        )

    def commit(
        self,
        *,
        expected_revision: int,
        observation: ContractSnapshot | None,
        trip: CourierTrip | None,
        plan: PlanPayload | None,
    ) -> StoredWorkspace:
        with directory_write_lock(self.directory):
            current = self._read()
            if current.revision != expected_revision:
                raise WorkspaceConflict(
                    "the workspace changed in another process; reload before continuing"
                )
            updated = StoredWorkspace(current.revision + 1, observation, trip, plan)
            self._write(updated)
            return updated
