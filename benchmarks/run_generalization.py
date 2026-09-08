"""Evaluate unseen route graphs with input seeds independent of CP-SAT's search seed."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import random
import time
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from importlib.metadata import version
from pathlib import Path

import eve_courier_optimizer
from benchmarks.run_stress import prepare_case
from eve_courier_optimizer.domain import (
    ActiveShipment,
    CollateralMode,
    ContractSnapshot,
    PlanningConstraints,
    PublicCourierContract,
    RoutableContract,
    SecurityPolicy,
    TravelTimeModel,
)
from eve_courier_optimizer.optimization import RouteOptimizer, SolverConfig
from eve_courier_optimizer.routing.route_problem import RouteProblem
from eve_courier_optimizer.routing.universe import Region, SdeMetadata, SolarSystem, UniverseGraph

FAMILIES = ("cycle", "tree", "sparse", "grid_chords")
HOLDOUT_SEEDS = tuple(range(4101, 4113))
DEVELOPMENT_CASES = (
    "empire_dst_2h",
    "clustered_locked",
    "clustered_rolling",
    "clustered_waypoints",
)


def prepare_generated_case(seed: int) -> tuple[str, UniverseGraph, RouteProblem]:
    rng = random.Random(seed)
    index = seed - HOLDOUT_SEEDS[0]
    family = FAMILIES[index % len(FAMILIES)]
    size = 16 if family == "grid_chords" else rng.randint(9, 16)
    edges: dict[int, set[int]] = {i: set() for i in range(1, size + 1)}

    def connect(a: int, b: int) -> None:
        edges[a].add(b)
        edges[b].add(a)

    for i in range(2, size + 1):
        if family == "grid_chords":
            if i % 4 != 1:
                connect(i - 1, i)
            if i > 4:
                connect(i - 4, i)
        else:
            connect(rng.randrange(1, i) if family == "tree" else i - 1, i)
    if family == "cycle":
        connect(size, 1)
    if family in {"sparse", "grid_chords"}:
        for _ in range(size // 3):
            connect(*rng.sample(tuple(edges), 2))
    now = datetime(2026, 9, 3, tzinfo=UTC)
    # Relabeling avoids giving low numeric IDs a consistent geometric meaning.
    labels = rng.sample(range(1000, 9000), size)
    label = dict(zip(edges, labels, strict=True))
    graph = UniverseGraph(
        systems={s: SolarSystem(s, 10, f"Generated {s}", 0.9) for s in labels},
        adjacency={
            label[s]: tuple(sorted(label[t] for t in adjacent)) for s, adjacent in edges.items()
        },
        station_systems={s + 10000: s for s in labels},
        regions={10: Region(10, "Generated")},
        metadata=SdeMetadata(1, now.isoformat(), "fixture://generalization-v1"),
    )
    ports = rng.sample(labels, rng.randint(4, min(8, size)))
    items: list[PublicCourierContract] = []
    for i in range(24 + 8 * (index % 3)):
        origin, destination = rng.sample(ports, 2)
        items.append(
            PublicCourierContract(
                contract_id=seed * 100 + i,
                origin_location_id=origin + 10000,
                destination_location_id=destination + 10000,
                volume_units=rng.randint(15, 65),
                collateral_units=rng.randint(15, 100),
                reward_units=rng.randint(50, 1000),
                date_expired=now + timedelta(seconds=rng.choice((120, 240, 86400))),
                days_to_complete=1,
            )
        )
    rng.shuffle(items)
    terminal_kind = (index // 4) % 3
    mode = CollateralMode.ROLLING if (index // 4 + index) % 2 else CollateralMode.LOCKED
    start = rng.choice(labels)
    terminal = rng.choice([s for s in labels if s != start]) if terminal_kind == 1 else None
    active: tuple[ActiveShipment, ...] = ()
    if index % 3 == 2:
        origin, destination = rng.sample(ports, 2)
        item = PublicCourierContract(
            contract_id=seed * 100 + 99,
            origin_location_id=origin + 10000,
            destination_location_id=destination + 10000,
            volume_units=20,
            collateral_units=30,
            reward_units=200,
            date_expired=now + timedelta(days=1),
            days_to_complete=1,
        )
        active = (
            ActiveShipment(
                RoutableContract.resolve(item, origin, destination),
                now + timedelta(seconds=180),
                picked=index % 2 == 0,
            ),
        )
    constraints = PlanningConstraints(
        start_system_id=start,
        cargo_capacity_units=100,
        collateral_budget_units=150 if mode is CollateralMode.ROLLING else 300,
        horizon_seconds=rng.choice((180, 240, 300)),
        snapshot_time=now,
        collateral_mode=mode,
        security=SecurityPolicy(0.45),
        travel=TravelTimeModel(8, 2),
        return_to_start=terminal_kind == 0,
        finish_system_id=terminal,
        required_system_ids=frozenset(rng.sample(labels, 2)) if index % 4 == 1 else frozenset(),
    )
    snapshot = ContractSnapshot(now, "2026-09-03", 1, (10,), tuple(items))
    problem = RouteProblem.from_snapshot(snapshot, graph, constraints, active_shipments=active)
    return family, graph, problem


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-seeds", nargs="+", type=int, default=HOLDOUT_SEEDS)
    parser.add_argument("--solver-seed", type=int, default=29)
    parser.add_argument("--time-limit", type=float, default=12)
    parser.add_argument("--decomposition-time", type=float, default=4)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--development", action="store_true")
    parser.add_argument(
        "--legacy-phase-budget",
        action="store_true",
        help="For pre-deadline revisions: event allowance = total minus decomposition",
    )
    parser.add_argument("--label", default="candidate")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    seconds = (
        args.time_limit - args.decomposition_time if args.legacy_phase_budget else args.time_limit
    )
    config = SolverConfig(
        max_time_seconds=seconds,
        num_workers=args.workers,
        random_seed=args.solver_seed,
        decomposition_time_seconds=args.decomposition_time,
        minimize_finish_time_after_proof=False,
    )
    source_root = Path(eve_courier_optimizer.__file__).parent
    source_hash = hashlib.sha256()
    for source in sorted(source_root.rglob("*.py")):
        source_hash.update(
            str(source.relative_to(source_root)).encode() + b"\0" + source.read_bytes()
        )
    cases = (
        (name, seed)
        for seed in args.input_seeds
        for name in (DEVELOPMENT_CASES if args.development else ("generated",))
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for name, seed in cases:
        if args.development:
            graph, problem = prepare_case(name, seed=seed)
        else:
            name, graph, problem = prepare_generated_case(seed)
        start = time.perf_counter()
        result = RouteOptimizer(problem, graph, config=config).solve()
        elapsed = time.perf_counter() - start
        c = result.certificate
        row = dict(
            case=name,
            input_seed=seed,
            solver_seed=args.solver_seed,
            label=args.label,
            python=platform.python_version(),
            ortools=version("ortools"),
            source_sha256=source_hash.hexdigest(),
            runner_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            config=asdict(config),
            target_seconds=args.time_limit,
            legacy_phase_budget=args.legacy_phase_budget,
            fingerprint=c.problem_sha256,
            eligible=len(problem.contracts),
            elapsed=elapsed,
            status=c.status.value,
            reward=c.objective_units,
            bound=c.best_bound_units,
            verified=c.feasibility_verified,
            master_wall=c.system_relaxation_wall_time_seconds,
            decomposition=c.decomposition_status,
            iterations=c.decomposition_iterations,
            cuts=c.decomposition_learned_cuts,
            oracle_wall=c.decomposition_subproblem_wall_time_seconds,
        )
        encoded = json.dumps(row, sort_keys=True)
        print(encoded, flush=True)
        with args.output.open("a", encoding="utf-8") as stream:
            stream.write(encoded + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
