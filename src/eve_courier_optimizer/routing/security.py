"""Resolve operator security choices against observed activity and gate threats."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable

from eve_courier_optimizer.domain import (
    ContractSnapshot,
    GateThreatEvent,
    SecurityBand,
    SecurityPolicy,
    ThreatCategory,
    parse_esi_datetime,
)
from eve_courier_optimizer.jsonio import json_array, json_int, json_string, optional_int
from eve_courier_optimizer.routing.universe import UniverseGraph


def observed_security_policy(
    snapshot: ContractSnapshot,
    *,
    minimum_security: float | None = None,
    allowed_bands: frozenset[SecurityBand] | None = None,
    avoided_system_ids: frozenset[int] = frozenset(),
    activity_threshold: int | None = None,
    threat_categories: frozenset[ThreatCategory] = frozenset(),
    threat_min_events: int | None = None,
    exempt_system_ids: frozenset[int] = frozenset(),
) -> SecurityPolicy:
    if activity_threshold is not None and snapshot.system_kills_fetched_at is None:
        raise ValueError("gank awareness requires system-kill activity; scan or refresh first")
    if threat_categories and snapshot.threat_intel_fetched_at is None:
        raise ValueError("gate-threat awareness requires zKill intel; scan or refresh first")
    if threat_categories and threat_min_events is None:
        raise ValueError("gate-threat awareness requires a minimum event count")
    return SecurityPolicy(
        minimum_security=minimum_security,
        allowed_bands=allowed_bands,
        avoided_system_ids=avoided_system_ids,
        gank_ship_kill_threshold=activity_threshold,
        gank_avoided_system_ids=frozenset(
            activity.system_id
            for activity in snapshot.system_kill_activity
            if activity_threshold is not None
            and activity.ship_kills >= activity_threshold
            and activity.system_id not in exempt_system_ids
        ),
        gank_activity_fetched_at=snapshot.system_kills_fetched_at
        if activity_threshold is not None
        else None,
        threat_categories=threat_categories,
        threat_min_events=threat_min_events if threat_categories else None,
        threat_avoided_system_ids=threat_avoided_systems(
            snapshot.gate_threat_events,
            threat_categories,
            minimum_events=threat_min_events if threat_min_events is not None else 1,
            exempt_system_ids=exempt_system_ids,
        )
        if threat_categories
        else frozenset(),
        threat_intel_fetched_at=snapshot.threat_intel_fetched_at if threat_categories else None,
        threat_window_seconds=snapshot.threat_window_seconds if threat_categories else None,
        threat_gate_radius_m=snapshot.threat_gate_radius_m if threat_categories else None,
        threat_coverage_region_ids=frozenset(snapshot.threat_coverage_region_ids)
        if threat_categories
        else frozenset(),
        threat_incomplete_region_ids=frozenset(snapshot.threat_incomplete_region_ids)
        if threat_categories
        else frozenset(),
    )


def security_policy_to_dict(
    policy: SecurityPolicy, *, bands_key: str = "allowed_bands"
) -> dict[str, object]:
    return {
        "minimum_security": policy.minimum_security,
        "avoided_system_ids": sorted(policy.avoided_system_ids),
        bands_key: sorted(band.value for band in policy.allowed_bands)
        if policy.allowed_bands is not None
        else None,
        "gank_avoided_system_ids": sorted(policy.gank_avoided_system_ids),
        "gank_ship_kill_threshold": policy.gank_ship_kill_threshold,
        "gank_activity_fetched_at": policy.gank_activity_fetched_at.isoformat()
        if policy.gank_activity_fetched_at
        else None,
        "threat_avoided_system_ids": sorted(policy.threat_avoided_system_ids),
        "threat_categories": sorted(category.value for category in policy.threat_categories),
        "threat_min_events": policy.threat_min_events,
        "threat_intel_fetched_at": policy.threat_intel_fetched_at.isoformat()
        if policy.threat_intel_fetched_at
        else None,
        "threat_window_seconds": policy.threat_window_seconds,
        "threat_gate_radius_m": policy.threat_gate_radius_m,
        "threat_coverage_region_ids": sorted(policy.threat_coverage_region_ids),
        "threat_incomplete_region_ids": sorted(policy.threat_incomplete_region_ids),
    }


def security_policy_from_dict(payload: dict[str, object]) -> SecurityPolicy:
    def ids(key: str) -> frozenset[int]:
        return frozenset(json_int(value, key) for value in json_array(payload.get(key, []), key))

    minimum = payload.get("minimum_security")
    if minimum is not None and (isinstance(minimum, bool) or not isinstance(minimum, int | float)):
        raise ValueError("minimum_security must be numeric")
    bands = payload.get("allowed_bands")
    activity_time = payload.get("gank_activity_fetched_at")
    threat_time = payload.get("threat_intel_fetched_at")
    return SecurityPolicy(
        minimum_security=minimum,
        allowed_bands=None
        if bands is None
        else frozenset(
            SecurityBand(json_string(value, "security band"))
            for value in json_array(bands, "allowed_bands")
        ),
        avoided_system_ids=ids("avoided_system_ids"),
        gank_avoided_system_ids=ids("gank_avoided_system_ids"),
        gank_ship_kill_threshold=optional_int(
            payload.get("gank_ship_kill_threshold"), "gank_ship_kill_threshold"
        ),
        gank_activity_fetched_at=parse_esi_datetime(
            json_string(activity_time, "gank_activity_fetched_at")
        )
        if activity_time is not None
        else None,
        threat_avoided_system_ids=ids("threat_avoided_system_ids"),
        threat_categories=frozenset(
            ThreatCategory(json_string(value, "threat category"))
            for value in json_array(payload.get("threat_categories", []), "threat_categories")
        ),
        threat_min_events=optional_int(payload.get("threat_min_events"), "threat_min_events"),
        threat_intel_fetched_at=parse_esi_datetime(
            json_string(threat_time, "threat_intel_fetched_at")
        )
        if threat_time is not None
        else None,
        threat_window_seconds=optional_int(
            payload.get("threat_window_seconds"), "threat_window_seconds"
        ),
        threat_gate_radius_m=optional_int(
            payload.get("threat_gate_radius_m"), "threat_gate_radius_m"
        ),
        threat_coverage_region_ids=ids("threat_coverage_region_ids"),
        threat_incomplete_region_ids=ids("threat_incomplete_region_ids"),
    )


def reachable_threat_regions(
    graph: UniverseGraph,
    *,
    start_system_id: int,
    security: SecurityPolicy,
    horizon_seconds: int,
    seconds_per_jump: int,
) -> tuple[int, ...]:
    """Return a proof-safe pre-threat regional transit envelope."""

    if seconds_per_jump <= 0:
        raise ValueError("seconds_per_jump must be positive")
    # Observed activity/threat avoids can change on refresh, so they must not shrink the next
    # observation envelope. Manual avoids and security-band policy are stable operator choices.
    clean_policy = SecurityPolicy(
        minimum_security=security.minimum_security,
        avoided_system_ids=security.avoided_system_ids,
        allowed_bands=security.allowed_bands,
    )
    regions = graph.reachable_region_ids(
        start_system_id,
        clean_policy,
        max_jumps=horizon_seconds // seconds_per_jump,
    )
    if not regions:
        raise ValueError("start system is outside the selected security policy")
    return tuple(sorted(regions))


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
