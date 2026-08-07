# CP-SAT and OR-Tools: developer guide

This guide explains both how this project uses OR-Tools CP-SAT and what the solver is doing beneath
the Python API. It assumes ordinary application-development experience, not operations-research
training.

## What CP-SAT solves

CP-SAT is Google's integer constraint-programming solver. Every decision in this project is encoded
with integer or Boolean variables: selecting a contract, choosing a route arc, event order, arrival
time, cargo, simultaneous parcel count, collateral, and reward. There is no floating-point variable
in the model.

The input is not a procedure such as “pick the next best contract.” It is a set of conditions every
valid route must satisfy plus an objective to maximize. CP-SAT searches assignments, rejects
inconsistent partial assignments early, keeps the best feasible assignment found, and tries to prove
that no better assignment exists.

The public status semantics are documented in Google's
[CP-SAT solver guide](https://developers.google.com/optimization/cp/cp_solver).

## Project model at a glance

For every optional contract $i$:

$$
x_i\in\{0,1\}
$$

means “select this contract.” Each pickup and delivery is a node. A Boolean $y_{uv}$ means the route
uses the transition from event $u$ to event $v$. The primary objective is:

$$
\max R=\sum_i r_i x_i + \text{mandatory reward}.
$$

Cargo, collateral, deadlines, listing expiry, precedence, the session horizon, and mandatory live
commitments constrain which combinations of $x$ and $y$ are possible. Exact formulas are in
[MATHEMATICAL_MODEL.md](MATHEMATICAL_MODEL.md).

## Why `AddCircuit` models one route

OR-Tools' circuit constraint requires one incoming and one outgoing selected arc for every node. An
unused optional event takes a self-loop; its self-loop is tied to `not x_i`. Used events instead form
one cycle containing the fixed artificial arc from dummy end back to start. Removing that artificial
arc leaves exactly one path:

```text
start -> selected/mandatory pickup and delivery events -> end
```

Pickup and delivery self-loops share the same selection variable, so a contract cannot keep only one
of its two actions. Mandatory nodes have no usable false self-loop. Explicit order constraints then
enforce pickup before delivery.

Required route systems are also mandatory circuit nodes, but have zero service time and no contract
selection variable. This is important: CP-SAT chooses where those systems belong in the joint event
order. The dummy end has a real solar system for loop/fixed-finish routes, so the final travel into
that system participates in the horizon exactly. A fully open route keeps the dummy end systemless
and therefore pays no artificial travel after its last event.

### Why CP-SAT rather than OR-Tools `RoutingModel`

This is intentionally modeled directly in CP-SAT. The application needs optional paired
pickup/delivery prizes, two collateral semantics, accepted mandatory commitments, listing/deadline
logic, exact integer reward, and--most importantly--a solver objective bound that can support an
explicit optimality certificate. OR-Tools' routing layer is excellent for conventional VRP local
search, but this project's proof contract and unusual resource state are a better fit for the
lower-level integer model. The metric-closure layer still borrows the standard routing idea of
precomputing action-to-action travel cost.

## Reification: constraints that apply only on a chosen arc

A route transition should update time and resources only when its arc is used. CP-SAT expresses that
with enforcement literals. Conceptually:

```python
model.add(arrival[v] == arrival[u] + service[u] + travel[u, v]).only_enforce_if(arc[u, v])
model.add(cargo[v] == cargo[u] + cargo_delta[v]).only_enforce_if(arc[u, v])
model.add(parcels[v] == parcels[u] + parcel_delta[v]).only_enforce_if(arc[u, v])
```

This is called **reification**: the Boolean arc activates the associated integer equalities. Similar
constraints propagate collateral and event order. Variable domains enforce nonnegative cargo and
the configured capacity/budget everywhere. If a simultaneous-contract limit is configured, the
parcel domain is `0..limit`; pickup adds one and delivery removes one. Accepted-but-unpicked locked
contracts do not count until their pickup because the constraint represents parcels in the trunk,
not contractual commitments.

The implementation uses equality for time propagation. An inequality would allow the solver to
invent waiting time, which would change rolling-deadline semantics.

## Metric closure rather than gate-edge decisions

The CP model does not contain one variable per stargate. Before modeling, local breadth-first search
computes the exact minimum jump distance between relevant action systems under the selected security,
manual-avoid, and gate-threat policy. Required waypoint systems and the terminal system are included
in that relevant set. CP-SAT then chooses among event-to-event arcs with those exact distances.

This separation keeps the combinatorial model smaller while preserving route correctness. After a
solve, independent verification reconstructs a concrete shortest stargate path for every contract,
waypoint, and final travel leg. This is also why a zero-cargo, zero-contract problem can still produce
a useful route when required systems or a finish system are supplied.

## What happens inside CP-SAT

CP-SAT is more than a generic backtracking loop. At a high level it combines several mature solving
ideas:

1. **Presolve** simplifies variables and constraints, detects some contradictions, and derives
   equivalent tighter forms before the main search.
2. **Constraint propagation** shrinks variable domains whenever a constraint makes values
   impossible.
3. **SAT/CDCL reasoning** represents Boolean structure as clauses, learns new conflict clauses from
   failed branches, and backjumps past irrelevant decisions.
4. **Lazy clause generation** lets integer/global constraints explain their propagation back to the
   SAT engine, so learned clauses are reusable across the search.
5. **Linear relaxation and cuts** provide objective bounds and rule out collections of integer
   assignments that cannot beat the incumbent.
6. **Branching and portfolio search** explore decisions using complementary strategies. With multiple
   workers, strategies can cooperate on incumbents, bounds, cuts, and subproblems.
7. **Large-neighborhood search (LNS)** may fix much of a known solution and re-optimize a neighborhood
   to improve the incumbent. A good incumbent is useful, but optimality still requires a closed bound.

Google's CP-SAT-LP paper describes the integral CP-SAT architecture, its SAT core, simplex/linear
relaxation, cuts, presolve, portfolio workers, and LNS in much more detail:
[The CP-SAT-LP Solver](https://drops.dagstuhl.de/storage/00lipics/lipics-vol280-cp2023/LIPIcs.CP.2023.3/LIPIcs.CP.2023.3.pdf).

The practical consequence is important: the solver can return a very good route quickly but spend
longer proving that nothing better exists.

## Incumbent, bound, and proof

For maximization, CP-SAT maintains:

- an **incumbent** $L$: reward of the best feasible route found; and
- a **best upper bound** $U$: a mathematically valid ceiling on any route not yet eliminated.

Therefore:

$$
L\le R^*\le U.
$$

When $L=U$, the reward optimum is proved. If the time limit expires while $L<U$, the route can still
be feasible and independently verified, but the certificate is `feasible_not_proven`. This project
never relabels such an incumbent as optimal.

| CP-SAT outcome | Project certificate | Meaning |
| --- | --- | --- |
| `OPTIMAL` | `proven_optimal` | feasible incumbent and objective bound meet |
| `FEASIBLE` | `feasible_not_proven` | incumbent exists; a better route may remain |
| `INFEASIBLE` | `proven_infeasible` | no assignment satisfies the modeled mandatory constraints |
| `UNKNOWN` / no incumbent | `unknown` | time/search ended without a solution or proof |

V1.5 adds a composite route to the first row. A system-master CP-SAT solve may return `OPTIMAL`, and
a second exact reduced model may produce an independently verified courier route with exactly that
reward. The route is the lower bound $L$ and the master optimum is the upper bound $U$. When they
match, the certificate uses solver status `DECOMPOSITION_OPTIMAL` and `proven_optimal`. No timed-out
status is being promoted; two separately valid bounds have met.

`scope_untruncated` is separate. CP-SAT may prove a candidate-capped problem optimal, but the project
will not call that a global eligible-set proof.

## Two-stage lexicographic solving

Reward is proved before route duration is optimized. On the full-model path, a second bounded solve
fixes the exact reward and minimizes finish time. On a decomposition proof, the selected set already
has globally optimal reward, so a reduced fixed-selection exact solve may minimize finish time. This
avoids mixing units through an unsafe hand-tuned weighted objective.

A timeout in the second stage does not invalidate the first-stage reward proof. It only means the
returned reward-optimal route is not proved to be the fastest among all equally rewarding routes.

## V1.4: tightening the upper bound before full route search

The hard Empire benchmarks showed that incumbent search was not the main proof bottleneck. On the
v1.3 DST loop instance, CP-SAT could find a 39.357511 M ISK route while still reporting a
176.598539 M upper bound after 60 seconds. More workers sometimes improved the route but left that
ceiling almost unchanged.

V1.4 therefore runs a proof-strengthening prepass on candidate sets of at least 20 contracts.

First, every pair of contracts is projected out of the full route. There are only six legal event
orders for two pickup/delivery pairs. The preprocessor checks all six using exact shortest-path
travel while tracking only those two contracts' cargo, rolling collateral and optional parcel
count. It deliberately removes active load, other jobs and required waypoints, so the projection is
optimistic. If even this easier two-contract problem cannot fit the horizon/resources, then the full
problem can safely add `x_i + x_j <= 1`. Locked-collateral conflicts provide the same kind of cut.

Mutually incompatible pairs are compressed into deterministic clique constraints. A clique of ten
contracts, for example, gives one `sum(x_i) <= 1` row instead of requiring the relaxation to
rediscover those exclusions indirectly through route state.

Second, a separate CP-SAT model routes only the distinct endpoint systems. A selected contract
requires both endpoint systems to be visited, but the relaxation drops global pickup/delivery
ordering, global cargo/parcel state, rolling collateral state and individual deadlines. It retains
the route horizon, service-action count, terminal shape, locked collateral, mandatory endpoint
systems and the valid incompatibility cuts. Repeated system visits from a real route can always be
shortcut through the metric closure without increasing travel, so every exact route has a feasible
image in this relaxed model. Consequently its optimum is a rigorous ceiling on exact reward.

If this prepass times out after finding a relaxation solution, the program uses CP-SAT's **best
objective bound**, never the relaxation incumbent, as the ceiling. If the prepass cannot supply a
valid bound, the exact solver simply continues without it. The prepass uses one deterministic worker
and at most five seconds by default; the full solver still honors the requested portfolio worker
count.

Finally, the exact event model now writes its complete elapsed duration in one redundant equation:

$$
t_{end}=\sum_{(u,v)} travel_{uv}y_{uv}
        +service\left(mandatoryActions+2\sum_i x_i\right).
$$

The existing reified arrival equations already imply this for integer routes. Writing it explicitly
gives the linear relaxation a direct global relationship between selecting reward and consuming the
time budget.

On the unchanged frozen Empire problems, the system relaxation itself proves a 25.651527 M ISK BR
ceiling and a 58 M ISK DST ceiling. The full BR model now proves the matching feasible reward
globally optimal; the DST proof remains open but starts from a radically smaller ceiling. See
[LIVE_BENCHMARKS.md](LIVE_BENCHMARKS.md) for the controlled comparison.

## V1.5: use the system bound as a master

V1.4 discovered the key information for DST but did not exploit the relaxation's selected contract
set. It proved that 58 M ISK was the ceiling, then asked the 194-node full event model to rediscover
a compatible pickup/dropoff subset and route from scratch. V1.5 separates those two questions.

Once the endpoint-system master itself returns `OPTIMAL`, its selected contracts are copied into a
reduced instance of the exact pickup/delivery model. All those selections are positive CP-SAT
assumptions; every other optional contract is absent. Active shipments, required waypoints, terminal
shape, metric closure, cargo, collateral, parcel count and deadlines remain exact. The resulting
model is usually tiny compared with the full event circuit.

There are three proof-safe outcomes:

1. **Exact feasible at the master reward.** Independent route simulation verifies the route against
   the original full prepared problem. Exact feasible reward equals rigorous master optimum, so the
   global reward proof closes immediately.
2. **Exact infeasible.** CP-SAT supplies a sufficient core of positive selection assumptions. The
   solver may shrink it by deletion tests, but removes a literal only after another `INFEASIBLE`
   result. Because deleting optional courier jobs cannot increase time or resource use and metric
   shortcutting cannot lengthen travel, no full route can contain every contract in that core. The
   master safely learns `sum(core selections) <= len(core) - 1` and resolves.
3. **Anything unresolved.** `FEASIBLE`/`UNKNOWN` master status, an `UNKNOWN` exact subproblem, the
   iteration limit or the decomposition wall limit cannot justify a new proof. The complete event
   model runs with every already-valid ceiling and learned core attached.

The master deliberately stays single-worker for reproducibility and stability. Each master solve is
allowed up to ten seconds by default; exact selection/core work gets up to two seconds per iteration,
inside a 20-second total decomposition envelope. These are search budgets only. They do not delete a
contract or weaken proof scope.

On the unchanged 96-eligible frozen Empire DST profile, the first master optimum is five contracts
worth 58.000000 M ISK. The reduced exact model routes those five and the independent verifier accepts
the route, so v1.5 proves 58.000000 M globally optimal in 7.769 seconds on the recorded release run.
The 48-eligible BR profile similarly proves 25.651527 M in 0.614 seconds. The configured 60-second
full-route fallback is not entered in either run.

## Hints and safe performance work

The optimizer builds a conservative greedy sequential route and uses its selected-contract set as a
CP-SAT **selection hint**. Before suggesting each additional contract it reserves a deterministic
nearest-next path through every still-required waypoint and the loop/fixed terminal, so the hint does
not consume the horizon that the declared trip ending still needs. A hint proposes starting values;
it neither forces those values nor removes any alternatives. CP-SAT remains free to replace the set
and interleave events differently, and the final bound still supplies the proof.

Current proof-preserving performance work also includes:

- sparse direct-distance lower bounds before the full retained endpoint closure is built;
- safe candidate elimination using capacity, collateral, reachability, horizon, expiry, and direct
  deadline lower bounds;
- stronger pickup/delivery event windows: a delivery cannot precede the exact
  start -> pickup -> delivery shortest-path lower bound, and a pickup must leave enough horizon to
  reach and service its own delivery;
- an explicit selected-contract inequality requiring delivery arrival to be at least pickup arrival
  plus pickup service plus the exact metric-closure pickup -> delivery distance;
- impossible event-arc pruning from those earliest/latest bounds plus direct structural precedence;
- an explicit service-action cardinality inequality that is redundant with exact time propagation
  but gives presolve/linear relaxation an immediate route-independent bound;
- a global elapsed-time equality tying the exact event-route arcs and selected action count directly
  to `finish_time`;
- resource-aware two-contract incompatibility projection plus deterministic clique cuts;
- a single-worker endpoint-system master whose rigorous objective bound is fed into dense exact
  solves without deleting any candidate;
- master-guided reduced exact routing plus rigorously proven higher-order assumption-core cuts;
- bounded caches for policy-specific jump closures and concrete paths; and
- deterministic single-worker proof benchmarks.

The optional `max_candidates` cap is different: it deletes modeled opportunities and therefore marks
the proof scope truncated.

## Reproducibility and workers

The library/CLI `num_workers=1` default remains the reproducible baseline for regression benchmarks.
The localhost UI defaults to **4 workers** because interactive hard instances benefit from CP-SAT's
portfolio search; it offers 1, 2, 4, and 8. Portfolio scheduling and elapsed timing can vary across
machines and runs. Regardless of worker count, proof still requires equality of a verified feasible
reward and a rigorous upper bound. That can come from one exact `OPTIMAL` solve or the v1.5
master/exact bound match described above.

The following controlled worker sweep is retained as a **historical pre-v1.4** observation on the
same saved 96-eligible-contract DST market. It gives a useful example of why there is no universal
worker multiplier, but its old 182 M bound should not be mistaken for current v1.4 behavior:

| Workers | 60 s outcome | Incumbent | Upper bound | Gap |
| ---: | --- | ---: | ---: | ---: |
| 1 | `unknown` | none | not reported | -- |
| 2 | feasible | 40.000 M ISK | 182.808 M ISK | 357.0% |
| 4 | feasible | 32.706 M ISK | 182.806 M ISK | 458.9% |
| 8 | feasible | **55.000 M ISK** | 182.806 M ISK | 232.4% |

Eight workers found 37.5% more reward than two in that particular run, but four happened to find a
worse incumbent than two. The 2/4/8 upper bounds were essentially identical. More workers therefore
bought search diversity and route quality here, not materially faster proof. Repeat runs can differ;
use the frozen single-worker cases for reproducible regression timing. The full setup is recorded in
[LIVE_BENCHMARKS.md](LIVE_BENCHMARKS.md).

The web UI starts with a 60-second primary limit and offers 30 seconds, 1, 2, 5, or 10 minutes. There
is real value in a multi-minute search when the incumbent or upper bound is still moving. There is no
magic duration, however: if the bound is nearly flat across successive runs, extra wall time can buy
very little. The result remains useful because `feasible_not_proven` reports both incumbent and the
rigorous upper bound rather than pretending elapsed time proves quality. After a reward proof, the
web's secondary fastest-tied-route stage is capped at five seconds so it cannot unexpectedly double
the interactive wait.

The solver version, worker count, elapsed time, branches, objective, bound, gap, verification flags,
and mathematical input fingerprint are written into the certificate.

The project deliberately avoids cargo-cult parameter tuning. A v1.2 experiment that additionally
pinned unused optional-node state and supplied a complete greedy circuit hint was benchmarked on the
same hard problem: at eight workers the 60-second incumbent fell from 55 M to 42 M ISK while the
upper bound improved by only about 0.27%. Those changes were rejected rather than shipped. Future
proof work should continue strengthening the master or generating targeted valid cuts where frozen
benchmarks demonstrate a real bottleneck, rather than simply turning more CP-SAT knobs.

## Reading the implementation

| File | What to inspect |
| --- | --- |
| `planning.py` | policy exclusions, exact/safe reductions, sparse lower bounds, retained metric closure |
| `bounds.py` | pair/clique necessary conditions, reusable endpoint-system master and learned no-good rows |
| `sde.py` | policy-filtered BFS, closure/path caches, concrete shortest paths |
| `solver.py` | master/exact loop, assumption cores, full event fallback, objectives and bound extraction |
| `verification.py` | independent route and resource simulation |
| `reference_solver.py` | exhaustive small-instance oracle independent of CP-SAT |
| `proof.py` | canonical mathematical-input fingerprint |
| `reporting.py` | stable certificate and route JSON |

For a live search log:

```bash
eve-courier solve \
  --snapshot contracts.json \
  --start Jita \
  --cargo-m3 62500 \
  --collateral-isk 10B \
  --hours 1 \
  --solver-log \
  --time-limit 300 \
  --output plan.json
```

The log is diagnostic. The durable result is `plan.json`, especially `certificate.status`,
`objective_units`, `best_bound_units`, `scope_untruncated`, and the verification flags.

## Common misconceptions

- **“It found a route, so it is optimal.”** No. A feasible incumbent is not a proof.
- **“A zero-looking displayed gap is enough.”** Use integer objective/bound and status; rounded UI
  text is not authoritative.
- **“A hint makes the solver heuristic.”** No. It changes the starting point, not the feasible set or
  proof requirement.
- **“More workers are always deterministic and faster.”** They can help, but portfolio behavior and
  overhead are instance- and machine-dependent.
- **“Independent verification proves optimality.”** Verification proves the extracted route obeys
  the model. CP-SAT's closed objective bound proves optimality.
