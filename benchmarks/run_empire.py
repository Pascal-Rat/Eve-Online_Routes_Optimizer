"""Run the realistic frozen NPC-Empire courier benchmarks.

Unlike ``run_frozen.py``'s tiny proof-regression universe, this benchmark preserves a real
Tranquility public-contract observation.  It is intentionally opt-in because each profile normally
gets a one-minute CP-SAT search budget.
"""

from __future__ import annotations

import argparse
import gzip
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from eve_courier_optimizer.domain import (
    ContractSnapshot,
    PlanningConstraints,
    ProofStatus,
    SecurityBand,
    SecurityPolicy,
    ThreatCategory,
    TravelTimeModel,
    cargo_capacity_to_units,
    isk_to_units,
)
from eve_courier_optimizer.eve.snapshot_file import read_snapshot
from eve_courier_optimizer.optimization import RouteOptimizer, SolverConfig
from eve_courier_optimizer.routing.route_problem import RouteProblem
from eve_courier_optimizer.routing.security import threat_avoided_systems
from eve_courier_optimizer.routing.universe import UniverseGraph

FIXTURE = Path(__file__).with_name("empire_snapshot_2026-08-06.json")
FROZEN_SDE = Path(__file__).with_name("empire_sde_3458726.sqlite3.gz")


def load_empire_graph() -> UniverseGraph:
    """Pin routing data as well as contracts so bundled SDE upgrades preserve this benchmark."""
    with TemporaryDirectory(prefix="eve-empire-sde-") as directory:
        path = Path(directory) / "route_sde.sqlite3"
        with gzip.open(FROZEN_SDE, "rb") as stream:
            path.write_bytes(stream.read())
        return UniverseGraph.from_sqlite(path)


THREAT_CATEGORIES = frozenset(
    {
        ThreatCategory.SUICIDE_GANK,
        ThreatCategory.SMARTBOMB,
        ThreatCategory.HEAVY_INTERDICTOR,
        ThreatCategory.CARRIER,
        ThreatCategory.GATE_CAMP,
        ThreatCategory.HAULER_LOSS,
    }
)


@dataclass(frozen=True, slots=True)
class EmpireProfile:
    name: str
    cargo_m3: Decimal
    collateral_isk: Decimal
    security_bands: frozenset[SecurityBand]
    expected_eligible: int
    expected_reward_isk: str


@dataclass(frozen=True, slots=True)
class EmpireBenchmarkResult:
    profile: str
    status: ProofStatus
    eligible_contracts: int
    selected_contracts: int
    reward_isk: str | None
    best_bound_isk: str | None
    relative_gap: float | None
    wall_time_seconds: float
    branches: int
    problem_sha256: str
    feasibility_verified: bool


PROFILES = (
    EmpireProfile(
        name="dst",
        cargo_m3=Decimal("62500"),
        collateral_isk=Decimal("10000000000"),
        security_bands=frozenset({SecurityBand.HIGH}),
        expected_eligible=96,
        expected_reward_isk="58000000",
    ),
    EmpireProfile(
        name="br",
        cargo_m3=Decimal("13000"),
        collateral_isk=Decimal("5000000000"),
        security_bands=frozenset({SecurityBand.HIGH, SecurityBand.LOW}),
        expected_eligible=48,
        expected_reward_isk="25651527",
    ),
)


def _jita_system_id(graph: UniverseGraph) -> int:
    matches = [system.system_id for system in graph.systems.values() if system.name == "Jita"]
    if len(matches) != 1:
        raise RuntimeError("frozen Empire benchmark requires exactly one Jita system")
    return matches[0]


def benchmark_constraints(
    graph: UniverseGraph,
    snapshot: ContractSnapshot,
    profile: EmpireProfile,
) -> PlanningConstraints:
    jita = _jita_system_id(graph)
    threat_avoids = threat_avoided_systems(
        snapshot.gate_threat_events,
        THREAT_CATEGORIES,
        minimum_events=1,
        exempt_system_ids=frozenset({jita}),
    )
    return PlanningConstraints(
        start_system_id=jita,
        cargo_capacity_units=cargo_capacity_to_units(profile.cargo_m3),
        collateral_budget_units=isk_to_units(profile.collateral_isk),
        horizon_seconds=3_600,
        snapshot_time=snapshot.fetched_at,
        return_to_start=True,
        travel=TravelTimeModel(seconds_per_jump=75, service_seconds=30),
        security=SecurityPolicy(
            minimum_security=None,
            allowed_bands=profile.security_bands,
            threat_avoided_system_ids=threat_avoids,
            threat_categories=THREAT_CATEGORIES,
            threat_min_events=1,
            threat_intel_fetched_at=snapshot.threat_intel_fetched_at,
            threat_window_seconds=snapshot.threat_window_seconds,
            threat_gate_radius_m=snapshot.threat_gate_radius_m,
            threat_coverage_region_ids=frozenset(snapshot.threat_coverage_region_ids),
            threat_incomplete_region_ids=frozenset(snapshot.threat_incomplete_region_ids),
        ),
    )


def _isk(units: int | None) -> str | None:
    return None if units is None else str(Decimal(units) / Decimal(100))


def run_empire_profile(
    profile: EmpireProfile,
    *,
    time_limit_seconds: float = 60.0,
    workers: int = 4,
) -> EmpireBenchmarkResult:
    """Solve one frozen Empire profile without truncating its eligible contract set."""

    graph = load_empire_graph()
    snapshot = read_snapshot(FIXTURE)
    problem = RouteProblem.from_snapshot(
        snapshot, graph, benchmark_constraints(graph, snapshot, profile)
    )
    eligible = problem.scope.eligible_contracts
    if eligible != profile.expected_eligible:
        raise RuntimeError(
            f"{profile.name} baseline changed: expected {profile.expected_eligible} eligible "
            f"contracts, got {eligible}"
        )
    solved = RouteOptimizer(
        problem,
        graph,
        config=SolverConfig(
            max_time_seconds=time_limit_seconds,
            num_workers=workers,
            minimize_finish_time_after_proof=False,
        ),
    ).solve()
    certificate = solved.certificate
    return EmpireBenchmarkResult(
        profile=profile.name,
        status=certificate.status,
        eligible_contracts=eligible,
        selected_contracts=len(solved.selected_contract_ids),
        reward_isk=_isk(certificate.objective_units),
        best_bound_isk=_isk(certificate.best_bound_units),
        relative_gap=certificate.relative_gap,
        wall_time_seconds=certificate.wall_time_seconds,
        branches=certificate.branches,
        problem_sha256=certificate.problem_sha256,
        feasibility_verified=certificate.feasibility_verified,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--profile",
        choices=["all", *(item.name for item in PROFILES)],
        default="all",
    )
    parser.add_argument("--time-limit", type=float, default=60.0)
    parser.add_argument("--workers", type=int, default=4)
    arguments = parser.parse_args()
    if arguments.time_limit <= 0:
        parser.error("--time-limit must be positive")
    if arguments.workers <= 0:
        parser.error("--workers must be positive")

    profiles = (
        PROFILES
        if arguments.profile == "all"
        else tuple(profile for profile in PROFILES if profile.name == arguments.profile)
    )
    print("profile status eligible selected reward_ISK bound_ISK gap wall_s branches")
    results: list[EmpireBenchmarkResult] = []
    for profile in profiles:
        result = run_empire_profile(
            profile,
            time_limit_seconds=arguments.time_limit,
            workers=arguments.workers,
        )
        results.append(result)
        gap = "--" if result.relative_gap is None else f"{result.relative_gap * 100:.3f}%"
        print(
            result.profile,
            result.status.value,
            result.eligible_contracts,
            result.selected_contracts,
            result.reward_isk or "--",
            result.best_bound_isk or "--",
            gap,
            f"{result.wall_time_seconds:.3f}",
            result.branches,
        )
    usable = all(
        result.status is ProofStatus.PROVEN_OPTIMAL
        and result.feasibility_verified
        and result.reward_isk == profile.expected_reward_isk
        and result.best_bound_isk == profile.expected_reward_isk
        for profile, result in zip(profiles, results, strict=True)
    )
    return 0 if usable else 1


if __name__ == "__main__":
    raise SystemExit(main())
