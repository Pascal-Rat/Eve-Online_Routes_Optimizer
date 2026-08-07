"""Run the frozen DST and blockade-runner proof benchmarks."""

from __future__ import annotations

import argparse
import json
import time
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

from eve_courier_optimizer.domain import (
    GateEvidence,
    GateThreatEvent,
    PlanningConstraints,
    ProofStatus,
    PublicCourierContract,
    SecurityBand,
    SecurityPolicy,
    ThreatCategory,
    TravelTimeModel,
    cargo_capacity_to_units,
    isk_to_units,
    parse_esi_datetime,
)
from eve_courier_optimizer.planning import prepare_problem
from eve_courier_optimizer.sde import Region, SdeMetadata, SolarSystem, UniverseGraph
from eve_courier_optimizer.snapshot import ContractSnapshot
from eve_courier_optimizer.solver import SolverConfig, solve_exact
from eve_courier_optimizer.threat_intel import threat_avoided_systems

FIXTURE = Path(__file__).with_name("frozen_universe.json")


@dataclass(frozen=True, slots=True)
class BenchmarkResult:
    name: str
    elapsed_seconds: float
    eligible_contracts: int
    selected_contracts: int
    reward_isk: str
    branches: int
    status: ProofStatus
    scope_untruncated: bool
    feasibility_verified: bool


def _load_fixture() -> dict[str, Any]:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("fixture_version") != 1:
        raise ValueError("unsupported frozen benchmark fixture")
    return cast(dict[str, Any], payload)


def _graph(payload: dict[str, Any]) -> UniverseGraph:
    systems = {
        int(row[0]): SolarSystem(int(row[0]), int(row[1]), str(row[2]), float(row[3]))
        for row in cast(list[list[Any]], payload["systems"])
    }
    adjacency_work: dict[int, set[int]] = {system_id: set() for system_id in systems}
    for source, destination in cast(list[list[int]], payload["edges"]):
        adjacency_work[source].add(destination)
        adjacency_work[destination].add(source)
    return UniverseGraph(
        systems=systems,
        adjacency={key: tuple(sorted(value)) for key, value in adjacency_work.items()},
        station_systems={1_000 + system_id: system_id for system_id in systems},
        regions={
            int(row[0]): Region(int(row[0]), str(row[1]))
            for row in cast(list[list[Any]], payload["regions"])
        },
        metadata=SdeMetadata(1, "2026-08-05T00:00:00Z", "fixture://frozen-v1"),
    )


def _threat_events(payload: dict[str, Any]) -> tuple[GateThreatEvent, ...]:
    fetched_at = parse_esi_datetime(str(payload["fetched_at"]))
    events: list[GateThreatEvent] = []
    for killmail_id, system_id, region_id, raw_categories in cast(
        list[list[Any]], payload["threat_events"]
    ):
        events.append(
            GateThreatEvent(
                killmail_id=int(killmail_id),
                occurred_at=fetched_at - timedelta(hours=1),
                system_id=int(system_id),
                region_id=int(region_id),
                gate_id=50_000_000 + int(system_id),
                distance_to_gate_m=0,
                evidence=GateEvidence.ZKILL_LOCATION,
                categories=frozenset(
                    ThreatCategory(str(value)) for value in cast(list[Any], raw_categories)
                ),
                victim_ship_type_id=1,
                player_attacker_count=1,
            )
        )
    return tuple(events)


def _snapshot(payload: dict[str, Any]) -> ContractSnapshot:
    fetched_at = parse_esi_datetime(str(payload["fetched_at"]))
    contracts: list[PublicCourierContract] = []
    for contract_id, origin, destination, volume_m3, collateral_b, reward_m in cast(
        list[list[Any]], payload["contracts"]
    ):
        contracts.append(
            PublicCourierContract(
                contract_id=int(contract_id),
                origin_location_id=1_000 + int(origin),
                destination_location_id=1_000 + int(destination),
                volume_units=cargo_capacity_to_units(str(volume_m3)),
                collateral_units=isk_to_units(
                    Decimal(str(collateral_b)) * Decimal("1000000000")
                ),
                reward_units=isk_to_units(Decimal(str(reward_m)) * Decimal("1000000")),
                date_expired=fetched_at + timedelta(days=1),
                days_to_complete=1,
                title=f"Frozen contract {contract_id}",
                date_issued=fetched_at - timedelta(hours=1),
            )
        )
    events = _threat_events(payload)
    return ContractSnapshot(
        fetched_at=fetched_at,
        compatibility_date="2026-08-05",
        sde_build_number=1,
        region_ids=(100, 200),
        contracts=tuple(contracts),
        threat_intel_fetched_at=fetched_at,
        threat_window_seconds=86_400,
        threat_gate_radius_m=250_000,
        threat_coverage_region_ids=(100, 200),
        threat_killmails_seen=len(events),
        gate_threat_events=events,
    )


def run_benchmark_scenarios(*, time_limit_seconds: float = 15.0) -> tuple[BenchmarkResult, ...]:
    payload = _load_fixture()
    graph = _graph(payload)
    snapshot = _snapshot(payload)
    results: list[BenchmarkResult] = []
    for raw_scenario in cast(list[dict[str, Any]], payload["scenarios"]):
        categories = frozenset(
            ThreatCategory(str(value))
            for value in cast(list[Any], raw_scenario["threat_categories"])
        )
        avoids = threat_avoided_systems(
            snapshot.gate_threat_events,
            categories,
            minimum_events=1,
            exempt_system_ids=frozenset({1}),
        )
        constraints = PlanningConstraints(
            start_system_id=1,
            cargo_capacity_units=cargo_capacity_to_units(str(raw_scenario["cargo_m3"])),
            collateral_budget_units=isk_to_units(
                Decimal(str(raw_scenario["collateral_billion_isk"]))
                * Decimal("1000000000")
            ),
            horizon_seconds=3_600,
            snapshot_time=snapshot.fetched_at,
            return_to_start=True,
            travel=TravelTimeModel(seconds_per_jump=75, service_seconds=30),
            security=SecurityPolicy(
                minimum_security=None,
                allowed_bands=frozenset(
                    SecurityBand(str(value))
                    for value in cast(list[Any], raw_scenario["security_bands"])
                ),
                threat_avoided_system_ids=avoids,
                threat_categories=categories,
                threat_min_events=1,
                threat_intel_fetched_at=snapshot.threat_intel_fetched_at,
                threat_window_seconds=snapshot.threat_window_seconds,
                threat_gate_radius_m=snapshot.threat_gate_radius_m,
                threat_coverage_region_ids=frozenset(snapshot.threat_coverage_region_ids),
            ),
        )
        started = time.perf_counter()
        prepared = prepare_problem(snapshot, graph, constraints)
        solved = solve_exact(
            prepared,
            graph,
            config=SolverConfig(
                max_time_seconds=time_limit_seconds,
                num_workers=1,
                minimize_finish_time_after_proof=False,
            ),
        )
        elapsed = time.perf_counter() - started
        results.append(
            BenchmarkResult(
                name=str(raw_scenario["name"]),
                elapsed_seconds=elapsed,
                eligible_contracts=prepared.problem.scope.eligible_contracts,
                selected_contracts=len(solved.selected_contract_ids),
                reward_isk=str(Decimal(solved.total_reward_units) / Decimal(100)),
                branches=solved.certificate.branches,
                status=solved.certificate.status,
                scope_untruncated=solved.certificate.scope_untruncated,
                feasibility_verified=solved.certificate.feasibility_verified,
            )
        )
    return tuple(results)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--time-limit", type=float, default=15.0)
    arguments = parser.parse_args()
    results = run_benchmark_scenarios(time_limit_seconds=arguments.time_limit)
    print("scenario status eligible selected reward_ISK elapsed_s branches")
    for result in results:
        print(
            result.name,
            result.status.value,
            result.eligible_contracts,
            result.selected_contracts,
            result.reward_isk,
            f"{result.elapsed_seconds:.3f}",
            result.branches,
        )
    valid = all(
        result.status is ProofStatus.PROVEN_OPTIMAL
        and result.scope_untruncated
        and result.feasibility_verified
        for result in results
    )
    return 0 if valid else 1


if __name__ == "__main__":
    raise SystemExit(main())
