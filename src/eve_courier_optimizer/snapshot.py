"""Serializable contract snapshots used to make optimization reproducible."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Final, cast

from .domain import (
    GateEvidence,
    GateThreatEvent,
    PublicCourierContract,
    SystemKillActivity,
    ThreatCategory,
    parse_esi_datetime,
)

SNAPSHOT_SCHEMA_VERSION: Final = 2


@dataclass(frozen=True, slots=True)
class ContractSnapshot:
    fetched_at: datetime
    compatibility_date: str
    sde_build_number: int
    region_ids: tuple[int, ...]
    contracts: tuple[PublicCourierContract, ...]
    system_kills_fetched_at: datetime | None = None
    system_kill_activity: tuple[SystemKillActivity, ...] = ()
    threat_intel_fetched_at: datetime | None = None
    threat_window_seconds: int | None = None
    threat_gate_radius_m: int | None = None
    threat_coverage_region_ids: tuple[int, ...] = ()
    threat_incomplete_region_ids: tuple[int, ...] = ()
    threat_killmails_seen: int = 0
    gate_threat_events: tuple[GateThreatEvent, ...] = ()

    def __post_init__(self) -> None:
        if self.fetched_at.tzinfo is None:
            raise ValueError("snapshot time must be timezone-aware")
        if self.system_kills_fetched_at is not None and self.system_kills_fetched_at.tzinfo is None:
            raise ValueError("system-kill activity time must be timezone-aware")
        if self.system_kill_activity and self.system_kills_fetched_at is None:
            raise ValueError("system-kill activity requires its fetched-at timestamp")
        if self.threat_intel_fetched_at is not None and self.threat_intel_fetched_at.tzinfo is None:
            raise ValueError("threat-intel time must be timezone-aware")
        if self.threat_killmails_seen < 0:
            raise ValueError("threat killmail count cannot be negative")
        if self.threat_window_seconds is not None and self.threat_window_seconds <= 0:
            raise ValueError("threat-intel window must be positive")
        if self.threat_gate_radius_m is not None and self.threat_gate_radius_m < 0:
            raise ValueError("threat-intel gate radius cannot be negative")
        threat_payload_present = bool(
            self.gate_threat_events
            or self.threat_coverage_region_ids
            or self.threat_incomplete_region_ids
            or self.threat_killmails_seen
        )
        if threat_payload_present and self.threat_intel_fetched_at is None:
            raise ValueError("threat-intel payload requires its fetched-at timestamp")
        if self.threat_intel_fetched_at is not None and (
            self.threat_window_seconds is None or self.threat_gate_radius_m is None
        ):
            raise ValueError("threat-intel timestamp requires window and gate radius")
        contract_ids = [contract.contract_id for contract in self.contracts]
        if len(contract_ids) != len(set(contract_ids)):
            raise ValueError("snapshot contract IDs must be unique")
        activity_system_ids = [item.system_id for item in self.system_kill_activity]
        if len(activity_system_ids) != len(set(activity_system_ids)):
            raise ValueError("system-kill activity IDs must be unique")
        killmail_ids = [item.killmail_id for item in self.gate_threat_events]
        if len(killmail_ids) != len(set(killmail_ids)):
            raise ValueError("gate-threat killmail IDs must be unique")
        for label, region_values in (
            ("coverage", self.threat_coverage_region_ids),
            ("incomplete", self.threat_incomplete_region_ids),
        ):
            if any(region_id <= 0 for region_id in region_values):
                raise ValueError(f"threat {label} region IDs must be positive")
            if len(region_values) != len(set(region_values)):
                raise ValueError(f"threat {label} region IDs must be unique")


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


def snapshot_from_dict(payload: dict[str, Any]) -> ContractSnapshot:
    schema_version = payload.get("schema_version")
    if schema_version not in {1, SNAPSHOT_SCHEMA_VERSION}:
        raise ValueError(f"unsupported snapshot schema: {payload.get('schema_version')!r}")
    raw_contracts = payload.get("contracts")
    if not isinstance(raw_contracts, list):
        raise ValueError("snapshot contracts must be a list")
    contracts: list[PublicCourierContract] = []
    for raw in raw_contracts:
        if not isinstance(raw, dict):
            raise ValueError("snapshot contract must be an object")
        contracts.append(contract_from_dict(cast(dict[str, Any], raw)))
    raw_activity = payload.get("system_kill_activity", [])
    if not isinstance(raw_activity, list):
        raise ValueError("snapshot system_kill_activity must be a list")
    activity: list[SystemKillActivity] = []
    for raw in raw_activity:
        if not isinstance(raw, dict):
            raise ValueError("snapshot system-kill activity must be an object")
        row = cast(dict[str, Any], raw)
        activity.append(
            SystemKillActivity(
                system_id=int(row["system_id"]),
                ship_kills=int(row["ship_kills"]),
                pod_kills=int(row["pod_kills"]),
                npc_kills=int(row["npc_kills"]),
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
        threat = cast(dict[str, Any], raw_threat)
        if threat.get("source") != "zkillboard":
            raise ValueError("unsupported threat-intel source")
        threat_time = parse_esi_datetime(str(threat["fetched_at"]))
        threat_window = int(threat["window_seconds"])
        threat_radius = int(threat["gate_radius_m"])
        threat_coverage = tuple(
            int(value) for value in cast(list[Any], threat.get("coverage_region_ids", []))
        )
        threat_incomplete = tuple(
            int(value) for value in cast(list[Any], threat.get("incomplete_region_ids", []))
        )
        threat_killmails_seen = int(threat.get("killmails_seen", 0))
        raw_events = threat.get("gate_events", [])
        if not isinstance(raw_events, list):
            raise ValueError("snapshot gate_events must be a list")
        for raw_event in raw_events:
            if not isinstance(raw_event, dict):
                raise ValueError("snapshot gate-threat event must be an object")
            event = cast(dict[str, Any], raw_event)
            threat_events.append(
                GateThreatEvent(
                    killmail_id=int(event["killmail_id"]),
                    occurred_at=parse_esi_datetime(str(event["occurred_at"])),
                    system_id=int(event["system_id"]),
                    region_id=int(event["region_id"]),
                    gate_id=int(event["gate_id"]),
                    distance_to_gate_m=int(event["distance_to_gate_m"]),
                    evidence=GateEvidence(str(event["evidence"])),
                    categories=frozenset(
                        ThreatCategory(str(value))
                        for value in cast(list[Any], event["categories"])
                    ),
                    victim_ship_type_id=int(event["victim_ship_type_id"]),
                    attacker_ship_type_ids=tuple(
                        int(value)
                        for value in cast(list[Any], event.get("attacker_ship_type_ids", []))
                    ),
                    attacker_weapon_type_ids=tuple(
                        int(value)
                        for value in cast(list[Any], event.get("attacker_weapon_type_ids", []))
                    ),
                    player_attacker_count=int(event["player_attacker_count"]),
                    zkill_labels=tuple(
                        str(value) for value in cast(list[Any], event.get("zkill_labels", []))
                    ),
                )
            )
    return ContractSnapshot(
        fetched_at=parse_esi_datetime(str(payload["fetched_at"])),
        compatibility_date=str(payload["compatibility_date"]),
        sde_build_number=int(payload["sde_build_number"]),
        region_ids=tuple(int(value) for value in cast(list[Any], payload["region_ids"])),
        contracts=tuple(contracts),
        system_kills_fetched_at=(
            parse_esi_datetime(str(raw_activity_time)) if raw_activity_time else None
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
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(snapshot_to_dict(snapshot), indent=2, sort_keys=True) + "\n"
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(serialized, encoding="utf-8")
    temporary.replace(path)


def read_snapshot(path: Path) -> ContractSnapshot:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("snapshot root must be an object")
    return snapshot_from_dict(cast(dict[str, Any], payload))


def contract_from_dict(row: dict[str, Any]) -> PublicCourierContract:
    issued = row.get("date_issued")
    return PublicCourierContract(
        contract_id=int(row["contract_id"]),
        origin_location_id=int(row["origin_location_id"]),
        destination_location_id=int(row["destination_location_id"]),
        volume_units=int(row["volume_units"]),
        collateral_units=int(row["collateral_units"]),
        reward_units=int(row["reward_units"]),
        date_expired=parse_esi_datetime(str(row["date_expired"])),
        days_to_complete=int(row["days_to_complete"]),
        title=str(row.get("title", "")),
        date_issued=parse_esi_datetime(str(issued)) if issued else None,
    )
