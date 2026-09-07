"""Measure preparation, construction and both proof models without running CP-SAT search."""

from __future__ import annotations

import argparse
import gc
import hashlib
import json
import platform
import sys
import time
from importlib.metadata import version
from pathlib import Path

import eve_courier_optimizer
from benchmarks.run_stress import CASES, prepare_case
from eve_courier_optimizer.optimization.models.pickup_delivery import PickupDeliveryModel
from eve_courier_optimizer.optimization.models.selection_bounds import build_selection_cuts
from eve_courier_optimizer.optimization.models.system_tour import SystemTourModel
from eve_courier_optimizer.optimization.proof_certificate import canonical_problem_sha256
from eve_courier_optimizer.optimization.search.route_insertion import (
    build_greedy_route_hint,
    construct_incumbent,
)


def peak_resident_bytes() -> int | None:
    if sys.platform == "win32":
        return None
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(peak * (1 if sys.platform == "darwin" else 1024))


def measure(case: str, *, seed: int) -> dict[str, object]:
    # Cleanup of cycles from earlier CP models must not be charged to this case's phases.
    gc.collect()
    started = time.perf_counter()
    graph, problem = prepare_case(case, seed=seed)
    preparation = time.perf_counter() - started
    started = time.perf_counter()
    incumbent = construct_incumbent(problem, graph)
    construction = time.perf_counter() - started
    if incumbent is None:
        raise RuntimeError(f"{case}: construction produced no verified route")
    started = time.perf_counter()
    cuts = build_selection_cuts(problem)
    selection = time.perf_counter() - started
    started = time.perf_counter()
    master = SystemTourModel(problem, selection_cuts=cuts)
    master_seconds = time.perf_counter() - started
    started = time.perf_counter()
    route = PickupDeliveryModel(problem, selection_hint=build_greedy_route_hint(problem))
    event_seconds = time.perf_counter() - started
    simulation = incumbent.simulation
    row: dict[str, object] = {
        "case": case,
        "seed": seed,
        "fingerprint": canonical_problem_sha256(problem),
        "eligible": len(problem.contracts),
        "preparation": preparation,
        "construction": construction,
        "selection_cuts": selection,
        "master_model": master_seconds,
        "event_model": event_seconds,
        "reward": simulation.total_reward_units,
        "finish": simulation.finish_seconds,
        "selected_ids": incumbent.selected_contract_ids,
        "verified": simulation.report.valid,
        "pairs": len(cuts.pairs),
        "cliques": len(cuts.cliques),
        "master_variables": len(master.model.proto.variables),
        "master_constraints": len(master.model.proto.constraints),
        "event_variables": len(route.model.proto.variables),
        "event_constraints": len(route.model.proto.constraints),
        "event_arcs": len(route.arc_is_used),
        "event_sha256": hashlib.sha256(str(route.model.proto).encode()).hexdigest(),
        "master_sha256": hashlib.sha256(str(master.model.proto).encode()).hexdigest(),
        "peak_resident_bytes": peak_resident_bytes(),
    }
    started = time.perf_counter()
    route.hint(simulation.visits, incumbent.selected_contract_ids)
    master.hint(
        incumbent.selected_contract_ids,
        tuple(leg.to_system_id for leg in simulation.travel_legs),
        simulation.total_reward_units,
    )
    row["hints"] = time.perf_counter() - started
    row["hinted_event_sha256"] = hashlib.sha256(str(route.model.proto).encode()).hexdigest()
    row["hinted_master_sha256"] = hashlib.sha256(str(master.model.proto).encode()).hexdigest()
    return row


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", nargs="+", choices=CASES, default=CASES)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat must be positive")
    source_root = Path(eve_courier_optimizer.__file__).parent
    digest = hashlib.sha256()
    for source in sorted(source_root.rglob("*.py")):
        digest.update(str(source.relative_to(source_root)).encode() + b"\0")
        digest.update(source.read_bytes())
    metadata = {
        "python": platform.python_version(),
        "ortools": version("ortools"),
        "source_sha256": digest.hexdigest(),
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }
    for case in args.cases:
        for trial in range(args.repeat):
            encoded = json.dumps({**metadata, **measure(case, seed=args.seed), "trial": trial})
            print(encoded, flush=True)
            if args.output:
                args.output.parent.mkdir(parents=True, exist_ok=True)
                with args.output.open("a") as stream:
                    stream.write(encoded + "\n")


if __name__ == "__main__":
    main()
