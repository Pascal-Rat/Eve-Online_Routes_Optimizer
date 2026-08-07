from __future__ import annotations

import gzip
import json
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eve_courier_optimizer.domain import GateEvidence, ThreatCategory
from eve_courier_optimizer.esi import EsiResponseCache, HttpResponse
from eve_courier_optimizer.sde import (
    Region,
    SdeMetadata,
    SolarSystem,
    Stargate,
    TypeGroup,
    UniverseGraph,
)
from eve_courier_optimizer.threat_intel import (
    ZkillClient,
    ZkillError,
    ZkillHttpError,
    classify_gate_threat,
    collect_gate_threat_intel,
    threat_avoided_systems,
)


class StaticTransport:
    def __init__(self, payload: object, *, gzip_body: bool = False) -> None:
        encoded = json.dumps(payload).encode()
        self.body = gzip.compress(encoded) if gzip_body else encoded
        self.headers = {"content-encoding": "gzip"} if gzip_body else {}
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get(
        self,
        url: str,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> HttpResponse:
        del timeout_seconds
        copied = {str(key): str(value) for key, value in dict(headers).items()}
        self.calls.append((url, copied))
        return HttpResponse(200, self.headers, self.body)


class SequenceTransport:
    def __init__(self, responses: list[HttpResponse]) -> None:
        self.responses = responses
        self.calls = 0

    def get(
        self,
        url: str,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> HttpResponse:
        del url, headers, timeout_seconds
        self.calls += 1
        return self.responses.pop(0)


class FailingTransport:
    def __init__(self) -> None:
        self.calls = 0

    def get(
        self,
        url: str,
        headers: Mapping[str, str],
        timeout_seconds: float,
    ) -> HttpResponse:
        del url, headers, timeout_seconds
        self.calls += 1
        raise OSError("fixture network outage")


@pytest.fixture
def threat_graph() -> UniverseGraph:
    return UniverseGraph(
        systems={
            1: SolarSystem(1, 10, "Gate System", 0.9),
            2: SolarSystem(2, 20, "Other System", 0.2),
        },
        adjacency={1: (2,), 2: (1,)},
        station_systems={},
        regions={10: Region(10, "One"), 20: Region(20, "Two")},
        metadata=SdeMetadata(1, "2026-08-05T00:00:00Z", "test://sde"),
        gates={500: Stargate(500, 1, 0.0, 0.0, 0.0)},
        type_groups={
            72: TypeGroup(72, 7, "Smart Bomb"),
            894: TypeGroup(894, 6, "Heavy Interdiction Cruiser"),
            547: TypeGroup(547, 6, "Carrier"),
            380: TypeGroup(380, 6, "Deep Space Transport"),
        },
        type_group_by_type_id={1001: 894, 1002: 547, 2001: 72, 3001: 380},
    )


def killmail(*, npc: bool = False, location_id: int = 500) -> dict[str, object]:
    return {
        "killmail_id": 9001,
        "killmail_time": "2026-08-05T11:30:00Z",
        "solar_system_id": 1,
        "attackers": [
            {
                "character_id": 11,
                "ship_type_id": 1001,
                "weapon_type_id": 2001,
                "final_blow": True,
            },
            {"character_id": 12, "ship_type_id": 1002, "weapon_type_id": 2001},
        ],
        "victim": {
            "character_id": 20,
            "ship_type_id": 3001,
            "position": {"x": 100_000.0, "y": 0.0, "z": 0.0},
        },
        "zkb": {
            "locationID": location_id,
            "npc": npc,
            "labels": ["ganked", "pvp", "#:2+", "loc:highsec"],
        },
    }


def test_gate_classifier_uses_exact_location_and_specific_categories(
    threat_graph: UniverseGraph,
) -> None:
    event = classify_gate_threat(killmail(), threat_graph)
    assert event is not None
    assert event.evidence is GateEvidence.ZKILL_LOCATION
    assert event.distance_to_gate_m == 0
    assert event.categories == frozenset(ThreatCategory)
    assert event.player_attacker_count == 2


def test_classifier_excludes_concord_npc_and_far_from_gate_losses(
    threat_graph: UniverseGraph,
) -> None:
    assert classify_gate_threat(killmail(npc=True), threat_graph) is None
    far = killmail(location_id=60_000_001)
    victim = far["victim"]
    assert isinstance(victim, dict)
    victim["position"] = {"x": 300_001.0, "y": 0.0, "z": 0.0}
    assert classify_gate_threat(far, threat_graph, maximum_distance_m=250_000) is None

    victim["position"] = {"x": 249_999.0, "y": 0.0, "z": 0.0}
    near = classify_gate_threat(far, threat_graph, maximum_distance_m=250_000)
    assert near is not None
    assert near.evidence is GateEvidence.VICTIM_POSITION
    assert near.distance_to_gate_m == 249_999


def test_zkill_client_honors_gzip_cache_user_agent_and_input_bounds(tmp_path: Path) -> None:
    transport = StaticTransport([killmail()], gzip_body=True)
    clock = [1_000.0]
    client = ZkillClient(
        transport=transport,
        cache=EsiResponseCache(tmp_path / "zkill.sqlite3"),
        now=lambda: clock[0],
        sleep=lambda _seconds: None,
    )
    first = client.region_losses(10, past_seconds=86_400)
    clock[0] += 1
    second = client.region_losses(10, past_seconds=86_400)
    assert first == second
    assert len(transport.calls) == 1
    assert transport.calls[0][0].endswith("/regionID/10/pastSeconds/86400/")
    assert "eve-courier-route-optimizer" in transport.calls[0][1]["User-Agent"]
    assert transport.calls[0][1]["Accept-Encoding"] == "gzip"
    for invalid in (0, 3_601, 604_801):
        with pytest.raises(ValueError, match="hourly multiple"):
            client.region_losses(10, past_seconds=invalid)
    with pytest.raises(ValueError, match="region ID"):
        client.region_losses(0)


def test_zkill_client_retries_transient_errors_and_rejects_bad_responses() -> None:
    sleeps: list[float] = []
    transport = SequenceTransport(
        [
            HttpResponse(429, {"retry-after": "2"}, b"busy"),
            HttpResponse(503, {}, b"later"),
            HttpResponse(200, {}, b"[]"),
        ]
    )
    client = ZkillClient(
        transport=transport,
        sleep=sleeps.append,
        now=lambda: 100.0,
        request_spacing_seconds=0,
    )
    assert client.region_losses(10) == ()
    assert transport.calls == 3
    assert sleeps.count(2.0) == 2

    forbidden = ZkillClient(
        transport=SequenceTransport([HttpResponse(403, {}, b"forbidden")]),
        max_retries=0,
        request_spacing_seconds=0,
    )
    with pytest.raises(ZkillHttpError, match="HTTP 403"):
        forbidden.region_losses(10)

    malformed = ZkillClient(
        transport=SequenceTransport([HttpResponse(200, {}, b"not json")]),
        request_spacing_seconds=0,
    )
    with pytest.raises(ZkillError, match="valid JSON"):
        malformed.region_losses(10)

    wrong_shape = ZkillClient(
        transport=SequenceTransport([HttpResponse(200, {}, b"{}")]),
        request_spacing_seconds=0,
    )
    with pytest.raises(ZkillError, match="was not a list"):
        wrong_shape.region_losses(10)

    invalid_gzip = ZkillClient(
        transport=SequenceTransport(
            [HttpResponse(200, {"content-encoding": "gzip"}, b"not gzip")]
        ),
        request_spacing_seconds=0,
    )
    with pytest.raises(ZkillError, match="invalid gzip"):
        invalid_gzip.region_losses(10)

    network = FailingTransport()
    unavailable = ZkillClient(
        transport=network,
        max_retries=1,
        sleep=lambda _seconds: None,
        request_spacing_seconds=0,
    )
    with pytest.raises(ZkillError, match="network request failed"):
        unavailable.region_losses(10)
    assert network.calls == 2


def test_collection_records_partial_coverage_and_system_thresholds(
    threat_graph: UniverseGraph,
) -> None:
    class PartialClient(ZkillClient):
        def region_losses(
            self,
            region_id: int,
            *,
            past_seconds: int = 86_400,
        ) -> tuple[dict[str, object], ...]:
            del past_seconds
            if region_id == 20:
                raise ZkillError("fixture outage")
            return (killmail(),)

    collection = collect_gate_threat_intel(
        PartialClient(),
        threat_graph,
        [10, 20],
        clock=lambda: datetime(2026, 8, 5, 12, 0, tzinfo=UTC),
    )
    assert collection.coverage_region_ids == (10,)
    assert collection.incomplete_region_ids == (20,)
    assert len(collection.events) == 1
    first = collection.events[0]
    second = replace(first, killmail_id=9002)
    selected = frozenset({ThreatCategory.SMARTBOMB, ThreatCategory.GATE_CAMP})
    assert threat_avoided_systems((first,), selected, minimum_events=2) == frozenset()
    assert threat_avoided_systems((first, second), selected, minimum_events=2) == frozenset({1})
    assert threat_avoided_systems(
        (first, second),
        selected,
        minimum_events=1,
        exempt_system_ids=frozenset({1}),
    ) == frozenset()
    with pytest.raises(ValueError, match="select at least one"):
        threat_avoided_systems((first,), frozenset(), minimum_events=1)
    with pytest.raises(ValueError, match="must be positive"):
        threat_avoided_systems((first,), selected, minimum_events=0)


def test_collection_marks_a_saturated_region_incomplete(
    threat_graph: UniverseGraph,
) -> None:
    class SaturatedClient(ZkillClient):
        def region_losses(
            self,
            region_id: int,
            *,
            past_seconds: int = 86_400,
        ) -> tuple[dict[str, object], ...]:
            del region_id, past_seconds
            return tuple(killmail() for _ in range(1_000))

    collection = collect_gate_threat_intel(
        SaturatedClient(),
        threat_graph,
        [10],
        clock=lambda: datetime(2026, 8, 5, 12, 0, tzinfo=UTC),
    )
    assert collection.killmails_seen == 1_000
    assert collection.incomplete_region_ids == (10,)
    assert len(collection.events) == 1
