# Optimization and proof

The objective is maximum gross courier reward over the declared snapshot and route policy.
A feasible route supplies a lower bound. A sound relaxation or completed exact search supplies
an upper bound. Equality proves the reward optimum; a timeout alone proves nothing.

For a worked example without the mathematical terminology, start with
[how the solver finds and proves a route](HOW_THE_SOLVER_WORKS.md).

## Complete event model

`optimization/models/pickup_delivery.py` exposes the mathematical structure directly: event catalog, circuit,
arrival/order/resource variables, contract constraints, reward objective, hints and extraction.

For optional contract `i`, Boolean `x_i` selects both pickup and delivery. Active shipments have
mandatory delivery events and, if unpicked, mandatory pickup events. Required systems have mandatory
zero-service waypoints. Start and end are distinct events even for a loop.

A circuit over these events uses a fixed artificial end-to-start arc. Optional events use
self-loops when their contract is skipped. The remaining arcs form one route from start to end.
Infeasible transitions are omitted using optimistic earliest-arrival/latest-departure windows.
Latest arrivals reserve service, the parcel's delivery when the event is a pickup, and shortest
travel to the required terminal. Equality at the horizon remains feasible; free finishes reserve
no final travel. Known incompatible contract pairs also exclude every arc between their events
before time and resource equations are created. The full model receives the same selection cuts
as the master, so these reductions do not depend on rediscovering pair conflicts in presolve.
The end represents the original start, a fixed destination, or a free finish with zero final travel.

For a used real arc from `u` to `v`:

```text
t_v     = t_u + service(u) + seconds_per_jump * J(system(u), system(v))
order_v = order_u + 1
```

`t_u` and `t_v` are arrival times; `J` is the exact shortest-path metric closure of the permitted graph.
Equality prevents invented waiting from changing relative delivery windows. Pickup precedes delivery
by event order, including when travel and service are zero. Start arrival is zero; finish arrival
cannot exceed the horizon.

Cargo, parcels and rolling collateral propagate by each destination event's resource change.
Their variable domains enforce capacity after every action. Active cargo/parcels initialize the
start; all active collateral starts locked. Accepted-but-unpicked pickups do not lock funds twice.
Every parcel is indivisible: one pickup loads its entire volume and one delivery unloads it.
No part of a parcel can be left behind or carried on a separate trip.
Finish has no undelivered parcels or cargo. Locked mode instead constrains total selected plus active
collateral at departure. [Domain rules](DOMAIN.md) specify expiry and deadline semantics.

For mandatory reward `R_A` and optional rewards `r_i`, the objective is:

```text
maximize R
R = R_A + sum(r_i * x_i for each optional contract i)
```

Each `x_i` is 1 when selected and 0 otherwise, so each selected reward is counted once.

A redundant total-duration equality telescopes travel plus service across the path. Necessary
resource-work and incompatibility inequalities strengthen propagation without changing feasibility.
The event model and system master share these constraints through `optimization/models/selection_bounds.py`.

## Proof-preserving search

`optimization.RouteOptimizer.solve` coordinates these steps and constructs the certificate once:

1. `optimization/search/route_insertion.py` builds and repairs routes. Only independently replayed feasible improvements
   become incumbents. Hints guide search; an incumbent reward floor is valid because its route is
   already known feasible.
2. For at least 20 optional contracts, `optimization/search/contract_selection.py` builds the endpoint-system master and
   checks its proposed selections with exact searches. Valid bounds and cuts survive every iteration.
3. If the master remains open, route diversification seeds distinct haul lanes and repairs two-job
   removals within the remaining budget. The complete event model then receives the bounds, cuts
   and best verified incumbent, retaining the entire eligible optional pool. Its result can never
   replace that incumbent with a worse route.
4. After reward is proven, an optional bounded search minimizes finish time while retaining the
   best verified route. On the decomposition path, the selected contract set stays fixed; there is
   no global fastest-route claim across other reward ties. The full event path permits those ties.

The smaller exact searches are deliberate specializations, not weaker substitutes:

| Search | Exactness condition and result |
| --- | --- |
| System master | One visit per selected endpoint/mandatory system; drops global action order and resource trajectories. Supplies a reward ceiling, never an unverified route. |
| Subset search | Bitmask state tracks location, picked and delivered sets with earliest-arrival dominance. Limited to supported small subsets; binding rolling completion windows, active shipments and required waypoints use the event oracle. An interrupted subset search supplies no upper bound. |
| Haul-lane batches | One origin/destination lane, locked collateral, no active shipments/waypoints and nonbinding completion deadlines. Unloading on arrival yields an exact batch normal form. Capacity, trips, service and finish remain constrained. |
| Event oracle | Forces the master's selected contracts through positive assumptions in a reduced event model, preserving every mandatory obligation and route requirement. A feasible assignment is replayed against the full problem. |

A real route projects into the system master by retaining endpoint visits and shortcutting repeats.
Triangle inequality cannot increase travel, so the master's optimum is an upper bound. Its feasible
objective is not an upper bound: only a completed optimum or the solver's valid best bound is used.
The single-worker master uses CP-SAT linearization level 2, including Boolean constraints in its
linear relaxation. The complete event model and assumption oracle retain their separate settings.

If a selected set is proven infeasible, every superset is infeasible: removing optional jobs only
reduces service/resources, and shortcutting cannot lengthen travel. The event oracle uses single-worker
assumption search and optionally shrinks sufficient infeasibility cores. Only proven deletions
strengthen a core; an `UNKNOWN` feasibility check never justifies an infeasibility cut.
Completed subset searches can also supply local reward ceilings and sufficient infeasible sets.

## Necessary inequalities

Pair checks enumerate all six precedence-respecting orders of two jobs. They retain the pair's
capacity, parcel, collateral, deadline and terminal requirements while optimistically omitting
other work. An impossible pair yields `x_i + x_j <= 1`; mutually incompatible cliques yield
`sum(x_i) <= 1`. Missing a clique only loses a strengthening opportunity.

Resource transport work relates each parcel's demand times its required carrying distance to
available capacity times total travel. For symmetric metrics, signed distance-to-pivot potentials supply additional
necessary work inequalities. Count and dual-feasible packing transforms account for unusable spare
capacity. A 1-Lipschitz potential with terminal change `delta` has total positive movement at
most `(travel + delta) / 2`; multiplying by capacity bounds the transported demand along that
potential. This argument requires the symmetric metric check in the implementation.
These are optimistic projections; none may impose a guessed visit limit or an arbitrary
candidate reduction. Small exhaustive packing and route tests guard their validity.

The system master also counts whole crossings of distance layers around the start and fixed
terminal. For a cut at distance `k` from a pivot, let `out_k` and `in_k` be nonnegative integer
crossings. A selected parcel whose pickup and delivery straddle that cut must cross while carried:

```text
sum(outward parcel demands) <= resource capacity * out_k
sum(inward parcel demands)  <= resource capacity * in_k
out_k - in_k = terminal side - start side
seconds_per_jump * sum(out_k + in_k over distance layers) + service <= horizon
```

For a free finish either terminal side is allowed. Cargo, parcel-count and rolling-collateral
packing transforms each impose their own demand inequality on the same crossing variables.
Active cargo originates at the start when already picked; active collateral originates there even
before pickup because those funds are already committed. Consecutive layers without an action
endpoint have identical obligations and may share crossing variables weighted by their width.

These inequalities are necessary because distance to a pivot changes by at most one per jump in
a symmetric metric. Each potential bounds actual travel independently; bounds from different
pivots must never be summed or charged against the master's shortcut tour. Asymmetric or numerically
unsafe cases omit this optional strengthening. For three 40 m³ parcels in a 100 m³ hold, two
outbound trips are necessary. A loop also needs two returns. A three-jump allowance cannot deliver
all three across a one-jump lane. An aggregate capacity-work relaxation can miss that integer
return-trip requirement. Such optimism affects only the reward ceiling, never parcel handling in
an accepted route. The strengthened master still relaxes ordering and exact packing; it is not a
complete route model.

## Proof interpretation and budgets

Every returned feasible route is replayed by `verification/route_replay.py`, which recomputes travel, policy,
precedence, resources, deadlines, required systems and final travel without trusting CP-SAT state.
For supported small locked problems, `verification/exhaustive_optimum.py` independently enumerates every feasible state to check
the reward optimum. It intentionally remains separate from production subset search.

| Certificate | Meaning |
| --- | --- |
| `proven_optimal` | Verified reward equals a rigorous upper bound. Check `scope_untruncated` separately. |
| `feasible_not_proven` | Verified incumbent with an open reward gap. |
| `proven_infeasible` | Mandatory model proved impossible. |
| `unknown` | No verified route and no infeasibility proof. |

Integer rewards come from solver variables, not floating objective values. Bound conversion rounds
conservatively, including values above the exact integer range of a double. Contradictory bounds,
invalid models and failed independent verification raise errors instead of becoming empty routes.
Reward maximization solves retain explicit best-bound callbacks even when CP-SAT returns `UNKNOWN`
before finding a solution. A verified incumbent from another search can match that ceiling and
close the reward proof. Without a callback, an `UNKNOWN` response's default zero is ignored.
Recorders are fresh for each reward solve and are never attached to duration minimization or
fixed-selection feasibility checks. Feasible and optimal responses retain their existing bound
handling, including exact integer objective reads at optimality.
The configured integer objective uses no positive relative-gap tolerance; OR-Tools 9.15's default
absolute tolerance is below one integer reward unit. Changing these tolerances requires revisiting
the interpretation of `OPTIMAL`. Hints are guidance, and assumption cores are sufficient conflicts,
not necessarily minimal ones. These contracts are documented in the pinned upstream
[CP-SAT model protocol](https://github.com/google/or-tools/blob/v9.15/ortools/sat/cp_model.proto)
and [solver parameters](https://github.com/google/or-tools/blob/v9.15/ortools/sat/sat_parameters.proto).

`SolverConfig.max_time_seconds` is one cooperative allowance for `RouteOptimizer.solve`, including
incumbent construction, model building, proof search, diversification, duration refinement and the
optional independent reference check. Snapshot loading and graph preparation happen outside it.
Atomic construction/replay operations and CP-SAT cleanup can slightly overrun the deadline; this is
not a hard process cutoff. An interrupted reference enumeration never claims a completed check.

Defaults cap decomposition at 20 seconds and half the remaining overall allowance, with at most
eight iterations and up to 10 seconds per single-worker master solve. An oracle normally receives
at most two seconds, clipped to the remaining phase allowance. Diversification receives at most
one second and a quarter of the remaining overall allowance, with checks inside insertion loops.
Secondary refinement has a phase cap within the same overall allowance. `max_time_seconds=None`
removes the overall deadline; phase caps still apply. Certificate wall time sums solver search
phases; benchmark elapsed time measures the complete operation.

Multiworker CP-SAT trajectories are nondeterministic. A longer independent solve can choose a
different trajectory and return a worse incumbent even though incumbents never worsen within one
solve. A stronger relaxation need not produce a tighter bound under every short wall-time cutoff:
extra presolve work can consume time before the solver establishes a useful bound.

The problem fingerprint includes contracts, constraints, obligations, provenance and the jump
matrix. It identifies the modeled problem; it is not a standalone machine-checkable proof transcript.
