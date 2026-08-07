"""Iteration 4: persistent execution state and rolling replanning transitions."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final, cast

from .domain import (
    ActiveShipment,
    CollateralMode,
    PlanningConstraints,
    RoutableContract,
    SecurityBand,
    SecurityPolicy,
    SolveResult,
    ThreatCategory,
    TravelTimeModel,
    parse_esi_datetime,
)
from .sde import UniverseGraph
from .snapshot import ContractSnapshot, contract_from_dict, contract_to_dict
from .threat_intel import threat_avoided_systems

EXECUTION_STATE_SCHEMA_VERSION: Final = 3


@dataclass(frozen=True, slots=True)
class ExecutionState:
    """Everything needed to replan after a real pickup/delivery or market refresh."""

    current_time: datetime
    session_deadline: datetime
    current_system_id: int
    cargo_capacity_units: int
    collateral_budget_units: int
    collateral_mode: CollateralMode
    travel: TravelTimeModel
    security: SecurityPolicy
    terminal_system_id: int | None = None
    remaining_required_system_ids: frozenset[int] = frozenset()
    max_simultaneous_contracts: int | None = None
    active_shipments: tuple[ActiveShipment, ...] = ()
    completed_contract_ids: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if self.current_time.tzinfo is None or self.session_deadline.tzinfo is None:
            raise ValueError("execution timestamps must be timezone-aware")
        if self.current_time > self.session_deadline:
            raise ValueError("execution time is after the session deadline")
        if self.terminal_system_id is not None and self.terminal_system_id <= 0:
            raise ValueError("execution terminal system ID must be positive")
        if any(system_id <= 0 for system_id in self.remaining_required_system_ids):
            raise ValueError("remaining required system IDs must be positive")
        if self.max_simultaneous_contracts is not None and self.max_simultaneous_contracts < 0:
            raise ValueError("max simultaneous contracts cannot be negative")
        if any(contract_id <= 0 for contract_id in self.completed_contract_ids):
            raise ValueError("completed contract IDs must be positive")
        if len(self.completed_contract_ids) != len(set(self.completed_contract_ids)):
            raise ValueError("completed contract IDs must be unique")
        active_ids = {
            shipment.contract.contract.contract_id for shipment in self.active_shipments
        }
        if active_ids & set(self.completed_contract_ids):
            raise ValueError("a contract cannot be both active and completed")
        picked_count = sum(1 for shipment in self.active_shipments if shipment.picked)
        if (
            self.max_simultaneous_contracts is not None
            and picked_count > self.max_simultaneous_contracts
        ):
            raise ValueError("execution state exceeds the simultaneous-contract limit")


def initial_execution_state(
    constraints: PlanningConstraints,
    contracts: tuple[RoutableContract, ...],
    active_shipments: tuple[ActiveShipment, ...],
    result: SolveResult,
    *,
    completed_contract_ids: tuple[int, ...] = (),
) -> ExecutionState:
    """Create the state immediately after the plan's required initial acceptances.

    In locked mode the mathematical model assumes all selected public contracts are accepted at
    time zero, so they become mandatory-but-unpicked commitments here. In rolling mode no optional
    contract is considered accepted until the user actually reaches its pickup and records it.
    """

    active_by_id = {
        shipment.contract.contract.contract_id: shipment for shipment in active_shipments
    }
    if constraints.collateral_mode is CollateralMode.LOCKED:
        optional = {item.contract.contract_id: item for item in contracts}
        for contract_id in result.selected_contract_ids:
            item = optional[contract_id]
            active_by_id[contract_id] = ActiveShipment(
                contract=item,
                deadline=constraints.snapshot_time
                + timedelta(days=item.contract.days_to_complete),
                picked=False,
            )
    return ExecutionState(
        current_time=constraints.snapshot_time,
        session_deadline=constraints.snapshot_time + timedelta(seconds=constraints.horizon_seconds),
        current_system_id=constraints.start_system_id,
        cargo_capacity_units=constraints.cargo_capacity_units,
        collateral_budget_units=constraints.collateral_budget_units,
        collateral_mode=constraints.collateral_mode,
        travel=constraints.travel,
        security=constraints.security,
        terminal_system_id=constraints.terminal_system_id,
        remaining_required_system_ids=(
            constraints.required_system_ids - {constraints.start_system_id}
        ),
        max_simultaneous_contracts=constraints.max_simultaneous_contracts,
        active_shipments=tuple(active_by_id[key] for key in sorted(active_by_id)),
        completed_contract_ids=completed_contract_ids,
    )


def constraints_for_replan(
    state: ExecutionState,
    snapshot: ContractSnapshot,
) -> PlanningConstraints:
    effective_time = max(state.current_time, snapshot.fetched_at)
    remaining = int((state.session_deadline - effective_time).total_seconds())
    if remaining < 0:
        raise ValueError("execution session has already ended")
    security = state.security
    exemptions = {state.current_system_id}
    exemptions.update(state.remaining_required_system_ids)
    if state.terminal_system_id is not None:
        exemptions.add(state.terminal_system_id)
    for shipment in state.active_shipments:
        if not shipment.picked:
            exemptions.add(shipment.contract.origin_system_id)
        exemptions.add(shipment.contract.destination_system_id)
    threshold = security.gank_ship_kill_threshold
    if threshold is not None:
        if snapshot.system_kills_fetched_at is None:
            raise ValueError(
                "gank awareness is active but the refreshed snapshot has no system-kill activity"
            )
        # A refreshed risk snapshot must never strand commitments the user has already accepted.
        # Their required endpoints (and the current system) remain serviceable; transit still
        # avoids every other system at or above the declared activity threshold.
        gank_avoids = {
            item.system_id
            for item in snapshot.system_kill_activity
            if item.ship_kills >= threshold and item.system_id not in exemptions
        }
        security = replace(
            security,
            gank_avoided_system_ids=frozenset(gank_avoids),
            gank_activity_fetched_at=snapshot.system_kills_fetched_at,
        )
    if security.threat_categories:
        if snapshot.threat_intel_fetched_at is None:
            raise ValueError(
                "gate-threat awareness is active but the refreshed snapshot has no zKill intel"
            )
        assert security.threat_min_events is not None
        security = replace(
            security,
            threat_avoided_system_ids=threat_avoided_systems(
                snapshot.gate_threat_events,
                security.threat_categories,
                minimum_events=security.threat_min_events,
                exempt_system_ids=frozenset(exemptions),
            ),
            threat_intel_fetched_at=snapshot.threat_intel_fetched_at,
            threat_window_seconds=snapshot.threat_window_seconds,
            threat_gate_radius_m=snapshot.threat_gate_radius_m,
            threat_coverage_region_ids=frozenset(snapshot.threat_coverage_region_ids),
            threat_incomplete_region_ids=frozenset(snapshot.threat_incomplete_region_ids),
        )
    return PlanningConstraints(
        start_system_id=state.current_system_id,
        cargo_capacity_units=state.cargo_capacity_units,
        collateral_budget_units=state.collateral_budget_units,
        horizon_seconds=remaining,
        snapshot_time=effective_time,
        collateral_mode=state.collateral_mode,
        travel=state.travel,
        security=security,
        return_to_start=False,
        required_system_ids=state.remaining_required_system_ids,
        finish_system_id=state.terminal_system_id,
        max_simultaneous_contracts=state.max_simultaneous_contracts,
    )


def record_pickup(
    state: ExecutionState,
    snapshot: ContractSnapshot,
    graph: UniverseGraph,
    contract_id: int,
    at: datetime,
) -> ExecutionState:
    """Record a successful in-game pickup/accept-and-pickup event."""

    if at < state.current_time or at > state.session_deadline:
        raise ValueError("pickup time is outside the execution session")
    active_by_id = {
        shipment.contract.contract.contract_id: shipment for shipment in state.active_shipments
    }
    existing = active_by_id.get(contract_id)
    if existing is not None:
        if existing.picked:
            raise ValueError(f"contract {contract_id} is already picked up")
        if (
            state.max_simultaneous_contracts is not None
            and sum(1 for shipment in state.active_shipments if shipment.picked) + 1
            > state.max_simultaneous_contracts
        ):
            raise ValueError("pickup would exceed the simultaneous-contract limit")
        active_by_id[contract_id] = replace(existing, picked=True)
        return replace(
            state,
            current_time=at,
            current_system_id=existing.contract.origin_system_id,
            remaining_required_system_ids=(
                state.remaining_required_system_ids - {existing.contract.origin_system_id}
            ),
            active_shipments=tuple(active_by_id[key] for key in sorted(active_by_id)),
        )

    if state.collateral_mode is CollateralMode.LOCKED:
        raise ValueError("locked-mode pickup must already exist as an accepted commitment")
    public = next((item for item in snapshot.contracts if item.contract_id == contract_id), None)
    if public is None:
        raise ValueError(f"contract {contract_id} is not present in the supplied snapshot")
    if at >= public.date_expired:
        raise ValueError(f"contract {contract_id} listing has expired")
    origin = graph.station_system(public.origin_location_id)
    destination = graph.station_system(public.destination_location_id)
    if origin is None or destination is None:
        raise ValueError("rolling pickup has an unsupported non-NPC-station endpoint")
    if not graph.system_allowed(origin, state.security) or not graph.system_allowed(
        destination, state.security
    ):
        raise ValueError("rolling pickup violates the execution security policy")
    locked_now = sum(
        shipment.contract.contract.collateral_units for shipment in state.active_shipments
    )
    cargo_now = sum(
        shipment.contract.contract.volume_units
        for shipment in state.active_shipments
        if shipment.picked
    )
    if locked_now + public.collateral_units > state.collateral_budget_units:
        raise ValueError("pickup would exceed collateral budget")
    if cargo_now + public.volume_units > state.cargo_capacity_units:
        raise ValueError("pickup would exceed cargo capacity")
    if (
        state.max_simultaneous_contracts is not None
        and sum(1 for shipment in state.active_shipments if shipment.picked) + 1
        > state.max_simultaneous_contracts
    ):
        raise ValueError("pickup would exceed the simultaneous-contract limit")
    routable = RoutableContract(public, origin, destination)
    active_by_id[contract_id] = ActiveShipment(
        contract=routable,
        deadline=at + timedelta(days=public.days_to_complete),
        picked=True,
    )
    return replace(
        state,
        current_time=at,
        current_system_id=origin,
        remaining_required_system_ids=state.remaining_required_system_ids - {origin},
        active_shipments=tuple(active_by_id[key] for key in sorted(active_by_id)),
    )


def record_delivery(state: ExecutionState, contract_id: int, at: datetime) -> ExecutionState:
    if at < state.current_time or at > state.session_deadline:
        raise ValueError("delivery time is outside the execution session")
    active_by_id = {
        shipment.contract.contract.contract_id: shipment for shipment in state.active_shipments
    }
    shipment = active_by_id.get(contract_id)
    if shipment is None:
        raise ValueError(f"contract {contract_id} is not an active commitment")
    if not shipment.picked:
        raise ValueError(f"contract {contract_id} has not been picked up")
    if at > shipment.deadline:
        raise ValueError(f"contract {contract_id} was delivered after its modeled deadline")
    del active_by_id[contract_id]
    completed = tuple(sorted((*state.completed_contract_ids, contract_id)))
    return replace(
        state,
        current_time=at,
        current_system_id=shipment.contract.destination_system_id,
        remaining_required_system_ids=(
            state.remaining_required_system_ids - {shipment.contract.destination_system_id}
        ),
        active_shipments=tuple(active_by_id[key] for key in sorted(active_by_id)),
        completed_contract_ids=completed,
    )


def record_route_system(state: ExecutionState, system_id: int, at: datetime) -> ExecutionState:
    """Record that the pilot actually reached a required waypoint or final system."""

    allowed_markers = set(state.remaining_required_system_ids)
    if state.terminal_system_id is not None:
        allowed_markers.add(state.terminal_system_id)
    if system_id not in allowed_markers:
        raise ValueError("system is not a pending required waypoint or finish")
    if at < state.current_time or at > state.session_deadline:
        raise ValueError("route-system time is outside the execution session")
    return replace(
        state,
        current_time=at,
        current_system_id=system_id,
        remaining_required_system_ids=state.remaining_required_system_ids - {system_id},
    )


def execution_state_to_dict(state: ExecutionState) -> dict[str, Any]:
    return {
        "schema_version": EXECUTION_STATE_SCHEMA_VERSION,
        "current_time": state.current_time.isoformat(),
        "session_deadline": state.session_deadline.isoformat(),
        "current_system_id": state.current_system_id,
        "cargo_capacity_units": state.cargo_capacity_units,
        "collateral_budget_units": state.collateral_budget_units,
        "collateral_mode": state.collateral_mode.value,
        "terminal_system_id": state.terminal_system_id,
        "remaining_required_system_ids": sorted(state.remaining_required_system_ids),
        "max_simultaneous_contracts": state.max_simultaneous_contracts,
        "travel": {
            "seconds_per_jump": state.travel.seconds_per_jump,
            "service_seconds": state.travel.service_seconds,
        },
        "security": {
            "minimum_security": state.security.minimum_security,
            "avoided_system_ids": sorted(state.security.avoided_system_ids),
            "allowed_bands": (
                sorted(band.value for band in state.security.allowed_bands)
                if state.security.allowed_bands is not None
                else None
            ),
            "gank_avoided_system_ids": sorted(state.security.gank_avoided_system_ids),
            "gank_ship_kill_threshold": state.security.gank_ship_kill_threshold,
            "gank_activity_fetched_at": (
                state.security.gank_activity_fetched_at.isoformat()
                if state.security.gank_activity_fetched_at is not None
                else None
            ),
            "threat_avoided_system_ids": sorted(
                state.security.threat_avoided_system_ids
            ),
            "threat_categories": sorted(
                category.value for category in state.security.threat_categories
            ),
            "threat_min_events": state.security.threat_min_events,
            "threat_intel_fetched_at": (
                state.security.threat_intel_fetched_at.isoformat()
                if state.security.threat_intel_fetched_at is not None
                else None
            ),
            "threat_window_seconds": state.security.threat_window_seconds,
            "threat_gate_radius_m": state.security.threat_gate_radius_m,
            "threat_coverage_region_ids": sorted(
                state.security.threat_coverage_region_ids
            ),
            "threat_incomplete_region_ids": sorted(
                state.security.threat_incomplete_region_ids
            ),
        },
        "completed_contract_ids": list(state.completed_contract_ids),
        "active_shipments": [
            {
                "contract": contract_to_dict(item.contract.contract),
                "origin_system_id": item.contract.origin_system_id,
                "destination_system_id": item.contract.destination_system_id,
                "deadline": item.deadline.isoformat(),
                "picked": item.picked,
            }
            for item in state.active_shipments
        ],
    }


def execution_state_from_dict(payload: dict[str, Any]) -> ExecutionState:
    if payload.get("schema_version") not in {1, 2, EXECUTION_STATE_SCHEMA_VERSION}:
        raise ValueError("unsupported execution-state schema")
    raw_travel = cast(dict[str, Any], payload["travel"])
    raw_security = cast(dict[str, Any], payload["security"])
    raw_active = cast(list[Any], payload.get("active_shipments", []))
    raw_completed = cast(list[Any], payload.get("completed_contract_ids", []))
    active: list[ActiveShipment] = []
    for item in raw_active:
        if not isinstance(item, dict):
            raise ValueError("active shipment must be an object")
        row = cast(dict[str, Any], item)
        public = contract_from_dict(cast(dict[str, Any], row["contract"]))
        active.append(
            ActiveShipment(
                contract=RoutableContract(
                    public,
                    int(row["origin_system_id"]),
                    int(row["destination_system_id"]),
                ),
                deadline=parse_esi_datetime(str(row["deadline"])),
                picked=bool(row["picked"]),
            )
        )
    minimum_security_raw = raw_security.get("minimum_security")
    minimum_security = (
        None if minimum_security_raw is None else float(minimum_security_raw)
    )
    raw_allowed_bands = raw_security.get("allowed_bands")
    allowed_bands = (
        None
        if raw_allowed_bands is None
        else frozenset(
            SecurityBand(str(item)) for item in cast(list[Any], raw_allowed_bands)
        )
    )
    raw_gank_threshold = raw_security.get("gank_ship_kill_threshold")
    raw_gank_time = raw_security.get("gank_activity_fetched_at")
    raw_threat_time = raw_security.get("threat_intel_fetched_at")
    return ExecutionState(
        current_time=parse_esi_datetime(str(payload["current_time"])),
        session_deadline=parse_esi_datetime(str(payload["session_deadline"])),
        current_system_id=int(payload["current_system_id"]),
        cargo_capacity_units=int(payload["cargo_capacity_units"]),
        collateral_budget_units=int(payload["collateral_budget_units"]),
        collateral_mode=CollateralMode(str(payload["collateral_mode"])),
        travel=TravelTimeModel(
            seconds_per_jump=int(raw_travel["seconds_per_jump"]),
            service_seconds=int(raw_travel["service_seconds"]),
        ),
        security=SecurityPolicy(
            minimum_security=minimum_security,
            avoided_system_ids=frozenset(
                int(item) for item in cast(list[Any], raw_security.get("avoided_system_ids", []))
            ),
            allowed_bands=allowed_bands,
            gank_avoided_system_ids=frozenset(
                int(item)
                for item in cast(list[Any], raw_security.get("gank_avoided_system_ids", []))
            ),
            gank_ship_kill_threshold=(
                int(raw_gank_threshold) if raw_gank_threshold is not None else None
            ),
            gank_activity_fetched_at=(
                parse_esi_datetime(str(raw_gank_time)) if raw_gank_time else None
            ),
            threat_avoided_system_ids=frozenset(
                int(item)
                for item in cast(
                    list[Any], raw_security.get("threat_avoided_system_ids", [])
                )
            ),
            threat_categories=frozenset(
                ThreatCategory(str(item))
                for item in cast(list[Any], raw_security.get("threat_categories", []))
            ),
            threat_min_events=(
                int(raw_security["threat_min_events"])
                if raw_security.get("threat_min_events") is not None
                else None
            ),
            threat_intel_fetched_at=(
                parse_esi_datetime(str(raw_threat_time)) if raw_threat_time else None
            ),
            threat_window_seconds=(
                int(raw_security["threat_window_seconds"])
                if raw_security.get("threat_window_seconds") is not None
                else None
            ),
            threat_gate_radius_m=(
                int(raw_security["threat_gate_radius_m"])
                if raw_security.get("threat_gate_radius_m") is not None
                else None
            ),
            threat_coverage_region_ids=frozenset(
                int(item)
                for item in cast(
                    list[Any], raw_security.get("threat_coverage_region_ids", [])
                )
            ),
            threat_incomplete_region_ids=frozenset(
                int(item)
                for item in cast(
                    list[Any], raw_security.get("threat_incomplete_region_ids", [])
                )
            ),
        ),
        terminal_system_id=(
            int(payload["terminal_system_id"])
            if payload.get("terminal_system_id") is not None
            else None
        ),
        remaining_required_system_ids=frozenset(
            int(item)
            for item in cast(list[Any], payload.get("remaining_required_system_ids", []))
        ),
        max_simultaneous_contracts=(
            int(payload["max_simultaneous_contracts"])
            if payload.get("max_simultaneous_contracts") is not None
            else None
        ),
        active_shipments=tuple(active),
        completed_contract_ids=tuple(int(item) for item in raw_completed),
    )


def write_execution_state(path: Path, state: ExecutionState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(execution_state_to_dict(state), indent=2, sort_keys=True) + "\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(serialized, encoding="utf-8")
    temporary.replace(path)


def read_execution_state(path: Path) -> ExecutionState:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("execution-state root must be an object")
    return execution_state_from_dict(cast(dict[str, Any], payload))
