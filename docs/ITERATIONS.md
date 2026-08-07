# Iterations and roadmap

## Iteration 1 -- acquisition and route substrate

Implemented in v1.

**Goal:** turn public EVE data into repeatable local optimization inputs.

- scan the public contract endpoint region by region;
- retain courier contracts only and de-duplicate moving page boundaries by contract ID;
- persist the observation before optimization;
- distill the official SDE into systems, security, NPC stations, regions, and stargates;
- compute exact shortest jump counts locally under arbitrary high/low/null band combinations,
  manual avoids, and the optional recorded gate-threat policy;
- collect/cache recent zKillboard observations politely and retain only player PvP localized to SDE
  gates; and
- honor ESI/zKill cache, spacing, and rate-limit signals.

The important architectural choice here is the snapshot boundary. Optimization never silently mixes
contract data fetched at different times during a long solve.

## Iteration 2 -- individual opportunity analysis

Implemented in v1.

**Goal:** establish feasibility and economics before route combinations complicate the picture.

For each candidate, v1 computes its direct start -> pickup -> delivery lower-bound trip and exposes:

- minimum solo jumps;
- deterministic solo time;
- gross ISK/hour;
- gross reward/jump;
- reward/collateral ratio.

The same pass performs endpoint/security policy exclusions and safe reductions. The ranking is useful
on its own and also provides the ordering used by the optional heuristic candidate cap.

## Iteration 3 -- exact pickup/dropoff optimization

Implemented in v1.

**Goal:** choose the profitable subset *and* its interleaved event order jointly.

The model supports carrying several courier packages simultaneously, delivering them in a different
order from pickup order, cargo capacity, locked or rolling collateral, expiry/deadline constraints,
an optional simultaneous picked-parcel cap, and a session horizon. Route shape is joint optimization
too: loops are the default, required systems are mandatory zero-service visits, and an open route may
have a fixed finish. Zero cargo is legal, allowing the same solver to act as a route finder. It
maximizes total reward rather than greedily chaining the best standalone ISK/hour scores.

The key addition requested for this project is proof: CP-SAT's optimal status, upper bound, gap,
problem fingerprint, explicit scope, independent route simulation, and a small exhaustive reference
solver are first-class result data rather than log text.

## Iteration 4 -- live replanning

Implemented in v1.

**Goal:** survive the fact that public contracts are a competitive live market.

Accepted shipments are moved into execution state. After an actual pickup/delivery and/or a refreshed
public scan, `replan` builds a new exact problem in which those accepted shipments remain mandatory.
It cannot “optimize away” a package already in the hold or a contract whose collateral is already
locked.

Execution state also retains the original terminal, remaining required route systems, and the
simultaneous-contract cap. Replanning from a later current system therefore cannot accidentally turn
an original Jita-to-Jita loop into a loop around wherever the pilot happens to be now.

This is intentionally event-driven, not an automated EVE client. The user records actions that really
happened and the optimizer recomputes from the resulting state.

## Iteration 5 -- minimalist localhost interface

Implemented in v1.

The interface remains deliberately thin over the same Python service/model boundaries:

- searchable region/system chip selectors including all-region scope; human K/M/B collateral and
  hours/minutes horizon controls; all security-band combinations; optional gate-threat categories,
  lookback, radius, and event threshold;
- loop/open routing, required-system multi-select, fixed-finish autocomplete, zero-cargo route-only
  use, and an optional simultaneous-contract limit;
- travel-time model, solver time limit and collateral mode;
- scan status plus snapshot age/SDE build;
- candidate table using iteration-2 scores;
- solve status with objective, bound, gap, verification flags and human proof claim;
- route table/timeline plus canonical gate itinerary including pickups/deliveries, required
  waypoints, and final return/destination travel;
- a visibly separate proof panel showing scope and `scope_untruncated`;
- buttons to record pickup/delivery, required route milestones, and refresh/replan;
- locked-mode acceptance confirmation before mathematical selections become execution commitments;
- persisted snapshot/plan/execution downloads; and
- loopback/Host/Origin/CSP hardening for the local HTTP boundary.

`webapp.py` calls `PlannerService` and the existing snapshot/report/state modules. JavaScript handles
presentation and user interaction only; resource, routing and execution validity are revalidated by
Python. The shipped runtime needs no Node server or front-end build step.

Authenticated location/waypoint convenience remains a possible later enhancement. Public contract
discovery and the v1 UI need no OAuth secret.

## Proof-preserving performance work (through v1.5)

Exact prize-collecting pickup-and-delivery routing is NP-hard and the arc model grows quadratically in
the number of event nodes. Large regional snapshots can therefore hit the solve time limit long before
they hit a correctness limit.

The current implementation includes:

- sparse direct-route lower bounds before building the retained all-pairs metric closure;
- safe reachability, horizon, expiry, capacity, collateral, and direct-deadline reductions;
- conservative earliest/latest-time arc pruning;
- tightened delivery-earliest/pickup-latest windows from exact pickup-precedence lower bounds;
- structural same-contract arc pruning and a redundant service-action cardinality inequality;
- a redundant global event-route elapsed-time equality for stronger linear propagation;
- resource-aware two-contract incompatibility projections and deterministic clique cuts;
- a deterministic endpoint-system relaxation that doubles as a rigorous reward ceiling and reusable
  selection master without deleting candidates;
- reduced exact routing of master-optimal selections, closing the proof immediately when verified
  exact reward meets the master ceiling;
- proof-safe higher-order no-goods from exact CP-SAT assumption cores, with deletion shrinking only
  after additional `INFEASIBLE` results;
- a bounded complete-event fallback that receives every valid ceiling and learned core when the
  decomposition cannot close;
- a constructive CP-SAT hint that never constrains the search;
- bounded policy-specific metric-closure and concrete-path caches;
- bounded parallel ESI acquisition plus SDE security-compatible contract-region filtering;
- proof-safe route-reachable zKill scoping; and
- frozen single-worker DST and blockade-runner scenarios that require untruncated optimality proofs.

See [CP_SAT_GUIDE.md](CP_SAT_GUIDE.md) and [BENCHMARKS.md](BENCHMARKS.md).

## Further proof-preserving optimization work

Future work should preserve the proof contract. Useful directions include stronger safe dominance,
stronger master relaxations or targeted valid inequalities demonstrated by frozen bottlenecks, and
richer calibrated fuel/opportunity-cost/risk objectives kept separate from the current hard gate
policy and gross-reward theorem.

Heuristic candidate pruning is useful for quick answers but is not a substitute for those methods
when a full eligible-set proof matters.
