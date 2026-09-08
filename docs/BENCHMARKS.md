# Reproducible benchmarks

## Workloads and hard checks

| Runner | Frozen problem | Required result |
| --- | --- | --- |
| `run_frozen` | 18 systems, 24 observed couriers, threat evidence; DST and BR profiles each retain 12 eligible jobs | DST 250M ISK, BR 165M ISK; both proven optimal, untruncated and independently verified |
| `run_empire` | 421 real observed NPC Empire couriers, 250 gate events, pinned SDE 3458726; one-hour Jita loops | DST: 96 eligible, 58M ISK. BR: 48 eligible, 25.651527M ISK. Both closed proofs with matching bounds. |
| `run_stress` | Eight deterministic cases: Empire locked/rolling and two-hour variants, shared-lane capacity, clustered locked/rolling and required-waypoint routes | Compare feasibility, reward, bound, proof closure and runtime under identical settings; open proofs are reported explicitly |

The Empire fixture has its own compressed SDE and [provenance manifest](../benchmarks/empire_fixture_manifest.json).
Updating the application's bundled SDE must not change benchmark fingerprints. Synthetic stress
seeds control both contract generation and solver search; Empire inputs are fixed independently of
the seed. Known optima are enforced by the runners and tests.

```bash
python -m benchmarks.run_frozen --time-limit 10
python -m benchmarks.run_empire --time-limit 60 --workers 4
python -m benchmarks.run_stress --time-limit 10 --seed 17 --workers 4 --output benchmarks/results/stress-10.jsonl
python -m benchmarks.run_stress --time-limit 30 --seed 71 --workers 4 --output benchmarks/results/stress-30.jsonl
python -m benchmarks.measure_pipeline --repeat 3 --output benchmarks/results/pipeline.jsonl
```

Gold runners exit unsuccessfully on a quality, proof, scope or feasibility regression. Stress output
includes complete solver settings, source/runner hashes, dependency versions, problem fingerprint,
reward, bound, proof status, selected IDs, route finish and phase search times. Pipeline measurements
isolate preparation, construction, selection cuts, system-master building and complete-event building
without starting CP-SAT. They include variable/constraint/arc counts and model hashes.
Each pipeline case starts after garbage collection so earlier models do not distort its phase times.

Commit benchmark inputs, runners and regression assertions. Results, solver dumps and console logs
are generated artifacts; `benchmarks/results/` is ignored by Git. Each `--output` appends one JSON
record per observation, so use a fresh output path for each measurement campaign.

## Threat-aware routing coverage

Both gold runners enable threat avoidance. The tiny frozen universe has three synthetic
gate-threat events; Empire retains 250 observed events across 24 covered regions. Empire
stress variants inherit that observation and policy. Generated generalization cases currently
vary route geometry and resources, without adding threat observations.

Threat matches remove systems from the permitted transit graph before shortest paths and
contract feasibility are computed. A permitted endpoint can therefore become unreachable,
or its detour can exceed the horizon. Reward cannot compensate for a forbidden transit.

`tests/optimization/test_threat_routing.py` exercises observation-to-policy conversion,
preprocessing, optimization and every returned gate path. A very valuable contract has
permitted endpoints but two camped entrances. The three cases require rejecting it when
both entrances are blocked, using a longer permitted detour when it fits, and rejecting it
when that detour exceeds the horizon. The unrestricted control selects the valuable contract;
reusing its graph also checks that cached unrestricted distances cannot bypass the new policy.

```bash
python -m pytest tests/optimization/test_threat_routing.py --no-cov
```

An on/off comparison on 2026-09-08 used the **frozen 2026-08-06 observation**, the same profile
constraints, ten-second budgets, four workers and solver seed 41. All four solves proved optimal:

| Profile | Threat avoidance | Eligible contracts | Reward ISK | Flagged systems in returned route |
| --- | --- | ---: | ---: | --- |
| DST | Enabled | 96 | 58,000,000 | None |
| DST | Disabled | 96 | 58,000,000 | Anttiri |
| BR | Enabled | 48 | 25,651,527 | None |
| BR | Disabled | 60 | 34,054,795 | Rancer, Kourmonen, Ahbazon |

The enabled policy excluded 28 systems. These names describe historical benchmark routes,
not current threat reports. Tied optimal routes can change; tests should assert compliance
with the forbidden set, rather than require a particular unrestricted itinerary.

Threat coverage verifies the recorded policy. It does not establish that an unobserved system
has no ambush. Start/mandatory-endpoint exemptions, incomplete feeds and the observation window
remain part of the [domain contract](DOMAIN.md#gate-threat-policy).

## Compare revisions

Use the same interpreter and OR-Tools installation. Run revisions sequentially and alternate their
order across repeated trials; do not run tests or competing solvers at the same time. Include every
trial in the comparison, including unsuccessful or unfavorable runs. Compare the same cases, seeds,
workers and budgets, then verify mathematical input fingerprints before interpreting performance.

To measure a baseline, replace `BASELINE_REF` below with the tag or commit being compared:

```bash
mkdir -p /tmp/eve-router-baseline
git archive BASELINE_REF | tar -x -C /tmp/eve-router-baseline
```

Use an empty baseline directory. From there, run the stress commands above using the current
environment's absolute Python path and `PYTHONPATH=src`. Run them from this checkout with the same
arguments, repeating both revisions. Summarize the observations with:

```bash
python -m benchmarks.compare_results \
  --baseline /tmp/eve-router-baseline/benchmarks/results/stress-10.jsonl \
             /tmp/eve-router-baseline/benchmarks/results/stress-30.jsonl \
  --candidate benchmarks/results/stress-10.jsonl benchmarks/results/stress-30.jsonl
```

The comparator rejects mismatched problem/settings groups and unverified routes. It displays reward
and bound ranges, proof counts and median elapsed times; it does not hide an unfavorable trial behind
one average or turn a timing threshold into an optimization theorem.

The time limit is a shared cooperative allowance for optimization, including decomposition,
construction and secondary refinement; input preparation is outside it. Atomic operations and
solver cleanup may overrun slightly. Older revisions such as `edf56a2` instead give the full event
search a separate allowance. When comparing those revisions, subtract the decomposition cap from
their event allowance and record actual elapsed time; this still gives the baseline additional
construction/diversification time. Stress runs disable secondary refinement to compare primary
search. Use total elapsed for operator latency and phase times for diagnosis.
Multiworker CP-SAT is nondeterministic. Even a single-worker master can cross a wall-time cutoff on a
busy or slower CPU and change the later proof trajectory. Investigate repeated distributions and
model/hint equality, while retaining every known optimum as an exact quality requirement.
For short phases, also control interpreter warmup, cleanup and working directory; use matched
processes or a shared-process profile to distinguish additional work from timing variation.

`peak_resident_bytes` is a process high-water mark (null on Windows), not per-model allocated memory.
To compare a particular case's memory, run `measure_pipeline --cases CASE --repeat 1` in a fresh
process for each revision with equivalent instrumentation and object lifetimes. Do not compare one
script retaining previous models with another that releases each case's local objects.

## Generalization checks

`run_generalization` separates generated-input seeds from the CP-SAT seed and varies graph shape
(cycles, trees, sparse graphs and grids with chords), ports, capacity pressure, collateral mode,
fixed/free/loop finishes, expiry, required waypoints and accepted shipments. Numeric system labels
are shuffled. These cases complement the frozen observed snapshots and independently enumerated
small correctness tests; none alone establishes performance on every EVE workload.

```bash
python -m benchmarks.run_generalization --input-seeds 4101 4102 4103 4104 \
  --solver-seed 29 --time-limit 12 --decomposition-time 4 \
  --output benchmarks/results/generalization.jsonl
```

Declare the evaluation seeds, workload families, budgets and acceptance criteria before looking at
results. Use `--development` for existing stress cases while developing a candidate. Freeze the
algorithm, settings and runner before evaluating held-out seeds, retain every result, and compare
problem fingerprints across revisions. If a candidate is changed in response to those results,
the inspected cases become development data and need a fresh holdout. The runner records source
and runner hashes, versions, settings, input/search seeds and proof evidence. For old budget
semantics use `--legacy-phase-budget` with the same target allowance and document the difference.

Benchmark identities and known optima belong in fixtures and assertions, never in production
selection, pruning or solver settings. Structural heuristics still need empirical evaluation:
using a contract-count threshold instead of a fixture name does not itself prove generalization.
