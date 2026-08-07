# Mathematical model

This document specifies the optimization problem implemented by `solver.py`. The implementation uses
OR-Tools CP-SAT, whose variables and constraints are integer-valued. Google documents `OPTIMAL` as an
optimal feasible solution and `INFEASIBLE` as a proof of infeasibility in the
[CP-SAT status reference](https://developers.google.com/optimization/cp/cp_solver).

## 1. Inputs

Let $K$ be the optional courier contracts surviving policy exclusions and safe preprocessing. For
each $i$ in $K$:

- $p_i$, $d_i$: pickup and delivery event;
- $s^p_i$, $s^d_i$: pickup/delivery solar systems;
- $v_i \ge 0$: cargo demand in 0.001 m³ integer units;
- $c_i \ge 0$: collateral in centi-ISK;
- $r_i > 0$: reward in centi-ISK;
- $E_i$: public listing expiry;
- $D_i$: delivery window (`days_to_complete * 86400`) in seconds.

There can also be a set $A$ of already accepted shipments. Their events are mandatory. A shipment
already in cargo has only a delivery event; an accepted-but-unpicked shipment has both events. Every
active shipment carries its absolute modeled delivery deadline.

Global resources/parameters are:

- $s_0$: starting solar system;
- $C$: cargo capacity;
- $B$: collateral budget;
- $M$: optional maximum number of picked-but-undelivered courier contracts;
- $H$: session horizon in seconds;
- $\gamma$: modeled seconds per stargate jump;
- $\sigma$: service seconds for every pickup/delivery;
- $T_0$: snapshot/planning time.

Route-shape inputs are a set $W$ of solar systems that must be visited at least once and a terminal
policy. The terminal is either the start $s_0$ for a loop, a fixed user-selected system $s_f$ for a
fixed-finish open route, or unconstrained for a fully open route. A loop is the default. Cargo
capacity $C=0$ is valid, so a problem with no eligible courier contracts can still optimize a pure
route through $W$ and/or to $s_f$.

For systems $a,b$, $J(a,b)$ is the exact minimum stargate jump count under the declared system
policy. Each SDE system belongs to exactly one band:

$$
\operatorname{band}(s)=
\begin{cases}
\text{high}, & securityStatus(s)\ge0.45,\\
\text{low}, & 0<securityStatus(s)<0.45,\\
\text{null}, & securityStatus(s)\le0.
\end{cases}
$$

The user selects any non-empty subset of `{high, low, null}`. A permitted route may use only those
bands, including its transit systems. Explicit manual avoids are removed as well. If $b$ is
unreachable from $a$ in that filtered graph, the transition does not exist. Travel seconds are

$$
\tau(a,b) = \gamma J(a,b).
$$

The model uses this metric closure; it does not optimize individual gate-edge variables.

### Optional gate-threat policy

A recorded zKillboard observation supplies the set $E_s$ of distinct player-caused killmails tied to
an SDE stargate in system $s$. Every event carries one or more deterministic categories. Let $C_T$
be the user-selected category set, $k$ the minimum event count, and $X$ the exempt systems. The hard
avoid set is:

$$
F_T=\left\{s\notin X:
\left|\left\{e\in E_s:categories(e)\cap C_T\ne\varnothing\right\}\right|\ge k
\right\}.
$$

In plain language: a non-exempt system is forbidden when at least $k$ distinct observed gate losses
match one or more selected categories. One killmail counts once even if it matches several selected
categories.

Systems in $F_T$ are forbidden as endpoints and transit systems. The declared start system is exempt
so the pilot can depart. During live replanning, the current system, every still-required endpoint
of an accepted shipment, pending required route systems, and the fixed terminal are exempt: a
refreshed safety preference must not erase a route obligation already committed to execution state.
Other matching transit systems remain forbidden.

The plan records $C_T$, $k$, the observation fetch time/window/radius, coverage and incomplete region
sets, and the exact derived forbidden-system IDs. Those values enter the canonical problem
fingerprint and define the theorem's route universe. CP-SAT can therefore prove a genuine
maximum-reward optimum over the allowed graph. The proof does not claim that the threat observation
is complete or predictive, or that a permitted route is safe. Exact localization and category rules
are in [GATE_THREAT_MODEL.md](GATE_THREAT_MODEL.md).

The legacy ESI aggregate ship-kill threshold uses the same hard-filter shape for old snapshots and
CLI compatibility, but it cannot localize or classify a kill and is not the current UI threat model.

## 2. Event path and contract selection

Add a start node $0$, dummy end node $e$, and one mandatory zero-service node for every required
system in $W$ that is not already guaranteed by the start or terminal. For every optional contract
define

$$
x_i \in \{0,1\},
$$

where $x_i=1$ means both events are selected.

For each feasible transition $u \rightarrow v$, define a binary arc $y_{uv}$. CP-SAT's circuit
constraint supplies exactly one incoming and outgoing arc for each active node. An optional event has
a self-loop with value $1-x_i$, so both $p_i$ and $d_i$ are skipped precisely when the contract
is not selected.

The arc $e \rightarrow 0$ is fixed to one; all other arcs into $0$ and out of $e$ are omitted.
Consequently, the non-self-loop part of the circuit is exactly one path

$$
0 \rightarrow \text{chosen/mandatory events} \rightarrow e.
$$

The end node encodes route shape. For a loop, $s_e=s_0$. For a fixed-finish route, $s_e=s_f$.
Travel into $e$ then has its normal metric-closure cost. For a fully open route $s_e$ is absent and
travel into $e$ costs zero, so the trip may end at its last action or waypoint. Required-system
nodes are part of the same circuit, which lets CP-SAT choose their best order relative to courier
events instead of adding them after optimization.

An integer order variable $o_v$ is propagated by one along every used route arc, with $o_0=0$.
For every selected optional contract,

$$
o_{d_i} \ge o_{p_i}+1.
$$

Accepted-but-unpicked mandatory shipments have the same precedence constraint without a selection
literal.

## 3. Time

Let $t_v \in [0,H]$ be arrival seconds at event $v$, with $t_0=0$. For every used arc
$u \rightarrow v$, except the artificial $e \rightarrow 0$ wrap,

$$
t_v = t_u + \mathbf{1}[u\text{ is an action}]\sigma +
      \tau(s_u,s_v).
$$

Equality is important. A `>=` constraint would let a solver invent arbitrary waiting slack and could
corrupt rolling relative deadlines. Waiting is not a decision variable in v1; the deterministic
model assumes immediate onward movement after each service.

Waypoint nodes have zero service. A constrained end includes its final travel, so

$$
t_e = t_{last}+\mathbf{1}[last\text{ is an action}]\sigma+\tau(s_{last},s_e) \le H.
$$

For a fully open route, the final travel term is zero. The same horizon therefore covers the
complete declared trip, including the return home on a loop or travel to a fixed finish.

The implementation also writes the telescoped path duration as one redundant linear equality. Let
$Y$ be the non-self-loop path arcs excluding the artificial $e\rightarrow0$ wrap, and let
$|A_{events}|$ be the mandatory pickup/delivery action count. Then

$$
t_e = \sum_{(u,v)\in Y}\tau(s_u,s_v)y_{uv}
      + \sigma\left(|A_{events}|+2\sum_{i\in K}x_i\right).
$$

Every integral event path already implies this equality through the arc-by-arc arrival equations,
so it changes no feasible route. Its purpose is proof speed: the linear relaxation sees in one row
that collecting additional reward must pay both service time and the selected travel arcs.

### Locked-mode deadlines

In `locked` mode all selected public contracts are assumed accepted at $T_0$. Thus

$$
t_{d_i}+\sigma \le D_i.
$$

### Rolling-mode listing and deadlines

In `rolling` mode acceptance occurs at pickup arrival. It must be strictly earlier than the listing
expiry. Since $t_{p_i}$ is integral seconds, let

$$
L_i = \lceil (E_i-T_0)_{seconds} \rceil - 1.
$$

Then

$$
t_{p_i} \le L_i
$$

and the delivery window starts at that acceptance event:

$$
t_{d_i}+\sigma \le t_{p_i}+D_i.
$$

For an already accepted shipment $a$, with absolute deadline $Q_a$, the model instead requires

$$
t_{d_a}+\sigma \le (Q_a-T_0)_{seconds}.
$$

## 4. Cargo

Let $q_v \in [0,C]$ be cargo immediately after applying the load change at event $v$. If accepted
shipment $a$ is already in cargo at the start,

$$
q_0 = \sum_{a \in A:\ picked_a} v_a.
$$

Event changes are

$$
\Delta q_{p_i}=+v_i, \qquad \Delta q_{d_i}=-v_i.
$$

Along each used arc $u \rightarrow v$:

$$
q_v=q_u+\Delta q_v.
$$

The domain $0 \le q_v \le C$ enforces capacity after every event, and $q_e=0$ requires every
carried package to be delivered before the route ends.

## 5. Simultaneous courier contracts

When the user supplies $M$, let $m_v \in [0,M]$ be the number of courier contracts physically in
the trunk immediately after event $v$. Accepted-but-unpicked contracts do not count because there
is no parcel aboard yet. Initially,

$$
m_0 = |\{a\in A : picked_a\}|.
$$

Pickup and delivery events change the count by one:

$$
\Delta m_{p_i}=+1, \qquad \Delta m_{d_i}=-1,
$$

and every used route arc enforces

$$
m_v=m_u+\Delta m_v.
$$

The variable domain is the hard simultaneous-parcel constraint, independent of cargo volume.
$m_e=0$ follows from mandatory delivery of every selected/carried contract. When the option is
blank, this resource is omitted completely. Setting $M=0$ safely removes every optional courier
contract and leaves only route-shape work, if any.

## 6. Collateral

Two semantics are useful because the operational strategies are genuinely different.

### Locked mode

All selected optional contracts are accepted immediately. Let

$$
B_0 = \sum_{a \in A} c_a
$$

be collateral already committed by live shipments. Feasibility requires

$$
B_0 + \sum_{i \in K} c_i x_i \le B.
$$

No dynamic collateral variable is necessary: the left side is the maximum locked amount, occurring
at time zero, and deliveries only release collateral afterward.

### Rolling mode

Let $b_v \in [0,B]$ be collateral locked after event $v$, with $b_0=B_0$. Optional pickup
acceptance locks $c_i$; every delivery releases it:

$$
\Delta b_{p_i}=+c_i, \qquad \Delta b_{d_i}=-c_i.
$$

For an already accepted-but-unpicked shipment, pickup has collateral change zero because its
collateral was paid before replanning. As with cargo,

$$
b_v=b_u+\Delta b_v
$$

on every used route arc and $b_e=0$.

Rolling mode can select contracts whose collateral totals more than $B$, provided deliveries free
enough collateral before later acceptances.

## 7. Objective and lexicographic tie-break

All active shipments are mandatory, so their reward is a constant. The primary objective is

$$
\max R = \sum_{a \in A}r_a + \sum_{i \in K}r_i x_i.
$$

This is gross courier reward: no value is assigned to travel time, probabilistic risk, fuel,
opportunity cost, or collateral carrying cost in the primary objective. When gate-threat awareness
is enabled, it changes the feasible routing graph as the explicit hard policy above; it does not
change reward coefficients.

If and only if the reward proof first establishes $R^*$, either through the complete exact model or
the master/exact equality described in section 9, the optimizer fixes

$$
R=R^*
$$

and performs a bounded exact secondary solve minimizing $t_e$. On the decomposition path the
master-optimal contract set is fixed as well. This makes “maximize reward, then prefer the faster
equally rewarding route” a true lexicographic objective. The reward proof survives even if the
secondary time minimization stops early.

## 8. Safe preprocessing

The default preprocessing never deletes a positive-reward contract merely because it looks
unprofitable. It removes only cases whose deletion cannot lower the model optimum:

| Reduction | Why it preserves the reward optimum |
| --- | --- |
| completed contract still visible during the same execution session | it cannot legally be accepted/delivered for reward a second time |
| already-active contract still visible publicly | its mandatory shipment copy already represents it exactly once |
| listing already expired | no legal future acceptance exists |
| zero reward | selecting it cannot raise reward and only adds nonnegative time/resources |
| individual volume > $C$ | its pickup cannot satisfy cargo capacity |
| individual collateral > $B$ | no acceptance can fund it |
| $M=0$ | no courier pickup can satisfy the simultaneous-parcel domain |
| locked: $B_0+c_i>B$ | it cannot be among contracts accepted at time zero |
| unreachable pickup/delivery | no permitted gate route can execute its event pair |
| solo shortest trip > $H$ | any route containing it has at least that travel/service lower bound |
| locked solo completion > $D_i$ | even the earliest direct completion misses its deadline |
| rolling earliest pickup >= expiry | even the fastest arrival cannot accept it in time |
| rolling minimum pickup-to-delivery > $D_i$ | even direct post-acceptance service/travel is late |

NPC-station-only endpoints, disallowed security bands, manual system avoids, and gate-threat endpoint
avoids are **policy exclusions**, not mathematical reductions. They change the declared problem
universe and are reported separately. A contract can also become safely `unreachable` when its
endpoints themselves are permitted but every connecting gate path intersects a forbidden system.

The optional `max_candidates` ranking/cap is neither policy nor a safe reduction. It can remove the
true optimum and is always recorded as a heuristic reduction.

## 9. Proof-strengthening upper bounds

Dense instances receive additional necessary conditions before the exact solve. They are redundant
with the true courier problem, so they can lower an upper bound without lowering the true optimum.

### Pair projection and clique cuts

For two optional contracts $i,j$, there are exactly six event orders respecting both pickup-before-
delivery precedences. The preprocessor evaluates all six with exact metric-closure travel. It keeps
only projected orders that satisfy the two contracts' cargo use, optional simultaneous-parcel cap,
and rolling collateral budget. This projection deliberately starts with no active cargo/parcels or
rolling collateral and omits every other contract, mandatory action and required waypoint, making
it optimistic relative to a real route. Locked collateral is checked directly against $B_0$.

If even this optimistic two-contract problem cannot finish within $H$, or the two contracts cannot
fit locked collateral, no exact route can select both. The model may therefore add

$$
x_i+x_j\le1.
$$

Mutually pair-incompatible contracts are combined into deterministic clique cuts. For a clique
$Q$,

$$
\sum_{i\in Q}x_i\le1.
$$

The implementation uses a deterministic greedy clique finder rather than an exponential
maximum-clique algorithm. Missing a clique only misses a strengthening opportunity; it cannot
invalidate a proof.

### Endpoint-system relaxation

The auxiliary upper-bound model replaces the two event nodes per contract with one visit variable
$z_s$ for each distinct endpoint/mandatory solar system. Selection implies visitation:

$$
x_i\le z_{s^p_i},\qquad x_i\le z_{s^d_i}.
$$

It chooses one metric-closure path through the visited systems, retains total service time, the
session horizon, loop/fixed terminal, mandatory endpoint systems, locked collateral and the valid
pair/clique cuts above. It intentionally drops global pickup/delivery order, global cargo state,
global simultaneous-parcel state, rolling collateral state and individual contract deadlines.

Every exact courier route maps to a feasible relaxation route: erase the dropped resource/order
requirements, collapse repeated endpoint-system visits, and shortcut between retained systems with
the metric closure. Shortest-path triangle inequality guarantees that shortcutting cannot increase
travel time. Therefore

$$
R^*_{exact}\le R^*_{system}.
$$

If the auxiliary CP-SAT solve proves $R^*_{system}=U_{system}$, the exact model safely receives
$R\le U_{system}$. If the auxiliary solve times out with a feasible solution, only CP-SAT's rigorous
best objective bound is usable as $U_{system}$; its feasible relaxation objective is **not** an upper
bound. If no valid bound is available, the extra ceiling is simply omitted.

### Logic-based master/exact decomposition

For at least 20 optional contracts, v1.5 can use the endpoint-system relaxation as a master rather
than only as a passive ceiling. Let an `OPTIMAL` master solution select the set $S\subseteq K$ with
reward $U_M=R^*_{system}$. The program builds a reduced copy of the exact event model containing
only optional contracts in $S$, while preserving active shipments, required systems, terminal shape,
all exact time/resource rules and the same metric closure. Positive CP-SAT assumptions force every
$i\in S$ selected.

If that reduced exact model finds a route $q$, the independent full-problem simulator must accept it.
Its reward is checked against the integer master objective. Then

$$
R(q)\le R^*_{exact}\le R^*_{system}=U_M=R(q),
$$

so every quantity is equal and the reward optimum is globally proven without solving the complete
$2|K|$-event model.

If the exact subproblem instead returns `INFEASIBLE`, CP-SAT can return a sufficient subset $C$ of
the positive assumptions that caused infeasibility. The crucial monotonicity fact is that optional
courier feasibility is downward-closed. From any feasible full route containing $C$, delete every
optional pickup/delivery pair not in $C$. Cargo, locked/rolling collateral, parcel count and service
time can only decrease. Mandatory events remain. Shortcut each deleted detour through the same
metric closure; triangle inequality guarantees travel cannot increase. Deadlines are upper bounds,
so arriving no later cannot invalidate them. The resulting route would make $C$ feasible, which
contradicts the exact infeasibility proof. Therefore the master may safely learn

$$
\sum_{i\in C}x_i\le |C|-1.
$$

CP-SAT cores are sufficient but need not be minimal. V1.5 tries deletion-based shrinking while the
subproblem budget remains. A contract leaves the core only after the smaller assumption set is
again proven `INFEASIBLE`; `FEASIBLE` or `UNKNOWN` shrink attempts keep the literal. An initial
`UNKNOWN` exact subproblem creates no cut at all.

The master is then re-solved with the learned no-good. This logic-based Benders-style loop is bounded
by wall time and iteration count. Each single-worker master solve gets at most ten seconds by
default, reduced exact/core work gets at most two seconds per iteration, and the dense decomposition
phase has a 20-second default envelope. If it does not close, all rigorously valid pair, clique,
master-bound and learned-core information is attached to the complete exact CP-SAT fallback. These
are performance budgets, not proof-scope truncations.

## 10. Complexity

This is a prize-collecting pickup-and-delivery routing problem with time/resource constraints and is
NP-hard. With $n$ optional contracts there are roughly $2n$ optional event nodes and the metric
closure allows $O(n^2)$ route arcs. Exact proof time can therefore rise sharply with the eligible
contract count.

Preparation avoids building the full closure over contracts that fail safe direct lower bounds.
After retention, impossible event arcs are removed using conservative earliest-arrival/latest-window
bounds. For an unpicked contract, the delivery lower bound explicitly includes the exact
start-to-pickup distance, pickup service, and pickup-to-delivery distance; the pickup upper bound
leaves enough time for its own direct delivery. Same-contract arcs that directly contradict
precedence are omitted. The redundant inequality

$$
\sigma\left(|A_{events}|+2\sum_{i\in K}x_i\right)\le H
$$

where $|A_{events}|$ is the number of mandatory pickup/delivery actions already in execution state,
also exposes the unavoidable action-service time directly to CP-SAT propagation. A greedy feasible
route is supplied as a nonbinding CP-SAT hint, and jump closures/concrete paths are cached by exact
routing policy. None of these changes deletes a feasible contract combination, so they preserve the
proof. The explicit `max_candidates` cap remains the only performance option that truncates scope.
The pair/clique cuts, endpoint-system master and exact-core feedback described above add stronger
problem-specific proof structure on dense cases. See [CP_SAT_GUIDE.md](CP_SAT_GUIDE.md) and
[BENCHMARKS.md](BENCHMARKS.md).

A time limit changes the **proof status**, not the mathematical problem. If CP-SAT has an incumbent
but has not closed its upper bound, the optimizer returns `feasible_not_proven` plus that bound/gap. It never
renames a timed-out incumbent “optimal.”
