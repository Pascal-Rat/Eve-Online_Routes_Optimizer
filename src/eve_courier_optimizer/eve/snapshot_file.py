"""Serializable contract snapshots used to make optimization reproducible."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Final, cast

from eve_courier_optimizer.domain import (
    ContractSnapshot,
    GateEvidence,
    GateThreatEvent,
    PublicCourierContract,
    SystemKillActivity,
    ThreatCategory,
    parse_esi_datetime,
)
from eve_courier_optimizer.jsonio import (
    json_array,
    json_int,
    json_string,
    read_json_object,
    write_json,
)

SNAPSHOT_SCHEMA_VERSION: Final = 2


def contract_to_dict(contract: PublicCourierContract) -> dict[str, Any]:
    return {
        "contract_id": contract.contract_id,
        "origin_location_id": contract.origin_location_id,
        "destination_location_id": contract.destination_location_id,
        "volume_units": contract.volume_units,
        "collateral_units": contract.collateral_units,
        "reward_units": contract.reward_units,
        "date_expired": contract.date_expired.isoformat(),
        "days_to_complete": contract.days_to_complete,
        "title": contract.title,
        "date_issued": contract.date_issued.isoformat() if contract.date_issued else None,
    }


def snapshot_to_dict(snapshot: ContractSnapshot) -> dict[str, Any]:
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "fetched_at": snapshot.fetched_at.isoformat(),
        "compatibility_date": snapshot.compatibility_date,
        "sde_build_number": snapshot.sde_build_number,
        "region_ids": list(snapshot.region_ids),
        "contracts": [contract_to_dict(contract) for contract in snapshot.contracts],
        "system_kills_fetched_at": (
            snapshot.system_kills_fetched_at.isoformat()
            if snapshot.system_kills_fetched_at is not None
            else None
        ),
        "system_kill_activity": [
            {
                "system_id": item.system_id,
                "ship_kills": item.ship_kills,
                "pod_kills": item.pod_kills,
                "npc_kills": item.npc_kills,
            }
            for item in snapshot.system_kill_activity
        ],
        "threat_intel": (
            {
                "source": "zkillboard",
                "fetched_at": snapshot.threat_intel_fetched_at.isoformat(),
                "window_seconds": snapshot.threat_window_seconds,
                "gate_radius_m": snapshot.threat_gate_radius_m,
                "coverage_region_ids": list(snapshot.threat_coverage_region_ids),
                "incomplete_region_ids": list(snapshot.threat_incomplete_region_ids),
                "killmails_seen": snapshot.threat_killmails_seen,
                "gate_events": [
                    {
                        "killmail_id": item.killmail_id,
                        "occurred_at": item.occurred_at.isoformat(),
                        "system_id": item.system_id,
                        "region_id": item.region_id,
                        "gate_id": item.gate_id,
                        "distance_to_gate_m": item.distance_to_gate_m,
                        "evidence": item.evidence.value,
                        "categories": sorted(category.value for category in item.categories),
                        "victim_ship_type_id": item.victim_ship_type_id,
                        "attacker_ship_type_ids": list(item.attacker_ship_type_ids),
                        "attacker_weapon_type_ids": list(item.attacker_weapon_type_ids),
                        "player_attacker_count": item.player_attacker_count,
                        "zkill_labels": list(item.zkill_labels),
                    }
                    for item in snapshot.gate_threat_events
                ],
            }
            if snapshot.threat_intel_fetched_at is not None
            else None
        ),
    }


def snapshot_from_dict(payload: dict[str, object]) -> ContractSnapshot:
    schema_version = json_int(payload.get("schema_version"), "snapshot schema_version")
    if schema_version not in {1, SNAPSHOT_SCHEMA_VERSION}:
        raise ValueError(f"unsupported snapshot schema: {payload.get('schema_version')!r}")
    raw_contracts = payload.get("contracts")
    if not isinstance(raw_contracts, list):
        raise ValueError("snapshot contracts must be a list")
    contracts: list[PublicCourierContract] = []
    for raw in raw_contracts:
        if not isinstance(raw, dict):
            raise ValueError("snapshot contract must be an object")
        contracts.append(contract_from_dict(cast(dict[str, object], raw)))
    raw_activity = payload.get("system_kill_activity", [])
    if not isinstance(raw_activity, list):
        raise ValueError("snapshot system_kill_activity must be a list")
    activity: list[SystemKillActivity] = []
    for raw in raw_activity:
        if not isinstance(raw, dict):
            raise ValueError("snapshot system-kill activity must be an object")
        row = cast(dict[str, object], raw)
        activity.append(
            SystemKillActivity(
                system_id=json_int(row["system_id"], "system_id"),
                ship_kills=json_int(row["ship_kills"], "ship_kills"),
                pod_kills=json_int(row["pod_kills"], "pod_kills"),
                npc_kills=json_int(row["npc_kills"], "npc_kills"),
            )
        )
    raw_activity_time = payload.get("system_kills_fetched_at")
    raw_threat = payload.get("threat_intel") if schema_version == SNAPSHOT_SCHEMA_VERSION else None
    threat_time: datetime | None = None
    threat_window: int | None = None
    threat_radius: int | None = None
    threat_coverage: tuple[int, ...] = ()
    threat_incomplete: tuple[int, ...] = ()
    threat_killmails_seen = 0
    threat_events: list[GateThreatEvent] = []
    if raw_threat is not None:
        if not isinstance(raw_threat, dict):
            raise ValueError("snapshot threat_intel must be an object or null")
        threat = cast(dict[str, object], raw_threat)
        if threat.get("source") != "zkillboard":
            raise ValueError("unsupported threat-intel source")
        threat_time = parse_esi_datetime(json_string(threat["fetched_at"], "fetched_at"))
        threat_window = json_int(threat["window_seconds"], "window_seconds")
        threat_radius = json_int(threat["gate_radius_m"], "gate_radius_m")
        threat_coverage = tuple(
            json_int(value, "array entry")
            for value in json_array(threat.get("coverage_region_ids", []), "coverage_region_ids")
        )
        threat_incomplete = tuple(
            json_int(value, "array entry")
            for value in json_array(
                threat.get("incomplete_region_ids", []), "incomplete_region_ids"
            )
        )
        threat_killmails_seen = json_int(threat.get("killmails_seen", 0), "killmails_seen")
        raw_events = threat.get("gate_events", [])
        if not isinstance(raw_events, list):
            raise ValueError("snapshot gate_events must be a list")
        for raw_event in raw_events:
            if not isinstance(raw_event, dict):
                raise ValueError("snapshot gate-threat event must be an object")
            event = cast(dict[str, object], raw_event)
            threat_events.append(
                GateThreatEvent(
                    killmail_id=json_int(event["killmail_id"], "killmail_id"),
                    occurred_at=parse_esi_datetime(
                        json_string(event["occurred_at"], "occurred_at")
                    ),
                    system_id=json_int(event["system_id"], "system_id"),
                    region_id=json_int(event["region_id"], "region_id"),
                    gate_id=json_int(event["gate_id"], "gate_id"),
                    distance_to_gate_m=json_int(event["distance_to_gate_m"], "distance_to_gate_m"),
                    evidence=GateEvidence(json_string(event["evidence"], "evidence")),
                    categories=frozenset(
                        ThreatCategory(json_string(value, "array entry"))
                        for value in json_array(event["categories"], "categories")
                    ),
                    victim_ship_type_id=json_int(
                        event["victim_ship_type_id"], "victim_ship_type_id"
                    ),
                    attacker_ship_type_ids=tuple(
                        json_int(value, "array entry")
                        for value in json_array(
                            event.get("attacker_ship_type_ids", []), "attacker_ship_type_ids"
                        )
                    ),
                    attacker_weapon_type_ids=tuple(
                        json_int(value, "array entry")
                        for value in json_array(
                            event.get("attacker_weapon_type_ids", []), "attacker_weapon_type_ids"
                        )
                    ),
                    player_attacker_count=json_int(
                        event["player_attacker_count"], "player_attacker_count"
                    ),
                    zkill_labels=tuple(
                        json_string(value, "array entry")
                        for value in json_array(event.get("zkill_labels", []), "zkill_labels")
                    ),
                )
            )
    return ContractSnapshot(
        fetched_at=parse_esi_datetime(json_string(payload["fetched_at"], "fetched_at")),
        compatibility_date=json_string(payload["compatibility_date"], "compatibility_date"),
        sde_build_number=json_int(payload["sde_build_number"], "sde_build_number"),
        region_ids=tuple(
            json_int(value, "array entry")
            for value in json_array(payload["region_ids"], "region_ids")
        ),
        contracts=tuple(contracts),
        system_kills_fetched_at=(
            parse_esi_datetime(json_string(raw_activity_time, "system_kills_fetched_at"))
            if raw_activity_time is not None
            else None
        ),
        system_kill_activity=tuple(activity),
        threat_intel_fetched_at=threat_time,
        threat_window_seconds=threat_window,
        threat_gate_radius_m=threat_radius,
        threat_coverage_region_ids=threat_coverage,
        threat_incomplete_region_ids=threat_incomplete,
        threat_killmails_seen=threat_killmails_seen,
        gate_threat_events=tuple(threat_events),
    )


def write_snapshot(path: Path, snapshot: ContractSnapshot) -> None:
    write_json(path, snapshot_to_dict(snapshot))


def read_snapshot(path: Path) -> ContractSnapshot:
    try:
        return snapshot_from_dict(read_json_object(path))
    except KeyError as error:
        raise ValueError(f"{path}: missing required field {error.args[0]!r}") from error


def contract_from_dict(row: dict[str, object]) -> PublicCourierContract:
    issued = row.get("date_issued")
    return PublicCourierContract(
        contract_id=json_int(row["contract_id"], "contract_id"),
        origin_location_id=json_int(row["origin_location_id"], "origin_location_id"),
        destination_location_id=json_int(row["destination_location_id"], "destination_location_id"),
        volume_units=json_int(row["volume_units"], "volume_units"),
        collateral_units=json_int(row["collateral_units"], "collateral_units"),
        reward_units=json_int(row["reward_units"], "reward_units"),
        date_expired=parse_esi_datetime(json_string(row["date_expired"], "date_expired")),
        days_to_complete=json_int(row["days_to_complete"], "days_to_complete"),
        title=json_string(row.get("title", ""), "title"),
        date_issued=parse_esi_datetime(json_string(issued, "date_issued"))
        if issued is not None
        else None,
    )
