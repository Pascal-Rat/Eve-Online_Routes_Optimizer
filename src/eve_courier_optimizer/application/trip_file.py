"""Read and atomically save the versioned courier-trip JSON file."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final, cast

from eve_courier_optimizer.application.courier_trip import CourierTrip
from eve_courier_optimizer.domain import (
    ActiveShipment,
    CollateralMode,
    RoutableContract,
    TravelTimeModel,
    parse_esi_datetime,
)
from eve_courier_optimizer.eve.snapshot_file import contract_from_dict, contract_to_dict
from eve_courier_optimizer.jsonio import (
    json_array,
    json_bool,
    json_int,
    json_object,
    json_string,
    read_json_object,
    write_json,
)
from eve_courier_optimizer.routing.security import (
    security_policy_from_dict,
    security_policy_to_dict,
)

EXECUTION_STATE_SCHEMA_VERSION: Final = 3


def trip_to_dict(state: CourierTrip) -> dict[str, Any]:
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


def trip_from_dict(payload: dict[str, object]) -> CourierTrip:
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
    return CourierTrip(
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


def write_trip(path: Path, state: CourierTrip) -> None:
    write_json(path, trip_to_dict(state))


def read_trip(path: Path) -> CourierTrip:
    try:
        return trip_from_dict(read_json_object(path))
    except KeyError as error:
        raise ValueError(f"{path}: missing required field {error.args[0]!r}") from error
