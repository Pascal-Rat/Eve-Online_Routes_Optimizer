"""Diverse deterministic solver stress cases; report bounds and actual elapsed time as JSONL."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import random
import time
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from pathlib import Path

import eve_courier_optimizer
from benchmarks.run_empire import FIXTURE, PROFILES, benchmark_constraints, load_empire_graph
from eve_courier_optimizer.domain import (
    CollateralMode,
    ContractSnapshot,
    PlanningConstraints,
    PublicCourierContract,
    SecurityPolicy,
    TravelTimeModel,
)
from eve_courier_optimizer.eve.snapshot_file import read_snapshot
from eve_courier_optimizer.optimization import RouteOptimizer, SolverConfig
from eve_courier_optimizer.routing.route_problem import RouteProblem
from eve_courier_optimizer.routing.universe import Region, SdeMetadata, SolarSystem, UniverseGraph

CASES = (
    "empire_dst",
    "empire_dst_2h",
    "empire_dst_rolling",
    "empire_br_rolling",
    "corridor_capacity",
    "clustered_locked",
    "clustered_rolling",
    "clustered_waypoints",
)


def prepare_case(name: str, *, seed: int = 17) -> tuple[UniverseGraph, RouteProblem]:
    if name.startswith("empire_"):
        graph = load_empire_graph()
        snapshot = read_snapshot(FIXTURE)
        profile = PROFILES[1] if name == "empire_br_rolling" else PROFILES[0]
        constraints = benchmark_constraints(graph, snapshot, profile)
        if name == "empire_dst_2h":
            constraints = replace(
                constraints,
                horizon_seconds=7200,
                collateral_budget_units=2 * constraints.collateral_budget_units,
            )
        elif name.endswith("rolling"):
            constraints = replace(constraints, collateral_mode=CollateralMode.ROLLING)
        return graph, RouteProblem.from_snapshot(snapshot, graph, constraints)
    now = datetime(2026, 8, 6, tzinfo=UTC)
    corridor = name == "corridor_capacity"
    count = 9 if corridor else 25
    systems = {i: SolarSystem(i, 10, f"System {i}", 0.9) for i in range(1, count + 1)}
    adjacency: dict[int, set[int]] = {i: set() for i in systems}
    for i in systems:
        candidates = (i + 1,) if corridor else (i + 1 if i % 5 else 0, i + 5)
        for j in candidates:
            if j in systems:
                adjacency[i].add(j)
                adjacency[j].add(i)
    graph = UniverseGraph(
        systems=systems,
        adjacency={i: tuple(sorted(edges)) for i, edges in adjacency.items()},
        station_systems={100 + i: i for i in systems},
        regions={10: Region(10, "Synthetic")},
        metadata=SdeMetadata(1, now.isoformat(), "fixture://stress-v1"),
    )
    rng = random.Random(seed)
    contracts: list[PublicCourierContract] = []
    for i in range(40 if corridor else 48):
        origin, dest = (1, 9) if corridor else rng.sample((1, 3, 5, 11, 13, 15, 21, 23, 25), 2)
        contracts.append(
            PublicCourierContract(
                contract_id=i + 1,
                origin_location_id=100 + origin,
                destination_location_id=100 + dest,
                volume_units=rng.randint(32, 48) if corridor else rng.randint(5, 40),
                collateral_units=rng.randint(10, 80),
                reward_units=rng.randint(100, 1000),
                date_expired=now + timedelta(hours=24),
                days_to_complete=1,
            )
        )
    snapshot = ContractSnapshot(now, "2026-08-05", 1, (10,), tuple(contracts))
    constraints = PlanningConstraints(
        start_system_id=1,
        cargo_capacity_units=100,
        collateral_budget_units=10000 if corridor else 600,
        horizon_seconds=700 if corridor else 600,
        snapshot_time=now,
        collateral_mode=CollateralMode.ROLLING
        if name == "clustered_rolling"
        else CollateralMode.LOCKED,
        security=SecurityPolicy(0.45),
        travel=TravelTimeModel(10, 2),
        return_to_start=name != "clustered_waypoints",
        required_system_ids=frozenset({5, 21}) if name == "clustered_waypoints" else frozenset(),
        finish_system_id=25 if name == "clustered_waypoints" else None,
    )
    return graph, RouteProblem.from_snapshot(snapshot, graph, constraints)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", nargs="+", choices=CASES, default=CASES)
    parser.add_argument("--time-limit", type=float, default=30)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--label", default="current")
    parser.add_argument("--log", action="store_true")
    args = parser.parse_args()
    config = SolverConfig(
        max_time_seconds=args.time_limit,
        num_workers=args.workers,
        random_seed=args.seed,
        minimize_finish_time_after_proof=False,
        log_search_progress=args.log,
    )
    source_root = Path(eve_courier_optimizer.__file__).parent
    source_hash = hashlib.sha256()
    for source in sorted(source_root.rglob("*.py")):
        source_hash.update(str(source.relative_to(source_root)).encode() + b"\0")
        source_hash.update(source.read_bytes())
    runner_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    for name in args.cases:
        start = time.perf_counter()
        graph, problem = prepare_case(name, seed=args.seed)
        preparation = time.perf_counter() - start
        result = RouteOptimizer(
            problem,
            graph,
            config=config,
        ).solve()
        c = result.certificate
        row = dict(
            case=name,
            label=args.label,
            seed=args.seed,
            workers=args.workers,
            python=platform.python_version(),
            ortools=version("ortools"),
            source_sha256=source_hash.hexdigest(),
            runner_sha256=runner_hash,
            config=asdict(config),
            eligible=len(problem.contracts),
            seconds=args.time_limit,
            elapsed=time.perf_counter() - start,
            preparation=preparation,
            solver_wall=c.wall_time_seconds,
            status=c.status.value,
            reward=c.objective_units,
            bound=c.best_bound_units,
            gap=c.relative_gap,
            selected=len(result.selected_contract_ids),
            selected_ids=result.selected_contract_ids,
            finish_seconds=result.finish_seconds,
            verified=c.feasibility_verified,
            fingerprint=c.problem_sha256,
            master_status=c.system_relaxation_status,
            master_wall=c.system_relaxation_wall_time_seconds,
            decomposition=c.decomposition_status,
            iterations=c.decomposition_iterations,
            cuts=c.decomposition_learned_cuts,
            oracle_wall=c.decomposition_subproblem_wall_time_seconds,
        )
        encoded = json.dumps(row, sort_keys=True)
        print(encoded, flush=True)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("a") as stream:
                stream.write(encoded + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
