# How the solver finds and proves a route

The planner answers two questions together: **which contracts should I take, and in what order
should I pick them up and deliver them?** It aims for the greatest total courier reward within
your time, cargo, collateral and route restrictions. Reward means the contract payout before
expenses; it is not net profit or ISK per hour.

Finding a good route and proving it optimal are different jobs. A route shows what you can earn.
A proof establishes that no allowed route can earn more.

## A small example you can check yourself

Imagine three contracts from the same pickup station to the same delivery station:

| Contract | Cargo | Reward |
| --- | --- | --- |
| A | 6 m3 | 9M ISK |
| B | 5 m3 | 8M ISK |
| C | 5 m3 | 8M ISK |

Your hold fits 10 m3. You start at the pickup and must finish at the delivery. Travel takes ten
minutes, and you have exactly ten minutes available. Assume zero pickup/delivery service time,
enough collateral, no binding deadlines and no parcel-count restriction. There is time for one
outward trip, so everything you take must fit together.

A has the largest individual payout, but B and C together pay more:

```text
cargo(B + C)  = 5 + 5 = 10 m3
reward(B + C) = 8 + 8 = 16M ISK
```

Loading B and C, flying to the destination and delivering both establishes that **at least 16M
is achievable**. To prove optimality, divide every possible selection into two groups:

- Take A: neither B nor C fits alongside it, so the most you can earn is 9M.
- Skip A: only B and C remain, so the most you can earn is 16M.

Every selection belongs to one group. Neither group can beat 16M, and we have a route that earns
16M. That is a proof without listing every pickup and delivery order. This is an illustrative
problem, not a transcript of the solver's search.

## Finding the first useful route

The planner first builds the permitted stargate graph and computes shortest allowed paths between
relevant systems. Security rules and excluded systems apply to transit as well as endpoints.
Contracts that violate policy or provably cannot fit even on their own can be removed safely.
Accepted jobs remain obligations when replanning.

It then constructs routes by inserting pickup/delivery pairs and repairing the selection and
order. These quick attempts exploit shared travel, but do not establish optimality. Sorting by
individual payout or reward per jump cannot settle a problem where contracts interact.

An independent checker walks through each candidate improvement, recalculating travel, cargo,
collateral, deadlines, pickup-before-delivery order and the required finish. Only a route that
passes becomes the best known solution, called the **incumbent**. Its reward is a **lower bound**:
the best possible reward cannot be smaller than something we can already do.

## Establishing what might still be possible

An **upper bound** is a defensible reward ceiling. In our example, adding all three payouts gives
a crude ceiling of 25M. No selection can earn more, although collecting all three is impossible.
Accounting for cargo tightens that ceiling to 16M.

For larger problems, the application starts with a deliberately optimistic model: choose contracts
and visit their endpoint systems, while leaving out some detailed pickup order and resource rules.
Every real route has a corresponding possibility in this easier problem. The easier problem can
therefore overestimate what we can earn, but cannot exclude a better real route.

This optimism never permits splitting a parcel in a returned route. For example, three 40 m³
parcels in a 100 m³ hold need two outbound trips. A simple volume estimate can undercount that
travel, making the reward ceiling too generous. The selection model now also counts whole
crossings and necessary returns to tighten that estimate. The exact route search and independent
checker always handle each parcel as one indivisible pickup and delivery.

There are two distinct results here: an optimistic contract selection to investigate, and a
rigorous ceiling on all selections. **Merely finding a selection worth 25M does not prove that
25M is a ceiling.** The optimizer needs a proven optimum of the easier model or a bound its solver
has established during search.

Each promising selection is checked with an exact route search. A successful check supplies a
real route. A proven failure can teach the selection model that a particular combination of
contracts is impossible, preventing it from proposing that combination again. Running out of time
does not justify such a rule. Bounds and proven conflicts carry forward between attempts.

## Searching the complete problem

If the reward proof remains open, the complete pickup/delivery model takes over. It uses
Google OR-Tools CP-SAT, a solver for discrete choices and whole-number constraints. The application
describes the decisions explicitly:

- Whether to select each optional contract.
- Which pickup, delivery or required visit follows which other event.
- Arrival time, event order and resources along the route.

The constraints link these decisions: selecting a contract requires both actions; pickup must
precede delivery; travel and service consume time; cargo and collateral must remain within limits.
Already accepted commitments and the requested finish are mandatory. The objective adds the
rewards of selected and mandatory jobs.

CP-SAT explores tentative decisions and propagates their consequences. For example, choosing a
pickup may force a delivery before another parcel can fit. A contradiction rules out that branch;
learned conflicts let it avoid related dead ends. Bounds also let it discard whole groups of
possibilities that cannot improve the incumbent. It need not try every route individually.

The full model receives the existing verified route as guidance, plus valid bounds and learned
constraints. It retains the entire eligible contract pool, so it can change the selection. For
supported small selections and simple repeated haul lanes, specialized exact searches can settle
the same questions with less work. They are used only when their assumptions hold.

## When the proof is finished

Throughout the search, the two numbers have different jobs:

```text
best verified reward <= true maximum reward <= rigorous reward ceiling
remaining reward gap = rigorous reward ceiling - best verified reward
```

Better routes raise the left side. Stronger bounds lower the right side. When they meet, the
reward is proven optimal. In the example, both reach 16M ISK. If a real run instead stops with a
16M route and a 19M ceiling, it has a usable route and at most 3M of possible improvement. It has
not established whether that improvement exists. Proving the last small gap can take much longer
than finding the route.

The result reports this distinction directly:

| Status | What you know |
| --- | --- |
| `proven_optimal` | A verified route reaches the rigorous reward ceiling. |
| `feasible_not_proven` | A verified route exists; a better one has not been ruled out. |
| `proven_infeasible` | The mandatory trip requirements cannot all be satisfied. |
| `unknown` | Neither a usable route nor an infeasibility proof was established. |

Independent route replay checks feasibility, not the absence of a better route. The bounds and
exact searches supply that second argument; an additional exhaustive checker tests optimality for
supported small problems.

The proof applies to the recorded contracts and declared rules. An explicit candidate cap narrows
that scope; it cannot certify the excluded contracts. Live availability and actual flight times
can change. After proving reward, the planner can spend extra time shortening the trip, but a
reward proof alone does not promise the fastest route among all equally rewarding choices.

For the precise rules, read [domain behavior](DOMAIN.md). For the models, mathematical arguments
and search budgets, read [optimization and proof](OPTIMIZATION.md). The implementation starts at
[`RouteOptimizer.solve`](../src/eve_courier_optimizer/optimization/optimizer.py).
