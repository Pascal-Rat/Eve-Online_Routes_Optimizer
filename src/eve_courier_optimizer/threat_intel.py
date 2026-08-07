"""Polite zKillboard ingestion and gate-focused threat classification.

The public zKill API supplies killmail-level observations that CCP's aggregate system-kill feed
cannot. This module deliberately narrows those observations to player-caused losses at stargates;
it does not turn unrelated station, belt, structure, or NPC losses into route danger.
"""

from __future__ import annotations

import gzip
import json
import math
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Final, cast

from .domain import GateEvidence, GateThreatEvent, ThreatCategory, parse_esi_datetime
from .esi import CacheRecord, EsiResponseCache, Transport, UrllibTransport
from .sde import Stargate, UniverseGraph

ZKILL_BASE_URL: Final = "https://zkillboard.com"
ZKILL_MAX_PAST_SECONDS: Final = 604_800
ZKILL_MAX_ROWS: Final = 1_000
# Gate-danger mode is primarily an active-threat signal. Two hours keeps recent camps/ganks useful
# without turning a kill from much earlier in the day into a current hard avoid. Operators can still
# request up to seven days when they deliberately want a broader historical observation.
DEFAULT_THREAT_WINDOW_SECONDS: Final = 7_200
DEFAULT_GATE_RADIUS_M: Final = 250_000
DEFAULT_CACHE_SECONDS: Final = 900
DEFAULT_REQUEST_SPACING_SECONDS: Final = 1.1
DEFAULT_ZKILL_USER_AGENT: Final = (
    "eve-courier-route-optimizer/1.5.0 (+local EVE route planning; zKill threat snapshots)"
)

_SMARTBOMB_GROUP: Final = 72
_HEAVY_INTERDICTOR_GROUP: Final = 894
_CARRIER_GROUPS: Final = frozenset({547, 659, 5120})
_HAULER_GROUPS: Final = frozenset({28, 380, 513, 883, 902, 941, 1202})


class ZkillError(RuntimeError):
    """Base error for zKillboard access or payload validation."""


class ZkillHttpError(ZkillError):
    def __init__(self, status: int, url: str, body: bytes) -> None:
        preview = body[:300].decode("utf-8", errors="replace")
        super().__init__(f"zKillboard returned HTTP {status} for {url}: {preview}")
        self.status = status
        self.url = url


@dataclass(frozen=True, slots=True)
class ThreatIntelCollection:
    fetched_at: datetime
    window_seconds: int
    gate_radius_m: int
    coverage_region_ids: tuple[int, ...]
    incomplete_region_ids: tuple[int, ...]
    events: tuple[GateThreatEvent, ...]
    killmails_seen: int


class ZkillClient:
    """Conservative zKill REST client with local TTL caching and request spacing."""

    def __init__(
        self,
        *,
        transport: Transport | None = None,
        cache: EsiResponseCache | None = None,
        user_agent: str = DEFAULT_ZKILL_USER_AGENT,
        timeout_seconds: float = 30.0,
        max_retries: int = 3,
        cache_seconds: int = DEFAULT_CACHE_SECONDS,
        request_spacing_seconds: float = DEFAULT_REQUEST_SPACING_SECONDS,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], float] = time.time,
    ) -> None:
        if cache_seconds <= 0:
            raise ValueError("zKill cache lifetime must be positive")
        if request_spacing_seconds < 0:
            raise ValueError("zKill request spacing cannot be negative")
        self.transport = transport or UrllibTransport()
        self.cache = cache
        self.user_agent = user_agent
        self.timeout_seconds = timeout_seconds
        self.max_retries = max_retries
        self.cache_seconds = cache_seconds
        self.request_spacing_seconds = request_spacing_seconds
        self.sleep = sleep
        self.now = now
        self._last_network_request_epoch: float | None = None

    @staticmethod
    def _body(response_body: bytes, headers: Mapping[str, str]) -> bytes:
        if headers.get("content-encoding", "").casefold() == "gzip":
            try:
                return gzip.decompress(response_body)
            except OSError as error:
                raise ZkillError("zKillboard returned invalid gzip data") from error
        return response_body

    def _space_request(self) -> None:
        if self._last_network_request_epoch is None:
            return
        wait = self.request_spacing_seconds - (self.now() - self._last_network_request_epoch)
        if wait > 0:
            self.sleep(wait)

    def region_losses(
        self,
        region_id: int,
        *,
        past_seconds: int = DEFAULT_THREAT_WINDOW_SECONDS,
    ) -> tuple[dict[str, Any], ...]:
        """Fetch recent losses located in one region.

        zKill requires ``pastSeconds`` to be an hourly multiple and caps it at seven days. The
        endpoint's 1,000-row ceiling is reported by the collection layer as incomplete coverage.
        """

        if region_id <= 0:
            raise ValueError("zKill region ID must be positive")
        if (
            past_seconds <= 0
            or past_seconds > ZKILL_MAX_PAST_SECONDS
            or past_seconds % 3_600 != 0
        ):
            raise ValueError("zKill lookback must be an hourly multiple from 1 hour through 7 days")
        url = (
            f"{ZKILL_BASE_URL}/api/losses/regionID/{region_id}/"
            f"pastSeconds/{past_seconds}/"
        )
        cached = self.cache.get(url) if self.cache is not None else None
        if cached is not None and cached.expires_epoch > self.now():
            return self._parse_rows(cached.body)

        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "User-Agent": self.user_agent,
        }
        for attempt in range(self.max_retries + 1):
            self._space_request()
            self._last_network_request_epoch = self.now()
            try:
                response = self.transport.get(url, headers, self.timeout_seconds)
            except OSError as error:
                if attempt < self.max_retries:
                    self.sleep(float(min(2**attempt, 8)))
                    continue
                raise ZkillError(f"zKillboard network request failed for {url}") from error
            body = self._body(response.body, response.headers)
            if 200 <= response.status < 300:
                if self.cache is not None:
                    self.cache.put(
                        CacheRecord(
                            url=url,
                            etag=response.headers.get("etag"),
                            expires_epoch=self.now() + self.cache_seconds,
                            body=body,
                            headers_json=json.dumps(dict(response.headers), sort_keys=True),
                        )
                    )
                return self._parse_rows(body)
            if response.status == 429 and attempt < self.max_retries:
                try:
                    retry_after = float(response.headers.get("retry-after", "5"))
                except ValueError:
                    retry_after = 5.0
                self.sleep(max(1.0, retry_after))
                continue
            if response.status >= 500 and attempt < self.max_retries:
                self.sleep(float(min(2**attempt, 8)))
                continue
            raise ZkillHttpError(response.status, url, body)
        raise AssertionError("zKill retry loop must return or raise")

    @staticmethod
    def _parse_rows(body: bytes) -> tuple[dict[str, Any], ...]:
        try:
            payload = json.loads(body, parse_float=Decimal)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ZkillError("zKillboard response was not valid JSON") from error
        if not isinstance(payload, list):
            raise ZkillError("zKillboard region response was not a list")
        return tuple(cast(dict[str, Any], row) for row in payload if isinstance(row, dict))


def _positive_ids(values: Iterable[object]) -> tuple[int, ...]:
    result: set[int] = set()
    for value in values:
        try:
            parsed = int(cast(Any, value))
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            result.add(parsed)
    return tuple(sorted(result))


def _gate_for_killmail(
    row: Mapping[str, Any],
    graph: UniverseGraph,
    *,
    system_id: int,
    maximum_distance_m: int,
) -> tuple[Stargate, float, GateEvidence] | None:
    zkb = row.get("zkb")
    if isinstance(zkb, Mapping):
        try:
            location_id = int(cast(Any, zkb.get("locationID", 0)))
        except (TypeError, ValueError):
            location_id = 0
        exact_gate = graph.gates.get(location_id)
        if exact_gate is not None and exact_gate.system_id == system_id:
            return exact_gate, 0.0, GateEvidence.ZKILL_LOCATION

    victim = row.get("victim")
    if not isinstance(victim, Mapping):
        return None
    position = victim.get("position")
    if not isinstance(position, Mapping):
        return None
    try:
        coordinates = (
            float(cast(Any, position["x"])),
            float(cast(Any, position["y"])),
            float(cast(Any, position["z"])),
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if not all(math.isfinite(value) for value in coordinates):
        return None
    nearest = graph.nearest_gate(
        system_id,
        coordinates,
        maximum_distance_m=float(maximum_distance_m),
    )
    if nearest is None:
        return None
    return nearest[0], nearest[1], GateEvidence.VICTIM_POSITION


def classify_gate_threat(
    row: Mapping[str, Any],
    graph: UniverseGraph,
    *,
    maximum_distance_m: int = DEFAULT_GATE_RADIUS_M,
) -> GateThreatEvent | None:
    """Classify one killmail, returning ``None`` unless it is player PvP near a gate."""

    try:
        killmail_id = int(cast(Any, row["killmail_id"]))
        system_id = int(cast(Any, row["solar_system_id"]))
        occurred_at = parse_esi_datetime(str(row["killmail_time"]))
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    system = graph.systems.get(system_id)
    if system is None:
        return None
    zkb = row.get("zkb")
    if not isinstance(zkb, Mapping) or bool(zkb.get("npc", False)):
        # This excludes pure NPC losses, including CONCORD destroying a ganker. Such a loss may be
        # evidence used by zKill's own post-processing, but it is not itself danger to a courier.
        return None
    attackers_raw = row.get("attackers")
    if not isinstance(attackers_raw, list):
        return None
    player_attackers = [
        cast(Mapping[str, Any], attacker)
        for attacker in attackers_raw
        if isinstance(attacker, Mapping) and attacker.get("character_id") is not None
    ]
    if not player_attackers:
        return None
    gate_match = _gate_for_killmail(
        row,
        graph,
        system_id=system_id,
        maximum_distance_m=maximum_distance_m,
    )
    if gate_match is None:
        return None

    victim = row.get("victim")
    if not isinstance(victim, Mapping):
        return None
    try:
        victim_ship_type_id = int(cast(Any, victim["ship_type_id"]))
    except (KeyError, TypeError, ValueError):
        return None
    ship_type_ids = _positive_ids(attacker.get("ship_type_id") for attacker in player_attackers)
    weapon_type_ids = _positive_ids(
        attacker.get("weapon_type_id") for attacker in player_attackers
    )
    ship_groups = {
        group.group_id
        for type_id in ship_type_ids
        if (group := graph.item_group(type_id)) is not None
    }
    weapon_groups = {
        group.group_id
        for type_id in weapon_type_ids
        if (group := graph.item_group(type_id)) is not None
    }
    victim_group = graph.item_group(victim_ship_type_id)
    labels_raw = zkb.get("labels", [])
    labels = tuple(
        sorted({str(label) for label in labels_raw})
        if isinstance(labels_raw, list)
        else ()
    )

    categories = {ThreatCategory.ANY_GATE_PVP}
    if "ganked" in labels:
        categories.add(ThreatCategory.SUICIDE_GANK)
    if _SMARTBOMB_GROUP in weapon_groups:
        categories.add(ThreatCategory.SMARTBOMB)
    if _HEAVY_INTERDICTOR_GROUP in ship_groups:
        categories.add(ThreatCategory.HEAVY_INTERDICTOR)
    if ship_groups & _CARRIER_GROUPS:
        categories.add(ThreatCategory.CARRIER)
    if len(player_attackers) >= 2:
        categories.add(ThreatCategory.GATE_CAMP)
    if victim_group is not None and victim_group.group_id in _HAULER_GROUPS:
        categories.add(ThreatCategory.HAULER_LOSS)

    gate, distance, evidence = gate_match
    return GateThreatEvent(
        killmail_id=killmail_id,
        occurred_at=occurred_at,
        system_id=system_id,
        region_id=system.region_id,
        gate_id=gate.gate_id,
        distance_to_gate_m=int(round(distance)),
        evidence=evidence,
        categories=frozenset(categories),
        victim_ship_type_id=victim_ship_type_id,
        attacker_ship_type_ids=ship_type_ids,
        attacker_weapon_type_ids=weapon_type_ids,
        player_attacker_count=len(player_attackers),
        zkill_labels=labels,
    )


def collect_gate_threat_intel(
    client: ZkillClient,
    graph: UniverseGraph,
    region_ids: Iterable[int],
    *,
    window_seconds: int = DEFAULT_THREAT_WINDOW_SECONDS,
    gate_radius_m: int = DEFAULT_GATE_RADIUS_M,
    clock: Callable[[], datetime] | None = None,
) -> ThreatIntelCollection:
    """Collect a bounded, auditable gate-threat observation for selected regions."""

    if gate_radius_m < 0:
        raise ValueError("gate radius cannot be negative")
    now = (clock or (lambda: datetime.now(UTC)))()
    if now.tzinfo is None:
        raise ValueError("threat-intel clock must return a timezone-aware datetime")
    observed_at = now.astimezone(UTC)
    cutoff = observed_at - timedelta(seconds=window_seconds)
    coverage: list[int] = []
    incomplete: set[int] = set()
    events_by_id: dict[int, GateThreatEvent] = {}
    killmails_seen = 0
    for region_id in sorted(set(region_ids)):
        try:
            rows = client.region_losses(region_id, past_seconds=window_seconds)
        except ZkillError:
            incomplete.add(region_id)
            continue
        coverage.append(region_id)
        killmails_seen += len(rows)
        if len(rows) >= ZKILL_MAX_ROWS:
            incomplete.add(region_id)
        for row in rows:
            event = classify_gate_threat(
                row,
                graph,
                maximum_distance_m=gate_radius_m,
            )
            if event is None or event.occurred_at < cutoff or event.occurred_at > observed_at:
                continue
            events_by_id[event.killmail_id] = event
    return ThreatIntelCollection(
        fetched_at=observed_at,
        window_seconds=window_seconds,
        gate_radius_m=gate_radius_m,
        coverage_region_ids=tuple(coverage),
        incomplete_region_ids=tuple(sorted(incomplete)),
        events=tuple(
            sorted(events_by_id.values(), key=lambda item: (item.occurred_at, item.killmail_id))
        ),
        killmails_seen=killmails_seen,
    )


def threat_avoided_systems(
    events: Iterable[GateThreatEvent],
    categories: frozenset[ThreatCategory],
    *,
    minimum_events: int,
    exempt_system_ids: frozenset[int] = frozenset(),
) -> frozenset[int]:
    """Derive hard avoids from matching event counts; category overlaps count once per killmail."""

    if not categories:
        raise ValueError("select at least one gate-threat category")
    if minimum_events <= 0:
        raise ValueError("minimum gate-threat events must be positive")
    counts: Counter[int] = Counter()
    for event in events:
        if event.categories & categories:
            counts[event.system_id] += 1
    return frozenset(
        system_id
        for system_id, count in counts.items()
        if count >= minimum_events and system_id not in exempt_system_ids
    )


def default_zkill_cache_path() -> Path:
    return Path.home() / ".cache" / "eve-courier-route-optimizer" / "zkill.sqlite3"
