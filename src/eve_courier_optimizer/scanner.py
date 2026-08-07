"""Iteration 1: public-contract scanning into reproducible snapshots."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Final

from .domain import PublicCourierContract, SystemKillActivity
from .esi import ESI_COMPATIBILITY_DATE, EsiClient, EsiError
from .sde import UniverseGraph
from .snapshot import ContractSnapshot
from .threat_intel import (
    DEFAULT_GATE_RADIUS_M,
    DEFAULT_THREAT_WINDOW_SECONDS,
    ThreatIntelCollection,
    ZkillClient,
    collect_gate_threat_intel,
)

DEFAULT_CONTRACT_SCAN_WORKERS: Final = 4
MAX_CONTRACT_SCAN_WORKERS: Final = 8


def _scan_contract_regions(
    client: EsiClient,
    region_ids: tuple[int, ...],
    *,
    workers: int,
) -> dict[int, tuple[PublicCourierContract, ...]]:
    """Fetch independent ESI regions concurrently while pagination stays sequential per region."""

    if workers <= 0 or workers > MAX_CONTRACT_SCAN_WORKERS:
        raise ValueError(
            f"contract scan workers must be between 1 and {MAX_CONTRACT_SCAN_WORKERS}"
        )
    if workers == 1 or len(region_ids) == 1:
        return {region_id: client.public_couriers(region_id) for region_id in region_ids}
    with ThreadPoolExecutor(
        max_workers=min(workers, len(region_ids)),
        thread_name_prefix="esi-courier-region",
    ) as executor:
        values = executor.map(client.public_couriers, region_ids)
        return dict(zip(region_ids, values, strict=True))


def scan_public_couriers(
    client: EsiClient,
    graph: UniverseGraph,
    region_ids: Iterable[int],
    *,
    clock: Callable[[], datetime] | None = None,
    include_system_kills: bool = False,
    zkill: ZkillClient | None = None,
    include_threat_intel: bool = False,
    threat_window_seconds: int = DEFAULT_THREAT_WINDOW_SECONDS,
    threat_gate_radius_m: int = DEFAULT_GATE_RADIUS_M,
    threat_region_ids: Iterable[int] | None = None,
    contract_workers: int = DEFAULT_CONTRACT_SCAN_WORKERS,
) -> ContractSnapshot:
    """Capture public couriers plus optional activity/threat observations.

    Regions are independent ESI resources, so a small bounded worker pool hides network latency.
    Pagination *inside* each region remains sequential and every request still uses ``EsiClient``'s
    cache/retry/rate-limit handling. zKill collection remains separately rate-spaced and sequential.

    ``threat_region_ids`` may be a proof-safe transit superset wider or narrower than the contract
    discovery scope. When omitted, threat collection retains the v1 behavior of using the contract
    regions. System-kill activity is optional auxiliary data; an outage does not discard an
    otherwise valid contract snapshot.
    """

    regions = tuple(sorted(set(region_ids)))
    if not regions:
        raise ValueError("at least one region is required")
    unknown = [region_id for region_id in regions if region_id not in graph.regions]
    if unknown:
        raise ValueError(f"unknown SDE region IDs: {unknown}")

    threat_regions = (
        regions if threat_region_ids is None else tuple(sorted(set(threat_region_ids)))
    )
    unknown_threat = [region_id for region_id in threat_regions if region_id not in graph.regions]
    if unknown_threat:
        raise ValueError(f"unknown threat region IDs: {unknown_threat}")
    if include_threat_intel and not threat_regions:
        raise ValueError("at least one threat region is required when threat intel is enabled")

    contracts_by_id = {}
    contracts_by_region = _scan_contract_regions(client, regions, workers=contract_workers)
    for region_id in regions:
        for contract in contracts_by_region[region_id]:
            contracts_by_id[contract.contract_id] = contract
    activity: tuple[SystemKillActivity, ...] = ()
    activity_available = False
    if include_system_kills:
        try:
            activity = client.system_kills()
            activity_available = True
        except EsiError:
            # This feed is advisory. Public courier discovery remains useful when it is unavailable.
            pass
    now = (clock or (lambda: datetime.now(UTC)))()
    if now.tzinfo is None:
        raise ValueError("scanner clock must return a timezone-aware datetime")
    now = now.astimezone(UTC)
    threat: ThreatIntelCollection | None = None
    if include_threat_intel:
        if zkill is None:
            raise ValueError("zKill client is required when threat intel is requested")
        threat = collect_gate_threat_intel(
            zkill,
            graph,
            threat_regions,
            window_seconds=threat_window_seconds,
            gate_radius_m=threat_gate_radius_m,
            clock=lambda: now,
        )
    return ContractSnapshot(
        fetched_at=now,
        compatibility_date=client.compatibility_date or ESI_COMPATIBILITY_DATE,
        sde_build_number=graph.metadata.build_number,
        region_ids=regions,
        contracts=tuple(contracts_by_id[key] for key in sorted(contracts_by_id)),
        system_kills_fetched_at=now if activity_available else None,
        system_kill_activity=activity,
        threat_intel_fetched_at=threat.fetched_at if threat is not None else None,
        threat_window_seconds=threat.window_seconds if threat is not None else None,
        threat_gate_radius_m=threat.gate_radius_m if threat is not None else None,
        threat_coverage_region_ids=threat.coverage_region_ids if threat is not None else (),
        threat_incomplete_region_ids=(
            threat.incomplete_region_ids if threat is not None else ()
        ),
        threat_killmails_seen=threat.killmails_seen if threat is not None else 0,
        gate_threat_events=threat.events if threat is not None else (),
    )
