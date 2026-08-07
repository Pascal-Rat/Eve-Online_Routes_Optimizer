# Live-universe benchmark observations

Live Tranquility results are useful operational measurements, but they are **not regression tests**.
Public contracts change continuously, zKill observations age, network latency varies, and CP-SAT
multi-worker scheduling is intentionally nondeterministic. The reproducible release gate remains
[BENCHMARKS.md](BENCHMARKS.md).

## v1.1 cold acquisition observation -- 2026-08-06

This observation used bundled SDE build **3457062** and captured its snapshot at
`2026-08-06T16:58:14Z`. The HTTP caches were new for the run.

| Input | Value |
| --- | --- |
| Contract discovery | all 114 SDE regions |
| ESI contract workers | 4; pages inside each region sequential |
| Threat-envelope basis | Jita, high+low, 1 hour, 75 seconds/jump |
| zKill regions actually required | 24 |
| zKill lookback | 2 hours |
| zKill gate radius | 250 km |
| Acquisition wall time | **261.08 s (4m 21s)** |
| Public couriers observed | 454 |
| zKill rows observed | 567 |
| Gate-relevant retained events | 250 |
| Threat coverage | 24/24 regions successful; 0 incomplete |

The earlier v1 live observation, made at a different market time, took **1,510.34 s (25m 10s)**
while contract discovery and zKill both used all 114 regions and threat history used 24 hours. It
observed 419 couriers, 9,430 raw zKill rows and 3,454 retained gate events. The v1.1 combined fast
path was therefore about **5.8x faster (82.7% less wall time)** in these two observations.

That ratio should not be treated as a controlled microbenchmark because the live inputs differ. It
does show why the old wait was not SDE work: the ~3 MB distilled SDE was already local. The expensive
parts were regional ESI contract pagination plus a deliberately rate-spaced zKill request for every
region. V1.1 attacks both costs without pruning the contract proof scope:

- ESI still scans all 114 requested contract regions, but four independent regions can make progress
  concurrently; pagination within a region remains sequential and every request keeps cache/retry/
  rate-limit handling.
- zKill collection is a different problem: only transit regions a route could reach need threat
  evidence. The pre-threat BFS envelope reduced this one-hour high+low case from 114 to 24 requests.
- the default two-hour window better represents an active hard-avoid signal and avoids downloading
  much older history that is unlikely to describe a current gate camp.

## v1.2 contract-scope reduction -- 2026-08-06

V1.2 applies the same “do not request impossible space” principle to **contract** acquisition. SDE
build 3458726 has 114 regions in the bundled route universe, but region security composition is:

| Allowed pickup bands | SDE regions containing a matching system | Region-count reduction vs 114 |
| --- | ---: | ---: |
| High | 25 | **78.1%** |
| Low | 23 | **79.8%** |
| Null | 89 | 21.9% |
| High + low | 25 | **78.1%** |
| NPC Empire preset | 24 before selected-band intersection | **78.9%** |

This count reduction is deterministic local SDE work, not a timing estimate. ESI regions have very
different page counts (The Forge is much heavier than many others), so “78% fewer regions” must not
be advertised as “78% less wall time.” It does remove 89 unnecessary regional request roots from a
high-sec scan while keeping every mixed region that can contain a high-sec pickup.

## v1.2.1 canonical NPC-Empire baseline

The all-region observation above was also retained with its complete ESI response cache, which lets
us construct a realistic Empire-space benchmark without guessing from region counts. Public ESI
contract pagination itself identifies the pickup region even when a courier starts at an unsupported
player structure, so the frozen slice contains every observed courier from the 24 Empire regions.

| Frozen observation fact | Value |
| --- | ---: |
| All-region courier contracts | 454 |
| NPC-Empire courier contracts | **421** |
| All-region public-contract ESI pages | 151 |
| NPC-Empire public-contract ESI pages | **61** |
| Contract-page reduction | **59.6%** |
| Threat coverage | 24/24 Empire regions |
| Retained gate events | 250 |

The 59.6% value is a **request-volume** comparison from the same captured cache, not an invented
wall-time speedup. In particular, The Forge alone contributes 34 of the 61 Empire contract pages,
so the 78.9% region-count reduction cannot be translated linearly into elapsed time.

The observation was captured against SDE 3457062. Before freezing it against current bundled SDE
3458726, systems, route adjacency, NPC-station mappings and normal stargates were compared and found
identical (8,490 systems, 5,210 station mappings and 13,978 gates). The fixture and provenance live
in `benchmarks/empire_snapshot_2026-08-06.json` and `empire_baseline_manifest.json`.

More importantly, the Empire slice keeps the exact same eligible contract IDs as the former
all-region solve: 48 for BR and 96 for DST. It therefore removes irrelevant acquisition work without
turning the solver benchmark into an easier mathematical instance.

One 60-second/four-worker reference run on this frozen Empire fixture produced:

| Profile | Eligible | Incumbent | Upper bound | Gap | Status |
| --- | ---: | ---: | ---: | ---: | ---: |
| BR, 13,000 m³, 5 B, high+low | 48 | 30.0 M | 58.885 M | 96.28% | `feasible_not_proven` |
| DST, 62,500 m³, 10 B, high | 96 | 25.0 M | 182.806 M | 631.23% | `feasible_not_proven` |

Those v1.2.1 solver rows used the old open-end route semantics and are retained only as historical
measurements. They should not be compared as if the route geometry were identical to v1.3.

Incumbents and branch counts are not golden values because multi-worker CP-SAT is nondeterministic.
This fixed Empire fixture is now the canonical realistic performance comparison; the small synthetic
fixture remains the fast CI test that is expected to prove optimality on every run.

## v1.3 loop-aware Empire baseline

V1.3 makes **return to start** the default and pins that requirement explicitly in both frozen
benchmark runners. The same Empire fixture, candidate sets, threat policy, one-hour horizon, 75
seconds/jump, 30 seconds/action, locked collateral, 60-second solver budget, and four workers now
produce the following reference observation:

| Profile | Eligible | Selected | Incumbent | Upper bound | Gap | Solver wall time |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| BR, 13,000 m³, 5 B, high+low | 48 | 3 | 25.651527 M | 58.344302 M | 127.450% | 60.092 s |
| DST, 62,500 m³, 10 B, high | 96 | 6 | 39.357511 M | 176.598539 M | 348.704% | 60.037 s |

Both are independently verified `feasible_not_proven` solutions. Their loop-aware problem hashes
are `3fbb834f9cc9bd81cc314ff41558f789846f0273b35056afc76cf2be77da9fab` (BR) and
`638062bb2505871841160776fe0b1b7e17a7edfb0b9a789d3aa98b6b0d774dd0` (DST). Multi-worker incumbent
quality and branch count can vary, but those hashes make the mathematical inputs directly
comparable across future solver changes.

## v1.5 master/exact Empire baseline

V1.5 keeps the same frozen Tranquility observation, problem fingerprints and 58 M DST system
ceiling. The change is how that ceiling is used. Once the endpoint-system model is itself proven
optimal, its selected contracts become a reduced exact pickup/dropoff subproblem. A verified exact
route at the master reward closes the global proof; a proven-infeasible selection can instead return
a sufficient assumption core as a higher-order master cut.

The release run with the standard 60-second full-event fallback and four exact-subproblem workers is:

| Profile | Eligible | Selected | Exact reward | Upper bound | Gap | Total solver wall time |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| BR, 13,000 m3, 5 B, high+low | 48 | 3 | **25.651527 M** | **25.651527 M** | **0.000%** | 0.614 s |
| DST, 62,500 m3, 10 B, high | 96 | 5 | **58.000000 M** | **58.000000 M** | **0.000%** | 7.769 s |

Both first master selections are exactly feasible, so no core cut is needed on these two canonical
profiles and the 60-second monolithic fallback is never entered. DST now closes the proof that v1.4
left open: the verified 58 M route is a lower bound and the system-master 58 M optimum is an upper
bound on the unchanged mathematical problem. Equality proves the exact reward optimum.

The production test suite separately contains a higher-order cargo fixture in which every pair is
feasible but a master-selected larger set is not. That fixture forces the assumption-core path and
checks that the learned core is smaller than the rejected master selection.

## v1.4 proof-strengthened Empire baseline

V1.4 keeps exactly the same frozen snapshot, eligible contracts, loop geometry, threat/security
policy, capacities, collateral model and problem fingerprints. It changes how the same theorem is
proved. Before the full event model, dense cases derive optimistic two-contract incompatibilities,
compress mutually incompatible sets into clique cuts, and solve a route relaxation over distinct
endpoint systems.

For this fixture the proof preprocessor found:

| Profile | Pair incompatibilities | Clique cuts | Distinct relaxation systems | Proved relaxation ceiling |
| --- | ---: | ---: | ---: | ---: |
| BR | 650 | 33 | 44 | **25.651527 M ISK** |
| DST | 2,620 | 58 | 52 | **58.000000 M ISK** |

The bound model uses one deterministic worker even though the following full-route solve uses four.
In representative release-tree runs here, those two relaxation ceilings were proved in roughly
0.3 seconds for BR and 4.0 seconds for DST. The full solve receives the proved ceiling as one
additional reward constraint plus the pair/clique cuts, and also exposes its complete elapsed route
time as one redundant linear equality.

With the standard 60-second full-route budget and four workers:

| Profile | Eligible | Selected | Incumbent | Upper bound | Gap | Total solver wall time |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| BR, 13,000 m3, 5 B, high+low | 48 | 3 | **25.651527 M** | **25.651527 M** | **0.000%** | 8.109 s |
| DST, 62,500 m3, 10 B, high | 96 | 6 | 41.251527 M | **58.000000 M** | **40.601%** | 64.002 s |

BR is now `proven_optimal` over the complete eligible frozen snapshot. DST remains
`feasible_not_proven`, but its ceiling fell from v1.3's 176.598539 M to 58 M on the unchanged
problem, and its gap fell from 348.704% to 40.601% in these recorded runs. Total solver wall time
includes the bound prepass, so the DST row is about four seconds above its 60-second full-route
search limit.

This is the distinction the v1.3 worker experiments exposed: adding CPU mostly helped find better
incumbents, while the old upper bound barely moved. V1.4 attacks the mathematical relaxation itself.
The exact multi-worker incumbent can still vary from run to run, but the system-level bound solve is
kept single-worker and the frozen problem hashes remain the stable comparison anchor.

## v1.1 live solve observation

Both requested profiles were solved from that **same saved snapshot**, with locked collateral,
75 seconds/jump, 30 seconds/action, one matching threat event as the hard-avoid threshold, no
candidate cap, and four CP-SAT workers.

| Profile | Search | Eligible | Incumbent | Upper bound | Gap | Certificate |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| BR, 13,000 m3, 5 B, high+low | 60 s | 48 | 30.0 M | 58.889 M | 96.30% | `feasible_not_proven` |
| DST, 62,500 m3, 10 B, high | 60 s | 96 | 29.0 M | 182.808 M | 530.37% | `feasible_not_proven` |
| DST, same problem | 300 s | 96 | **43.5 M** | 182.806 M | 320.24% | `feasible_not_proven` |

The DST 60-second and five-minute searches used identical mathematical input and release code. With
multiple workers the exact incumbent trajectory can vary between runs, so the point is not the
particular 29 M starting result. The five-minute run demonstrates that extra search time can improve
the route found; it also demonstrates that elapsed time alone is not an optimality proof. The DST
upper bound barely moved, so this instance still had a large unresolved proof space after five
minutes.

For comparison, the frozen release cases prove their requested DST and BR optima in well under one
second on this environment. That difference is expected: exact pickup-and-delivery routing is
NP-hard, and the live candidate set can be much larger than the intentionally compact regression
universe.

### Worker-count sweep on the same DST problem

The following is a historical pre-v1.4 observation. It is retained to show why simply increasing
workers was not enough; current v1.4 proof-strengthening changes the bound behavior substantially.

To answer the worker question without changing the market underneath the solver, the saved DST
problem above was run for 60 seconds at each worker count. It has 96 eligible contracts, 62,500 m3
cargo, 10 B collateral, high-sec only, the same threat policy, and no candidate cap.

| Workers | Status | Incumbent | Upper bound | Gap | Branches |
| ---: | --- | ---: | ---: | ---: | ---: |
| 1 | `unknown` | -- | -- | -- | 142,763 |
| 2 | feasible | 40.000 M ISK | 182.808 M ISK | 357.0% | 141,080 |
| 4 | feasible | 32.706 M ISK | 182.806 M ISK | 458.9% | 261,891 |
| 8 | feasible | **55.000 M ISK** | 182.806 M ISK | 232.4% | 1,988 |

This is intentionally a single controlled sweep, not an average: CP-SAT's portfolio is
nondeterministic with multiple workers. Eight workers found 37.5% more reward than two on this run,
while four found less than two. More importantly for optimality, the 2/4/8 upper bounds barely
moved. On this dense instance extra workers materially improved the best route *sometimes*, but did
not solve the weak-bound/proof bottleneck. This is why the UI offers the worker count instead of
claiming a linear speedup.

## How to interpret these numbers

- A `feasible_not_proven` route is independently simulated and usable as a feasible plan; it is not
  claimed optimal.
- The upper bound is the mathematically meaningful measure of what remains unproved. If it stays
  almost flat while the incumbent improves, more time is buying route quality more than proof.
- Longer search is worth trying when the best route or bound is still moving. The localhost UI
  therefore offers 30-second through 10-minute presets, with one minute as the default.
- For repeatable performance/correctness comparisons, use the frozen scenarios rather than these
  live numbers.
