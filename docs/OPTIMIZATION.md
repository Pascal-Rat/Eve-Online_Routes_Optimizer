# Optimization and proof

The objective is maximum gross courier reward over the declared snapshot and route policy.
A feasible route supplies a lower bound. A sound relaxation or completed exact search supplies
an upper bound. Equality proves the reward optimum; a timeout alone proves nothing.

## Complete event model

`optimization/models/pickup_delivery.py` exposes the mathematical structure directly: event catalog, circuit,
arrival/order/resource variables, contract constraints, reward objective, hints and extraction.

For optional contract `i`, Boolean `x_i` selects both pickup and delivery. Active shipments have
mandatory delivery events and, if unpicked, mandatory pickup events. Required systems have mandatory
zero-service waypoints. Start and end are distinct events even for a loop.

A circuit over these events uses a fixed artificial end-to-start arc. Optional events use
self-loops when their contract is skipped. The remaining arcs form one route from start to end.
Infeasible transitions are omitted using optimistic earliest-arrival/latest-departure windows.
The end represents the original start, a fixed destination, or a free finish with zero final travel.

For a used real arc from `u` to `v`:

$$
t_v = t_u + service(u) + jumpSeconds \times J(system(u), system(v)),
\qquad order_v = order_u + 1.
$$

`J` is the exact shortest-path metric closure of the permitted graph. Equality prevents invented
waiting from changing relative delivery windows. Pickup precedes delivery by event order, including
when travel and service are zero. Start arrival is zero; finish arrival cannot exceed the horizon.

Cargo, parcels and rolling collateral propagate by each destination event's resource change.
Their variable domains enforce capacity after every action. Active cargo/parcels initialize the
start; all active collateral starts locked. Accepted-but-unpicked pickups do not lock funds twice.
Finish has no undelivered parcels or cargo. Locked mode instead constrains total selected plus active
collateral at departure. [Domain rules](DOMAIN.md) specify expiry and deadline semantics.

For mandatory reward `R_A` and optional rewards `r_i`, the objective is:

$$
\max R = R_A + \sum_i r_i x_i.
$$

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
3. If the master remains open, the complete event model receives its bounds, cuts and best
   incumbent, retaining the entire eligible optional pool. If reward is still unproven afterward,
   route diversification seeds distinct haul lanes and repairs two-job removals. Independent replay
   can improve the final reward without changing the bounded CP-SAT search trajectory.
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

If a selected set is proven infeasible, every superset is infeasible: removing optional jobs only
reduces service/resources, and shortcutting cannot lengthen travel. The event oracle uses single-worker
assumption search and optionally shrinks sufficient infeasibility cores. Only proven deletions
strengthen a core; an `UNKNOWN` result never justifies a cut. Completed subset searches can also
supply local reward ceilings and sufficient infeasible sets.

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

`SolverConfig` makes budgets explicit. Defaults allow 20 seconds of decomposition, at most eight
iterations, up to 10 seconds per single-worker master solve, and up to two seconds per oracle.
The complete-event time limit is additional; construction and model building are additional too.
Route diversification has a one-second soft budget checked between insertion passes; cases with a closed
reward proof skip it.
Secondary duration search has its own budget. Certificate wall time sums search phases; benchmark
elapsed time measures the complete operation. Multiworker CP-SAT trajectories are nondeterministic.

The problem fingerprint includes contracts, constraints, obligations, provenance and the jump
matrix. It identifies the modeled problem; it is not a standalone machine-checkable proof transcript.
