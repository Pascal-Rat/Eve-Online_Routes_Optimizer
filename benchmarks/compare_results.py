"""Compare repeated stress observations only when problem fingerprints and settings agree."""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path
from statistics import median

from eve_courier_optimizer.jsonio import json_int, json_object, json_string

type CaseKey = tuple[str, int, float, int]


def observations(paths: list[Path]) -> dict[CaseKey, list[dict[str, object]]]:
    groups: dict[CaseKey, list[dict[str, object]]] = defaultdict(list)
    for path in paths:
        for line in path.read_text().splitlines():
            row = json_object(json.loads(line), str(path))
            seconds = row["seconds"]
            if not isinstance(seconds, int | float) or isinstance(seconds, bool):
                raise ValueError(f"{path}: seconds must be numeric")
            if not math.isfinite(seconds) or seconds <= 0:
                raise ValueError(f"{path}: seconds must be finite and positive")
            key = (
                json_string(row["case"], "case"),
                json_int(row["seed"], "seed"),
                float(seconds),
                json_int(row["workers"], "workers"),
            )
            groups[key].append(row)
    return groups


def objective_range(rows: list[dict[str, object]], field: str) -> str:
    values = [json_int(row[field], field) for row in rows if row[field] is not None]
    if not values:
        return "unknown"
    result = str(min(values)) if min(values) == max(values) else f"{min(values)}–{max(values)}"
    return result + (" / unknown" if len(values) != len(rows) else "")


def elapsed(rows: list[dict[str, object]]) -> float:
    values = []
    for row in rows:
        value = row["elapsed"]
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError("elapsed must be numeric")
        values.append(value)
    return median(values)


def compare(baseline_paths: list[Path], candidate_paths: list[Path]) -> str:
    baseline, candidate = observations(baseline_paths), observations(candidate_paths)
    if not baseline or baseline.keys() != candidate.keys():
        raise ValueError(
            "baseline and candidate must contain the same case/seed/budget/worker groups"
        )
    lines = [
        "Reward and bound ranges use integer centi-ISK; elapsed is the median total seconds.",
        "Proofs counts proven-optimal runs. Each cell is baseline → candidate.",
        "",
        "| Case / seed / seconds / workers | Runs | Reward | Bound | Proofs | Elapsed |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for key in sorted(baseline):
        old, new = baseline[key], candidate[key]
        signatures = {
            (
                json_string(row["fingerprint"], "fingerprint"),
                json.dumps(row["config"], sort_keys=True),
            )
            for row in [*old, *new]
        }
        if len(signatures) != 1:
            raise ValueError(f"{key}: non-equivalent problems or solver settings")
        if any(row["verified"] is not True for row in [*old, *new]):
            raise ValueError(f"{key}: an observation has no independently verified route")
        proofs = [sum(row["status"] == "proven_optimal" for row in rows) for rows in (old, new)]
        lines.append(
            f"| {' / '.join(map(str, key))} | {len(old)} → {len(new)} | "
            f"{objective_range(old, 'reward')} → {objective_range(new, 'reward')} | "
            f"{objective_range(old, 'bound')} → {objective_range(new, 'bound')} | "
            f"{proofs[0]} → {proofs[1]} | {elapsed(old):.3f} → {elapsed(new):.3f} |"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", nargs="+", type=Path, required=True)
    parser.add_argument("--candidate", nargs="+", type=Path, required=True)
    args = parser.parse_args()
    print(compare(args.baseline, args.candidate), end="")


if __name__ == "__main__":
    main()
