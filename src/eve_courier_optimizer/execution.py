"""Persistent accepted commitments, real progress, and replanning constraints."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Final, cast

from .domain import (
    ActiveShipment,
    CollateralMode,
    PlanningConstraints,
    RoutableContract,
    SecurityPolicy,
    SolveResult,
    TravelTimeModel,
    parse_esi_datetime,
)
from .jsonio import (
    json_array,
    json_bool,
    json_int,
    json_object,
    json_string,
    read_json_object,
    write_json,
)
from .route_policy import (
    observed_security_policy,
    security_policy_from_dict,
    security_policy_to_dict,
)
from .sde import UniverseGraph
from .snapshot import ContractSnapshot, contract_from_dict, contract_to_dict

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
        if self.current_system_id <= 0:
            raise ValueError("execution system ID must be positive")
        if self.cargo_capacity_units < 0 or self.collateral_budget_units < 0:
            raise ValueError("execution resource limits cannot be negative")
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
        active_ids = {shipment.contract.contract_id for shipment in self.active_shipments}
        if len(active_ids) != len(self.active_shipments):
            raise ValueError("active shipment contract IDs must be unique")
        if sum(s.contract.volume_units for s in self.active_shipments if s.picked) > (
            self.cargo_capacity_units
        ):
            raise ValueError("execution cargo exceeds cargo capacity")
        if sum(s.contract.collateral_units for s in self.active_shipments) > (
            self.collateral_budget_units
        ):
            raise ValueError("execution collateral exceeds budget")
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

    active_by_id = {shipment.contract.contract_id: shipment for shipment in active_shipments}
    if constraints.collateral_mode is CollateralMode.LOCKED:
        optional = {item.contract_id: item for item in contracts}
        for contract_id in result.selected_contract_ids:
            item = optional[contract_id]
            active_by_id[contract_id] = ActiveShipment(
                contract=item,
                deadline=constraints.snapshot_time + timedelta(days=item.days_to_complete),
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
    *,
    at: datetime | None = None,
) -> PlanningConstraints:
    if at is not None and at.tzinfo is None:
        raise ValueError("replanning time must be timezone-aware")
    effective_time = max(state.current_time, snapshot.fetched_at, at or state.current_time)
    if effective_time > state.session_deadline:
        raise ValueError("planning horizon has ended; extend the horizon before replanning")
    remaining = int((state.session_deadline - effective_time).total_seconds())
    security = state.security
    exemptions = {state.current_system_id}
    exemptions.update(state.remaining_required_system_ids)
    if state.terminal_system_id is not None:
        exemptions.add(state.terminal_system_id)
    for shipment in state.active_shipments:
        if not shipment.picked:
            exemptions.add(shipment.contract.origin_system_id)
        exemptions.add(shipment.contract.destination_system_id)
    # Refreshed observations may not strand already accepted obligations.
    security = observed_security_policy(
        snapshot,
        minimum_security=security.minimum_security,
        allowed_bands=security.allowed_bands,
        avoided_system_ids=security.avoided_system_ids,
        activity_threshold=security.gank_ship_kill_threshold,
        threat_categories=security.threat_categories,
        threat_min_events=security.threat_min_events,
        exempt_system_ids=frozenset(exemptions),
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
    snapshot: ContractSnapshot | None,
    graph: UniverseGraph,
    contract_id: int,
    at: datetime,
) -> ExecutionState:
    """Record a successful in-game pickup/accept-and-pickup event."""

    if at.tzinfo is None or at < state.current_time:
        raise ValueError("pickup time must be timezone-aware and cannot precede recorded progress")
    active_by_id = {shipment.contract.contract_id: shipment for shipment in state.active_shipments}
    existing = active_by_id.get(contract_id)
    if existing is not None:
        if existing.picked:
            raise ValueError(f"contract {contract_id} is already picked up")
        if at > existing.deadline:
            raise ValueError(f"contract {contract_id} is past its delivery deadline")
        cargo_now = sum(
            shipment.contract.volume_units for shipment in state.active_shipments if shipment.picked
        )
        if cargo_now + existing.contract.volume_units > state.cargo_capacity_units:
            raise ValueError("pickup would exceed cargo capacity")
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
    if snapshot is None:
        raise ValueError("a snapshot is required to accept a new rolling contract")
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
    locked_now = sum(shipment.contract.collateral_units for shipment in state.active_shipments)
    cargo_now = sum(
        shipment.contract.volume_units for shipment in state.active_shipments if shipment.picked
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
    routable = RoutableContract.resolve(public, origin, destination)
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
    if at.tzinfo is None or at < state.current_time:
        raise ValueError(
            "delivery time must be timezone-aware and cannot precede recorded progress"
        )
    active_by_id = {shipment.contract.contract_id: shipment for shipment in state.active_shipments}
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
    if at.tzinfo is None or at < state.current_time:
        raise ValueError("route-system time must be timezone-aware and cannot precede progress")
    return replace(
        state,
        current_time=at,
        current_system_id=system_id,
        remaining_required_system_ids=state.remaining_required_system_ids - {system_id},
    )


def extend_execution_horizon(
    state: ExecutionState,
    *,
    additional_seconds: int,
    at: datetime,
) -> ExecutionState:
    """Extend the planning budget without changing any accepted contract deadline."""

    if additional_seconds <= 0:
        raise ValueError("horizon extension must be positive")
    if at.tzinfo is None or at < state.current_time:
        raise ValueError("extension time must be timezone-aware and cannot precede progress")
    return replace(
        state,
        current_time=at,
        session_deadline=max(at, state.session_deadline) + timedelta(seconds=additional_seconds),
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
        "security": security_policy_to_dict(state.security),
        "completed_contract_ids": list(state.completed_contract_ids),
        "active_shipments": [
            {
                "contract": contract_to_dict(item.contract),
                "origin_system_id": item.contract.origin_system_id,
                "destination_system_id": item.contract.destination_system_id,
                "deadline": item.deadline.isoformat(),
                "picked": item.picked,
            }
            for item in state.active_shipments
        ],
    }


def execution_state_from_dict(payload: dict[str, object]) -> ExecutionState:
    schema_version = json_int(payload.get("schema_version"), "execution schema_version")
    if schema_version not in {1, 2, EXECUTION_STATE_SCHEMA_VERSION}:
        raise ValueError("unsupported execution-state schema")
    raw_travel = json_object(payload["travel"], "travel")
    raw_security = json_object(payload["security"], "security")
    raw_active = json_array(payload.get("active_shipments", []), "active_shipments")
    raw_completed = json_array(payload.get("completed_contract_ids", []), "completed_contract_ids")
    active: list[ActiveShipment] = []
    for item in raw_active:
        if not isinstance(item, dict):
            raise ValueError("active shipment must be an object")
        row = cast(dict[str, object], item)
        public = contract_from_dict(json_object(row["contract"], "active contract"))
        active.append(
            ActiveShipment(
                contract=RoutableContract.resolve(
                    public,
                    json_int(row["origin_system_id"], "origin_system_id"),
                    json_int(row["destination_system_id"], "destination_system_id"),
                ),
                deadline=parse_esi_datetime(json_string(row["deadline"], "deadline")),
                picked=json_bool(row["picked"], "picked"),
            )
        )
    return ExecutionState(
        current_time=parse_esi_datetime(json_string(payload["current_time"], "current_time")),
        session_deadline=parse_esi_datetime(
            json_string(payload["session_deadline"], "session_deadline")
        ),
        current_system_id=json_int(payload["current_system_id"], "current_system_id"),
        cargo_capacity_units=json_int(payload["cargo_capacity_units"], "cargo_capacity_units"),
        collateral_budget_units=json_int(
            payload["collateral_budget_units"], "collateral_budget_units"
        ),
        collateral_mode=CollateralMode(json_string(payload["collateral_mode"], "collateral_mode")),
        travel=TravelTimeModel(
            seconds_per_jump=json_int(raw_travel["seconds_per_jump"], "seconds_per_jump"),
            service_seconds=json_int(raw_travel["service_seconds"], "service_seconds"),
        ),
        security=security_policy_from_dict(raw_security),
        terminal_system_id=(
            json_int(payload["terminal_system_id"], "terminal_system_id")
            if payload.get("terminal_system_id") is not None
            else None
        ),
        remaining_required_system_ids=frozenset(
            json_int(item, "array entry")
            for item in json_array(
                payload.get("remaining_required_system_ids", []), "remaining_required_system_ids"
            )
        ),
        max_simultaneous_contracts=(
            json_int(payload["max_simultaneous_contracts"], "max_simultaneous_contracts")
            if payload.get("max_simultaneous_contracts") is not None
            else None
        ),
        active_shipments=tuple(active),
        completed_contract_ids=tuple(
            json_int(item, "completed contract ID") for item in raw_completed
        ),
    )


def write_execution_state(path: Path, state: ExecutionState) -> None:
    write_json(path, execution_state_to_dict(state))


def read_execution_state(path: Path) -> ExecutionState:
    try:
        return execution_state_from_dict(read_json_object(path))
    except KeyError as error:
        raise ValueError(f"{path}: missing required field {error.args[0]!r}") from error
