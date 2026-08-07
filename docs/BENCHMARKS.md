# Benchmark suite

The release has two complementary frozen benchmark layers. The small synthetic universe is the fast
CI **proof regression**; the real NPC-Empire observation is the canonical **performance baseline**.
Both exercise preparation, gate-threat filtering, CP-SAT, certificates and independent feasibility
verification without letting a moving contract market invalidate comparisons between code changes.

## Why the universe is frozen

A live ESI benchmark is not repeatable: contracts disappear, pagination moves, prices change, and
threat observations age while the test is running. It also cannot distinguish a performance
regression from a different market instance.

`benchmarks/frozen_universe.json` therefore pins:

- an 18-system high/low/null stargate graph;
- SDE identity and snapshot time;
- 24 public courier contracts;
- three gate-threat observations with smartbomb, suicide-gank, HIC, carrier, and camp signatures;
- exact capacity/collateral/horizon/travel values; and
- an explicit return-to-start loop; and
- the selected threat/security policy for each scenario.

This fixture is deliberately small enough for routine CI while still requiring subset selection,
pickup/delivery ordering, threat-induced graph filtering, and an objective proof.

## Canonical realistic baseline: NPC Empire

`empire_snapshot_2026-08-06.json` is a frozen slice of the real Tranquility observation captured at
`2026-08-06T16:58:14Z`. It contains all 421 observed courier contracts whose ESI pickup region was
one of the 24 faction-owned high/low NPC-Empire regions, plus all 250 retained gate-threat events.
Threat coverage is complete for exactly those 24 regions.

This is now the preferred baseline for measuring solver changes because its preparation density and
candidate counts came from a real market rather than a hand-sized synthetic graph. The two standard
profiles deliberately have **no candidate cap**:

| Profile | Cargo | Collateral | Horizon | Route | Security | Eligible contracts |
| --- | ---: | ---: | ---: | --- | --- | ---: |
| DST | 62,500 m³ | 10 B ISK | 1 hour | Jita loop | high | 96 |
| Blockade runner | 13,000 m³ | 5 B ISK | 1 hour | Jita loop | high + low | 48 |

Both start and finish in Jita, use locked collateral, 75 seconds/jump, 30 seconds/action, a one-event
hard-avoid threshold, and all six focused threat categories (suicide gank, smartbomb, HIC/tackle,
carrier, multi-pilot camp and hauler loss). Four workers and a 60-second full-route search are the
reference fallback run. V1.5 first gives each deterministic single-worker master solve up to ten
seconds inside a 20-second decomposition envelope. Reduced exact/core work uses the requested four
workers. Reported solver wall time includes every master and exact-subproblem solve actually used.

The v1.5 release run on this environment is:

| Profile | Status | Incumbent | Upper bound | Gap | Solver wall | Branches |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| Blockade runner | `proven_optimal` | **25.651527 M ISK** | **25.651527 M ISK** | **0.000%** | 0.614 s | 7,095 |
| DST | `proven_optimal` | **58.000000 M ISK** | **58.000000 M ISK** | **0.000%** | 7.769 s | 62,088 |

Both routes passed independent feasibility verification and close in the first master/exact
iteration. For DST, the optimal system master selects five contracts worth 58 M ISK. The reduced
exact event model routes all five, so the verified 58 M lower bound meets the rigorous 58 M master
upper bound. Neither profile enters the configured 60-second full-event fallback. The mathematical
input fingerprints remain unchanged because decomposition changes the proof method, not the problem.

For comparison, the v1.4 run used the same input but treated the system relaxation only as a passive
ceiling:

| Profile | Status | Incumbent | Upper bound | Gap | Solver wall |
| --- | --- | ---: | ---: | ---: | ---: |
| Blockade runner | `proven_optimal` | 25.651527 M ISK | 25.651527 M ISK | 0.000% | 8.109 s |
| DST | `feasible_not_proven` | 41.251527 M ISK | 58.000000 M ISK | 40.601% | 64.002 s |

For direct comparison, the v1.3 loop-aware 60-second/four-worker reference run was:

| Profile | Status | Incumbent | Upper bound | Gap | Branches |
| --- | --- | ---: | ---: | ---: | ---: |
| Blockade runner | `feasible_not_proven` | 25.651527 M ISK | 58.344302 M ISK | 127.450% | 896,204 |
| DST | `feasible_not_proven` | 39.357511 M ISK | 176.598539 M ISK | 348.704% | 198,883 |

The solver-reported wall times were 60.092 s and 60.037 s respectively. Both incumbents passed
independent feasibility verification. Multi-worker incumbent/branch trajectories are not golden
values; the immutable loop-aware problem fingerprints are
`3fbb834f9cc9bd81cc314ff41558f789846f0273b35056afc76cf2be77da9fab` (BR) and
`638062bb2505871841160776fe0b1b7e17a7edfb0b9a789d3aa98b6b0d774dd0` (DST).

The source observation used SDE 3457062. Before normalizing the fixture to the current bundled SDE
3458726, the benchmark preparation verified that all 8,490 systems, stargate adjacency, 5,210 NPC
station mappings and 13,978 normal stargates were identical between the two route databases. The
provenance and hashes are in `empire_baseline_manifest.json`.

Run the realistic baseline explicitly. It is not part of routine pytest because the fallback budget
still permits one-minute full-event searches on instances where decomposition cannot close:

```bash
PYTHONPATH=src .venv/bin/python -m benchmarks.run_empire --time-limit 60 --workers 4
```

Multi-worker CP-SAT is portfolio search, so incumbent reward and branch count are observations, not
golden assertions. Stable comparison points are the frozen problem, eligible count, proof scope,
objective bound/gap and whether independent feasibility verification succeeds.

## Scenarios

| Scenario | Cargo | Collateral | Horizon | Security | Selected threat categories |
| --- | ---: | ---: | ---: | --- | --- |
| DST | 62,500 m³ | 10 B ISK | 1 hour | high | suicide gank, smartbomb, camp, hauler loss |
| Blockade runner | 13,000 m³ | 5 B ISK | 1 hour | high + low | suicide gank, smartbomb, HIC, carrier, camp, hauler loss |

Both use 75 seconds per jump, 30 seconds per action, one CP-SAT worker, no candidate cap, and at
least one matching event as the hard-avoid threshold.

## Run

From an editable development install:

```bash
PYTHONPATH=src .venv/bin/python -m benchmarks.run_frozen --time-limit 10
```

The automated regression is:

```bash
.venv/bin/pytest tests/test_benchmarks.py
```

The runner exits nonzero unless every scenario returns `proven_optimal`, has untruncated scope, and
passes independent feasibility verification.

## Small-fixture reference result

The compact release-tree run, with loop closure explicit in the fixture runner, produces:

| Scenario | Status | Eligible | Selected | Reward | Solve + prepare |
| --- | --- | ---: | ---: | ---: | ---: |
| DST | `proven_optimal` | 12 | 3 | 250 M ISK | 0.158 s |
| Blockade runner | `proven_optimal` | 12 | 2 | 165 M ISK | 0.135 s |

Elapsed time is informational and will differ by CPU, operating system, Python/OR-Tools build, and
background load. The stable regression requirements are the proof status, untruncated scope,
independent feasibility, and nonempty solution--not a wall-clock threshold or one particular route
among objective ties.

## What this benchmark does not claim

- It does not estimate how many live contracts will be eligible.
- It does not promise that a large all-region snapshot will prove within ten seconds.
- It does not measure real DST or blockade-runner align/warp/dock performance; the travel model is a
  declared synthetic input.
- It does not validate the empirical predictive power of threat categories.
- It does not replace live SDE/ESI compatibility tests.

Its purpose is narrower and auditable: the two requested model profiles have small regression cases
that repeatedly reach a genuine global optimum certificate through the same code used in production.
Use the frozen Empire fixture above for realistic solver performance, and
[LIVE_BENCHMARKS.md](LIVE_BENCHMARKS.md) for the provenance and dated live observations.
