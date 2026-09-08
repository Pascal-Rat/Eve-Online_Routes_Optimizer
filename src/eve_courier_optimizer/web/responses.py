"""Pilot-facing names and display values derived from canonical route and execution artifacts."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any, cast

from eve_courier_optimizer.application.courier_trip import CourierTrip
from eve_courier_optimizer.application.trip_file import trip_to_dict
from eve_courier_optimizer.domain import (
    ContractSnapshot,
    PublicCourierContract,
    isk_units_to_decimal,
    security_band,
    volume_units_to_decimal,
)
from eve_courier_optimizer.routing.route_problem import RouteProblem
from eve_courier_optimizer.routing.universe import UniverseGraph

JsonObject = dict[str, Any]


def snapshot_summary(snapshot: ContractSnapshot | None, graph: UniverseGraph) -> JsonObject | None:
    if snapshot is None:
        return None
    age_seconds = max(
        0,
        int((datetime.now(UTC) - snapshot.fetched_at).total_seconds()),
    )
    return {
        "fetched_at": snapshot.fetched_at.isoformat(),
        "age_seconds": age_seconds,
        "compatibility_date": snapshot.compatibility_date,
        "sde_build_number": snapshot.sde_build_number,
        "region_ids": list(snapshot.region_ids),
        "region_names": [
            graph.regions[region_id].name
            for region_id in snapshot.region_ids
            if region_id in graph.regions
        ],
        "contracts": len(snapshot.contracts),
        "system_kills_fetched_at": (
            snapshot.system_kills_fetched_at.isoformat()
            if snapshot.system_kills_fetched_at is not None
            else None
        ),
        "system_kill_systems": len(snapshot.system_kill_activity),
        "threat_intel_fetched_at": (
            snapshot.threat_intel_fetched_at.isoformat()
            if snapshot.threat_intel_fetched_at is not None
            else None
        ),
        "threat_window_seconds": snapshot.threat_window_seconds,
        "threat_gate_radius_m": snapshot.threat_gate_radius_m,
        "threat_coverage_region_ids": list(snapshot.threat_coverage_region_ids),
        "threat_incomplete_region_ids": list(snapshot.threat_incomplete_region_ids),
        "threat_killmails_seen": snapshot.threat_killmails_seen,
        "gate_threat_events": len(snapshot.gate_threat_events),
    }


def trip_response(
    execution: CourierTrip | None, graph: UniverseGraph, at: datetime
) -> JsonObject | None:
    if execution is None:
        return None
    payload = trip_to_dict(execution)
    for shipment in payload["active_shipments"]:
        for key in ("origin", "destination"):
            system_id = shipment[f"{key}_system_id"]
            system_name = graph.systems.get(system_id)
            shipment[f"{key}_system_name"] = system_name.name if system_name else str(system_id)
    system = graph.systems.get(execution.current_system_id)
    terminal = (
        graph.systems.get(execution.terminal_system_id)
        if execution.terminal_system_id is not None
        else None
    )
    cargo_units = sum(
        shipment.contract.volume_units for shipment in execution.active_shipments if shipment.picked
    )
    collateral_units = sum(
        shipment.contract.collateral_units for shipment in execution.active_shipments
    )
    payload.update(
        {
            "current_system_name": system.name if system is not None else None,
            "active_count": len(execution.active_shipments),
            "completed_count": len(execution.completed_contract_ids),
            "cargo_in_use_m3": str(volume_units_to_decimal(cargo_units)),
            "collateral_locked_isk": str(isk_units_to_decimal(collateral_units)),
            "terminal_system_name": terminal.name if terminal is not None else None,
            "remaining_required_systems": [
                {
                    "system_id": system_id,
                    "name": graph.systems[system_id].name,
                }
                for system_id in sorted(execution.remaining_required_system_ids)
                if system_id in graph.systems
            ],
            "can_end_safely": not execution.active_shipments,
            "horizon_expired": at > execution.session_deadline,
        }
    )
    return payload


def scope_payload(problem: RouteProblem) -> JsonObject:
    scope = problem.scope
    return {
        "public_couriers_seen": scope.public_couriers_seen,
        "eligible_contracts": scope.eligible_contracts,
        "policy_exclusions": dict(scope.policy_exclusions),
        "safe_reductions": dict(scope.safe_reductions),
        "heuristic_reductions": dict(scope.heuristic_reductions),
        "scope_untruncated": scope.is_untruncated,
    }


def decorate_plan(
    payload: JsonObject,
    graph: UniverseGraph,
    snapshot: ContractSnapshot | None,
    execution: CourierTrip | None,
) -> JsonObject:
    model = payload.get("model")
    if isinstance(model, dict):
        saved_model = cast(JsonObject, model)
        start_id = int(saved_model.get("start_system_id", 0))
        start = graph.systems.get(start_id)
        saved_model["start_system_name"] = start.name if start is not None else None
        finish_id = saved_model.get("finish_system_id")
        finish = graph.systems.get(int(finish_id)) if finish_id is not None else None
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
                    "name": graph.systems[system_id].name,
                }
                for raw_id in cast(list[int | str], raw_ids)
                if (system_id := int(raw_id)) in graph.systems
            ]

    route = payload.get("route")
    if not isinstance(route, list):
        return payload
    public = (
        {contract.contract_id: contract for contract in snapshot.contracts}
        if snapshot is not None
        else {}
    )
    active = (
        {
            shipment.contract.contract_id: shipment.contract
            for shipment in execution.active_shipments
        }
        if execution is not None
        else {}
    )
    for raw_step in cast(list[object], route):
        if not isinstance(raw_step, dict):
            continue
        step = cast(JsonObject, raw_step)
        contract_id = int(step.get("contract_id", 0))
        contract = public.get(contract_id) or active.get(contract_id)
        _decorate_route_step(graph, step, contract, mandatory=contract_id in active)
    _decorate_travel_legs(payload, graph)
    return payload


def _decorate_jump_path(payload: JsonObject, graph: UniverseGraph) -> None:
    raw_path = payload.get("jump_path")
    if not isinstance(raw_path, list):
        return
    payload["jump_count"] = max(0, len(cast(list[object], raw_path)) - 1)
    path_systems: list[JsonObject] = []
    for raw_system_id in cast(list[int | str], raw_path):
        system_id = int(raw_system_id)
        path_system = graph.systems.get(system_id)
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


def _decorate_travel_legs(payload: JsonObject, graph: UniverseGraph) -> None:
    raw_legs = payload.get("travel_legs")
    if not isinstance(raw_legs, list):
        return
    for raw_leg in cast(list[object], raw_legs):
        if not isinstance(raw_leg, dict):
            continue
        leg = cast(JsonObject, raw_leg)
        from_system = graph.systems.get(int(leg.get("from_system_id", 0)))
        to_system = graph.systems.get(int(leg.get("to_system_id", 0)))
        leg["from_system_name"] = from_system.name if from_system is not None else None
        leg["to_system_name"] = to_system.name if to_system is not None else None
        _decorate_jump_path(leg, graph)


def _decorate_route_step(
    graph: UniverseGraph,
    step: JsonObject,
    contract: PublicCourierContract | None,
    *,
    mandatory: bool,
) -> None:
    """Add human-readable pilot guidance without changing canonical plan semantics."""

    system = graph.systems.get(int(step.get("system_id", 0)))
    step["system_name"] = system.name if system is not None else None
    _decorate_jump_path(step, graph)
    if contract is not None:
        step["title"] = contract.title
        step["reward_isk"] = str(isk_units_to_decimal(contract.reward_units))
        step["volume_m3"] = str(volume_units_to_decimal(contract.volume_units))
        step["mandatory"] = mandatory


def ranked_contracts(problem: RouteProblem, graph: UniverseGraph) -> JsonObject:
    scores = problem.rank_contracts()
    items: list[JsonObject] = []
    for score in scores[:50]:
        contract = score.contract
        origin = graph.systems[score.contract.origin_system_id]
        destination = graph.systems[score.contract.destination_system_id]
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
                    score.reward_per_hour_isk if math.isfinite(score.reward_per_hour_isk) else None
                ),
                "reward_per_jump_isk": (
                    score.reward_per_jump_isk if math.isfinite(score.reward_per_jump_isk) else None
                ),
                "reward_to_collateral": (
                    score.reward_to_collateral
                    if math.isfinite(score.reward_to_collateral)
                    else None
                ),
            }
        )
    return {"scope": scope_payload(problem), "items": items}
