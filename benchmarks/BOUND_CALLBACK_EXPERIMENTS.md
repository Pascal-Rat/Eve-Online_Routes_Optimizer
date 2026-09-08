# Retaining bounds before the first solution

Explored on 2026-09-08 with Python 3.12.3 and OR-Tools 9.15.6755. The change retains
proof evidence from existing reward solves. It introduces no new solving stage,
solver parameters, budget allocation, candidate reduction or benchmark-specific rule.

## Why the change is sound

OR-Tools calls best-bound callbacks when its global objective bound improves, including
before a solution exists. It applies objective scaling and serializes callbacks under
the response-manager mutex. See the pinned upstream
[bound update implementation](https://github.com/google/or-tools/blob/v9.15/ortools/sat/synchronization.cc#L372-L437).

An early `UNKNOWN` response can instead contain a default zero: the
[presolve exit precedes objective initialization](https://github.com/google/or-tools/blob/v9.15/ortools/sat/cp_model_solver.cc#L2747-L2772).
Unconditionally consuming that field would risk a false certificate.

Each reward solve now records the smallest finite callback ceiling, converting doubles
conservatively to integer units. On `UNKNOWN`, it retains that explicit evidence. With
no event it preserves the previous ceiling or absence of a ceiling. Solution extraction
still requires `FEASIBLE` or `OPTIMAL`; those statuses keep the existing final response
handling. Feasibility checks and duration minimization receive no reward recorder.

A verified route may come from the route constructor or an earlier solve. If its reward
matches a retained ceiling, the application can prove optimality even though this
particular CP-SAT solve found no solution. The individual solver status remains `UNKNOWN`.

## Controlled verification

`tests/optimization/test_bound_updates.py` uses real CP-SAT solves with deterministic
interruption points, not tiny wall-time limits. It covers:

- Root propagation tightening a route/master ceiling from 300 to 200 before a solution.
- A callback ceiling of 100 closing a proof with an independently verified reward of 100,
  through the full route, master and batch paths.
- Presolve interruption producing no event and an unusable default zero.
- Empty solution fields, fresh recorder state, real zero, nonfinite events, conservative
  conversion above 2**53, and separation from duration refinement.
- Bounds checked against enumeration on 12 generated eight-variable knapsacks with an
  objective offset, repeated with one and four workers.

The tests do not claim that an interrupted solve is itself optimal. They check the
separate route and bound evidence used to establish the application certificate.

## Practical results

Six paired full-pipeline cases used a shared ten-second allowance, four workers, solver
seed 41, no independent reference stage and no duration refinement. Runs were sequential
and variant order alternated. The baseline recorded diagnostic events but did not forward
them to the new recorder. Thus this comparison isolates evidence handling, not total
callback overhead. All rewards, ceilings and proof statuses were identical:

| Case | Reward units, both | Ceiling units, both |
| --- | ---: | ---: |
| Empire DST, two hours | 9,439,689,400 | 13,083,713,700 |
| Clustered rolling | 21,525 | 27,533 |
| Fresh generated 4801 | 11,923 | 14,589 |
| Fresh generated 4802 | 200 | 6,116 |
| Fresh generated 4803 | 10,276 | 12,721 |
| Fresh generated 4804 | 5,373 | 5,373 |

Both variants proved one of six cases. The `UNKNOWN` reward solves in these runs emitted
no events: they stopped before objective initialization. Six additional unhinted Empire
master runs at 0.05, 0.1, 0.25, 0.5, 1 and 2 seconds also emitted no bound events.

For overhead, ten paired solves of generated case 4801's unhinted master compared an
actual absent callback with the production recorder, after one warm-up pair. Every run
returned the same optimum (14,589), 1,803 branches and 72 conflicts. Median solver time
was 223.13 ms without and 221.49 ms with the callback; ranges were 215.73–232.68 ms and
214.74–234.73 ms. This is consistent with timing noise, not evidence of a speedup.
In the instrumented pipeline runs, callback bodies used 0.492 ms across 67 events;
this excludes the C++/Python transition and synchronization overhead.

Local raw measurements and pipeline input/source hashes are in the ignored directory
`benchmarks/results/bound-callback-2026-09-08/`. No parameters were tuned after these runs.
The subsequent production edit only preserved the previous batch statistics behavior.

## Decision

Keep the small evidence-retention change. It repairs a real loss of valid information
and can close proofs in the demonstrated circumstances. It did not improve the six
measured pipeline outcomes and cannot recover bound progress that OR-Tools never emitted.
Further search or formulation improvements remain separate work.

Final verification: 283 tests passed with 89.40% branch-inclusive coverage; Ruff and
mypy passed. Frozen and Empire benchmark suites preserved their proven rewards. The
wheel built successfully and contains the recorder, typing marker, database and web assets.
