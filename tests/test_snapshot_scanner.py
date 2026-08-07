from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from threading import Barrier, Lock

import pytest

from eve_courier_optimizer.domain import (
    GateEvidence,
    GateThreatEvent,
    PublicCourierContract,
    SystemKillActivity,
    ThreatCategory,
)
from eve_courier_optimizer.esi import EsiClient, HttpResponse
from eve_courier_optimizer.scanner import scan_public_couriers
from eve_courier_optimizer.sde import Region, UniverseGraph
from eve_courier_optimizer.snapshot import ContractSnapshot, read_snapshot, write_snapshot
from eve_courier_optimizer.threat_intel import ZkillClient

from .conftest import make_contract, make_snapshot


class OnePageTransport:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def get(self, url: str, headers: Mapping[str, str], timeout_seconds: float) -> HttpResponse:
        del url, headers, timeout_seconds
        return HttpResponse(200, {"x-pages": "1"}, self.body)


class ContractAndActivityTransport:
    def __init__(self, contract_body: bytes, *, activity_status: int = 200) -> None:
        self.contract_body = contract_body
        self.activity_status = activity_status

    def get(self, url: str, headers: Mapping[str, str], timeout_seconds: float) -> HttpResponse:
        del headers, timeout_seconds
        if url.endswith("/universe/system_kills/"):
            body = json.dumps(
                [{"system_id": 2, "ship_kills": 7, "pod_kills": 1, "npc_kills": 3}]
            ).encode()
            return HttpResponse(self.activity_status, {}, body)
        return HttpResponse(200, {"x-pages": "1"}, self.contract_body)


def test_snapshot_roundtrip(now: datetime, tmp_path: Path) -> None:
    threat_event = GateThreatEvent(
        killmail_id=88,
        occurred_at=now,
        system_id=2,
        region_id=10,
        gate_id=500,
        distance_to_gate_m=42,
        evidence=GateEvidence.VICTIM_POSITION,
        categories=frozenset({ThreatCategory.SMARTBOMB, ThreatCategory.ANY_GATE_PVP}),
        victim_ship_type_id=3001,
        attacker_ship_type_ids=(1001,),
        attacker_weapon_type_ids=(2001,),
        player_attacker_count=1,
        zkill_labels=("pvp",),
    )
    snapshot = replace(
        make_snapshot(now, make_contract(now, 1, 101, 102)),
        system_kills_fetched_at=now,
        system_kill_activity=(SystemKillActivity(2, 7, 1, 3),),
        threat_intel_fetched_at=now,
        threat_window_seconds=86_400,
        threat_gate_radius_m=250_000,
        threat_coverage_region_ids=(10,),
        threat_killmails_seen=12,
        gate_threat_events=(threat_event,),
    )
    path = tmp_path / "snapshot.json"
    write_snapshot(path, snapshot)
    assert read_snapshot(path) == snapshot


def test_snapshot_rejects_duplicate_contract_ids(now: datetime) -> None:
    contract = make_contract(now, 1, 101, 102)
    with pytest.raises(ValueError, match="unique"):
        ContractSnapshot(now, "2026-08-05", 1, (10,), (contract, contract))

    with pytest.raises(ValueError, match="requires its fetched-at"):
        replace(
            make_snapshot(now, contract),
            system_kill_activity=(SystemKillActivity(2, 1, 0, 0),),
        )
    with pytest.raises(ValueError, match="activity IDs"):
        replace(
            make_snapshot(now, contract),
            system_kills_fetched_at=now,
            system_kill_activity=(
                SystemKillActivity(2, 1, 0, 0),
                SystemKillActivity(2, 2, 0, 0),
            ),
        )


def test_scanner_builds_reproducible_snapshot(now: datetime, tiny_graph: UniverseGraph) -> None:
    payload = [
        {
            "contract_id": 1,
            "start_location_id": 101,
            "end_location_id": 102,
            "volume": 1.0,
            "collateral": 100.0,
            "reward": 200.0,
            "date_expired": "2026-08-06T12:00:00Z",
            "days_to_complete": 1,
            "type": "courier",
        }
    ]
    client = EsiClient(transport=OnePageTransport(json.dumps(payload).encode()))
    snapshot = scan_public_couriers(client, tiny_graph, [10], clock=lambda: now)
    assert snapshot.fetched_at == now
    assert snapshot.sde_build_number == 1
    assert [item.contract_id for item in snapshot.contracts] == [1]


def test_scanner_uses_bounded_region_concurrency(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    graph = UniverseGraph(
        systems=tiny_graph.systems,
        adjacency=tiny_graph.adjacency,
        station_systems=tiny_graph.station_systems,
        regions={**tiny_graph.regions, 11: Region(11, "Other Region")},
        metadata=tiny_graph.metadata,
    )

    class ConcurrentClient(EsiClient):
        def __init__(self) -> None:
            super().__init__(max_retries=0)
            self.barrier = Barrier(2)
            self.lock = Lock()
            self.active = 0
            self.peak = 0

        def public_couriers(self, region_id: int) -> tuple[PublicCourierContract, ...]:
            del region_id
            with self.lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
            self.barrier.wait(timeout=1)
            with self.lock:
                self.active -= 1
            return ()

    client = ConcurrentClient()
    snapshot = scan_public_couriers(
        client,
        graph,
        [10, 11],
        clock=lambda: now,
        contract_workers=2,
    )
    assert snapshot.region_ids == (10, 11)
    assert client.peak == 2

    with pytest.raises(ValueError, match="workers must be between"):
        scan_public_couriers(client, graph, [10], clock=lambda: now, contract_workers=9)


def test_scanner_optionally_captures_activity_without_making_it_a_hard_dependency(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    contract_payload = json.dumps(
        [
            {
                "contract_id": 1,
                "start_location_id": 101,
                "end_location_id": 102,
                "volume": 1.0,
                "collateral": 100.0,
                "reward": 200.0,
                "date_expired": "2026-08-06T12:00:00Z",
                "days_to_complete": 1,
                "type": "courier",
            }
        ]
    ).encode()
    available = scan_public_couriers(
        EsiClient(transport=ContractAndActivityTransport(contract_payload)),
        tiny_graph,
        [10],
        clock=lambda: now,
        include_system_kills=True,
    )
    assert available.system_kills_fetched_at == now
    assert available.system_kill_activity == (SystemKillActivity(2, 7, 1, 3),)

    unavailable = scan_public_couriers(
        EsiClient(
            transport=ContractAndActivityTransport(contract_payload, activity_status=403),
            max_retries=0,
        ),
        tiny_graph,
        [10],
        clock=lambda: now,
        include_system_kills=True,
    )
    assert unavailable.contracts
    assert unavailable.system_kills_fetched_at is None
    assert unavailable.system_kill_activity == ()


def test_scanner_records_an_empty_but_successful_threat_observation(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    class EmptyZkillClient(ZkillClient):
        def region_losses(
            self,
            region_id: int,
            *,
            past_seconds: int = 86_400,
        ) -> tuple[dict[str, object], ...]:
            assert region_id == 10
            assert past_seconds == 43_200
            return ()

    snapshot = scan_public_couriers(
        EsiClient(transport=OnePageTransport(b"[]")),
        tiny_graph,
        [10],
        clock=lambda: now,
        zkill=EmptyZkillClient(),
        include_threat_intel=True,
        threat_window_seconds=43_200,
        threat_gate_radius_m=100_000,
    )
    assert snapshot.threat_intel_fetched_at == now
    assert snapshot.threat_window_seconds == 43_200
    assert snapshot.threat_gate_radius_m == 100_000
    assert snapshot.threat_coverage_region_ids == (10,)
    assert snapshot.threat_incomplete_region_ids == ()
    assert snapshot.gate_threat_events == ()


def test_scanner_can_collect_threats_for_a_distinct_transit_scope(
    now: datetime,
    tiny_graph: UniverseGraph,
) -> None:
    graph = UniverseGraph(
        systems=tiny_graph.systems,
        adjacency=tiny_graph.adjacency,
        station_systems=tiny_graph.station_systems,
        regions={**tiny_graph.regions, 11: Region(11, "Transit Region")},
        metadata=tiny_graph.metadata,
    )

    class RecordingZkillClient(ZkillClient):
        def __init__(self) -> None:
            super().__init__()
            self.regions: list[int] = []

        def region_losses(
            self,
            region_id: int,
            *,
            past_seconds: int = 7_200,
        ) -> tuple[dict[str, object], ...]:
            assert past_seconds == 7_200
            self.regions.append(region_id)
            return ()

    zkill = RecordingZkillClient()
    snapshot = scan_public_couriers(
        EsiClient(transport=OnePageTransport(b"[]")),
        graph,
        [10],
        clock=lambda: now,
        zkill=zkill,
        include_threat_intel=True,
        threat_region_ids=[11],
    )
    assert zkill.regions == [11]
    assert snapshot.region_ids == (10,)
    assert snapshot.threat_coverage_region_ids == (11,)
