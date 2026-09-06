"""Isolate verified primal construction cost, without starting CP-SAT search."""

from __future__ import annotations

import argparse
import hashlib
import json
import time
from pathlib import Path

from eve_courier_optimizer import solver
from eve_courier_optimizer.construction import insert_additional_contracts
from eve_courier_optimizer.proof import canonical_problem_sha256
from eve_courier_optimizer.verification import PlannedAction, simulate_and_verify

from .explore_avenues import rebuild_incumbent
from .run_stress import CASES, prepare_case


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    digest = hashlib.sha256()
    root = Path(solver.__file__).parent
    for source in sorted(root.rglob("*.py")):
        digest.update(str(source.relative_to(root)).encode() + b"\0")
        digest.update(source.read_bytes())
    for case in CASES:
        graph, prepared = prepare_case(case, seed=args.seed)
        for variant in ("insertion", "rebuild", "repair"):
            start = time.perf_counter()
            visits = solver._build_greedy_route_hint(prepared)
            ids = tuple(sorted({v.contract_id for v in visits if isinstance(v, PlannedAction)}))
            sim = simulate_and_verify(prepared.problem, graph, visits, ids)
            if variant == "insertion":
                _, result = insert_additional_contracts(prepared, graph, visits, sim)
            else:
                _, result = rebuild_incumbent(
                    prepared, graph, visits, sim, repair=variant == "repair"
                )
            elapsed = time.perf_counter() - start
            assert result.report.valid
            row = dict(
                case=case,
                variant=variant,
                seed=args.seed,
                eligible=len(prepared.scores),
                reward=result.total_reward_units,
                finish=result.finish_seconds,
                elapsed=elapsed,
                verified=result.report.valid,
                fingerprint=canonical_problem_sha256(prepared.problem, prepared.jump_matrix),
                source_sha256=digest.hexdigest(),
                adapter_sha256=hashlib.sha256(
                    Path(__file__).with_name("explore_avenues.py").read_bytes()
                ).hexdigest(),
                runner_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            )
            encoded = json.dumps(row, sort_keys=True)
            print(encoded, flush=True)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("a") as stream:
                stream.write(encoded + "\n")


if __name__ == "__main__":
    main()
