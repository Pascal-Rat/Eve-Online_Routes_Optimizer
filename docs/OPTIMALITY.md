# Optimality, bounds, and proof scope

“Optimal” is only useful if the quantifier is explicit. The optimizer therefore treats proof scope
as data.

## The precise claim

For an untruncated prepared problem, `proven_optimal` means:

> No route with greater total courier reward exists among the observed public courier contracts that
> remain after the declared endpoint/routing policy and mathematically safe reductions, subject to
> the recorded SDE stargate graph, cargo/collateral/parcel-count limits, loop/required-system/final
> route shape, horizon, deadlines, collateral mode, and deterministic travel-time model.

That is a global reward optimum **inside the declared model**, not a claim about every opportunity
that may exist on Tranquility.

The plan JSON records the ESI compatibility date, snapshot observation time, SDE build, scanned region
IDs, number of public couriers observed, every exclusion/reduction count, and the exact model
parameters needed to interpret this statement.

When gate-threat categories are enabled there is an additional precondition: every region reachable
from the start within the route's maximum jump/time envelope under the stable security/manual policy
must appear in the successful threat-observation coverage. `prepare_problem()` derives this envelope
independently of the scanner. Missing coverage is a validation error, not an implicit zero-danger
observation. This keeps acquisition pruning from weakening the declared threat-aware theorem.

## Scope layers

There are three intentionally different categories:

1. **Policy exclusions** define what v1 is willing to model. Player-structure endpoints, disallowed
   security bands, manual avoids, and endpoints excluded by the optional gate-threat policy
   are examples. Excluding them changes the problem statement.
2. **Safe reductions** remove contracts provably incapable of improving the optimum under that
   policy. Their proofs are summarized in
   [MATHEMATICAL_MODEL.md](MATHEMATICAL_MODEL.md#8-safe-preprocessing).
3. **Heuristic reductions** trade scope for speed. `--max-candidates` is currently the only one. No
   global eligible-set claim is allowed after using it.

`certificate.scope_untruncated` is true exactly when category 3 is empty. It says nothing about the
   choice of regions or routing policy; those remain part of the declared scope.

The CLI flag `--require-global-optimal` means “require an optimal, untruncated solve of this declared
scope.” It intentionally does not pretend that scanning The Forge proves an optimum over regions that
were never scanned.

## Solver statuses

The certificate records the mathematical conclusion, while `solver_status` records which proof path
produced it:

| Certificate | Meaning |
| --- | --- |
| `proven_optimal` | an independently verified feasible reward equals a rigorous upper bound; either full exact CP-SAT returned `OPTIMAL` or the v1.5 master/exact bounds matched |
| `feasible_not_proven` | a verified route exists, but the proof search stopped with a larger upper bound |
| `proven_infeasible` | full exact CP-SAT, or a proof-preserving relaxation/decomposition argument, proved that no route satisfies the mandatory model |
| `unknown` | no incumbent/proof was available when search stopped or the solver did not resolve status |

Google's authoritative status definitions are in the
[OR-Tools CP-SAT documentation](https://developers.google.com/optimization/cp/cp_solver).

For a maximization timeout with incumbent $L$ and solver upper bound $U$, v1 reports

$$
0 \le R^*-L \le U-L,
$$

plus:

$$
\text{absolute gap}=U-L,
\qquad
\text{relative gap}=\frac{U-L}{\max(1,|L|)}.
$$

The objective itself comes from CP-SAT's integer variable, not a floating-point rendering. CP-SAT's
Python bound API is floating-point; above the exact binary-float integer range v1 widens a nonoptimal
bound by one representable floating ULP before rounding upward. That can make a timeout bound slightly
looser, never deliberately tighter. For `OPTIMAL`, the exact integer objective is the exact bound.

### Composite master/exact proof

For dense problems, the auxiliary system model deliberately relaxes the courier problem to a route
over distinct endpoint systems and adds only independently derived necessary pair/clique conditions.
Every exact route maps to a feasible auxiliary route, so

$$
R^*_{exact}\le U_{system}.
$$

V1.5 can also use an `OPTIMAL` auxiliary solution as a master selection. It routes that exact selected
set in the real pickup/delivery model and independently simulates the extracted route against the
original full prepared problem. If the verified reward is exactly $U_{system}$, then

$$
R_{route}\le R^*_{exact}\le U_{system}=R_{route},
$$

which is a complete reward proof. The relaxation solution itself is never called a courier route;
the lower bound comes from a separately feasible exact route.

If the reduced exact model proves a master selection infeasible, a sufficient set $C$ of positive
selection assumptions may be fed back as

$$
\sum_{i\in C}x_i\le |C|-1.
$$

This is safe because optional-contract feasibility is downward-closed: removing other courier jobs
only removes service/resource load, and metric-closure shortcutting cannot increase travel. Core
shrinking removes a literal only after another exact `INFEASIBLE` proof. An `UNKNOWN` result never
creates a cut. If the bounded decomposition does not close, the complete exact model still runs with
all valid bounds and cuts accumulated so far.

Plan schema 3 records the relaxation status, ceiling, wall time, distinct endpoint-system count,
pair/clique counts, decomposition status/iterations, learned-core count, exact-subproblem time and
whether the composite proof closed under `certificate.bound_strengthening`. A timed-out auxiliary
`FEASIBLE` solve contributes only its rigorous CP-SAT best objective bound, never its relaxation
incumbent. It is not used as a master selection. An auxiliary `UNKNOWN` result without a usable
ceiling is ignored. Full derivations are in
[MATHEMATICAL_MODEL.md](MATHEMATICAL_MODEL.md#9-proof-strengthening-upper-bounds).

## Verification chain

A successful proof result passes several distinct checks:

1. `CpModel.validate()` rejects structurally or numerically invalid models before search.
2. On dense cases, the system relaxation and pair/clique derivations supply only mathematically
   necessary upper-bound strengthening; random small instances are regression-checked against the
   independent exhaustive optimum.
3. Either full exact CP-SAT closes its own objective bound, or an optimal system master and reduced
   exact model produce matching upper/lower rewards. Proven reduced-model assumption cores are fed
   back only as necessary coexistence cuts.
4. Route extraction follows only selected circuit arcs from start to dummy end.
5. An independent simulator rebuilds shortest stargate paths and checks time, order, cargo,
   simultaneous parcel count, collateral, expiry, deadlines, required systems, terminal travel,
   horizon, mandatory commitments, final resources, and exact reward.
6. For locked mode with at most ten optional contracts, no live commitments, and no required
   waypoint systems, an independent exhaustive dynamic program must return the same optimum. It
   supports loop/fixed-finish terminal travel and the simultaneous-contract cap.

Step 5 is also the feasible lower-bound witness in a decomposition proof; on every path it remains an
independent defense against extraction/model-linking mistakes. Step 6 is an additional small-case
cross-check, not a replacement for either CP-SAT proof path.

### Why the reference solver is exhaustive

For the supported small locked-mode case its state is

$$
(\text{current system},\ \text{ever-picked mask},\ \text{delivered mask}).
$$

Cargo, locked collateral, selected reward, and remaining actions are determined by those masks. For
the same state, an earlier arrival dominates a later arrival because all remaining temporal
constraints are upper deadlines. Keeping only the earliest arrival per state therefore removes only
dominated paths. Enumerating every feasible transition over all masks exhausts every relevant subset
and event order.

The automated suite also generates deterministic random five-contract instances and compares this
oracle with CP-SAT.

## Problem fingerprint

`problem_sha256` hashes a canonical JSON representation of the mathematical input, including:

- resource, horizon, time, security, collateral-mode, loop/final/required-system, and
  simultaneous-contract parameters;
- all eligible contract IDs, endpoints/systems, exact units, expiry and delivery windows;
- all mandatory shipment coordinates, units, status and deadlines;
- snapshot/SDE/scope provenance and reduction counts; and
- the exact threat categories, count/radius/window, coverage/incomplete regions and derived avoids;
- the exact relevant-system jump matrix.

The fingerprint is an identity/checksum, not a cryptographic proof transcript. It lets two artifacts
demonstrate that they refer to the same modeled problem; the actual optimality proof remains CP-SAT's
search result/status.

## What v1 cannot prove about the live game

The following facts are outside the optimization theorem:

- **ESI pagination is not transactional.** A regional scan spans several live pages; jobs can appear,
  disappear, or move between pages during it. IDs are de-duplicated, but no API-side atomic snapshot
  exists here. The proof quantifies over the bounded observation captured in the file.
- **Availability can change after scanning.** Another player may accept a public contract before you.
  Locked mode reduces this risk by assuming selected contracts are accepted immediately; rolling mode
  explicitly accepts the availability risk and should replan.
- **NPC stations only.** Player structures are excluded because access, docking and location
  semantics need more data and can change independently of the SDE.
- **Static normal stargates only.** V1 does not model wormholes, Ansiblex jump gates, filaments,
  shipcasters, jump drives, dynamic topology, or other travel mechanics.
- **Travel time is a calibrated deterministic approximation.** “60 seconds/jump + 30 seconds/action”
  is not a theorem about your ship, client, session changes, warp geometry, docking, or server load.
- **Security bands are routing policy, not safety probabilities.** High/low/null determine which
  static systems may be traversed; they do not imply a loss probability.
- **Gate-threat awareness is an observational policy, not a safety proof.** zKillboard rows are
  retained only for player-caused losses tied to an SDE gate, then classified by auditable labels,
  type groups, attacker count, and victim type. The selected event threshold deterministically
  removes matching systems from the routing graph (with documented current/mandatory-endpoint
  exemptions). The proof is exact for that filtered graph, but does not prove observation
  completeness, current camp presence, future safety, or statistical optimality for cargo exposure.
  A failed route-reachable region now blocks the solve entirely. A saturated 1,000-row region is
  still marked incomplete; the mathematical proof remains about the derived observed policy, and
  that empirical incompleteness stays visible in the certificate.
  See [GATE_THREAT_MODEL.md](GATE_THREAT_MODEL.md).
- **Gross reward is not profit/risk-adjusted utility.** Fuel, losses, taxes, opportunity cost and the
  time-value/risk of collateral are not subtracted.

These are not reasons to weaken the mathematical proof; they are reasons to keep the theorem narrow
and visible.

## Operational proof checklist

For the strongest v1 claim:

1. refresh the bundled SDE if it is stale;
2. scan every region you truly want in scope immediately before planning;
3. do not use `--max-candidates`;
4. calibrate the time model conservatively for your actual hauling setup;
5. use explicit security/avoid-system policy;
6. if threat filtering is enabled, inspect category/radius/window and complete region coverage;
7. give CP-SAT enough time to return `proven_optimal`;
8. require `scope_untruncated=true` and `feasibility_verified=true` in the JSON;
9. in locked mode, actually accept every selected contract at plan time;
10. re-scan/replan when reality changes.

`--require-global-optimal` makes step 6/7 machine-checkable as a CLI exit condition for the declared
scope.
