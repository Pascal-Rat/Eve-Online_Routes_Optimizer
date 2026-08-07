# Local web control deck

Iteration 5 is a deliberately local presentation layer over the same Python planner used by the
CLI. It exists to make the entire operating loop visible without weakening the proof contract.

## Start it

After installing the project:

```bash
eve-courier web
```

The command prints and opens:

```text
http://127.0.0.1:8765/
```

Useful launch options:

```bash
eve-courier web --no-browser
eve-courier web --port 9876
eve-courier web --workspace /path/to/session-data
```

The browser contains HTML/CSS/JavaScript, but the shipped application has no Node runtime. Python's
standard-library HTTP server serves those packaged assets and all API calls are same-origin.

## Operating flow

### 1. Scan

The default acquisition preset is **Match security bands**. It asks the local SDE which regions
contain at least one system in the currently enabled high/low/null bands and skips the others before
touching ESI; mixed-security regions are retained. On the current SDE that means 25 regions for
high-sec or 23 for low-sec instead of 114. **NPC Empire** selects SDE faction-owned high/low regions
and excludes player-sovereign and NPC nullsec; **Use all SDE regions** remains the widest preset.
You can instead search region names and add exact removable chips. There is no comma-separated
parsing in the browser workflow.

Select **Scan public contracts**; the Python service uses a bounded four-worker pool across
independent ESI regions, keeps pagination sequential inside each region, uses the persistent HTTP
cache, and writes `snapshot.json` before any optimization. If gate-threat awareness is enabled, it
also records the selected 1–168 hour zKillboard lookback (two hours by default) with the configured
gate radius. zKill requests remain rate-spaced and sequential.

The UI does **not** blindly query zKill for all contract regions. Before collecting threats it takes
the selected start, security bands, time budget, and seconds/jump and computes the exact BFS ball that
could be reached before the horizon *without* dynamic threat avoids. Only regions intersecting that
pre-threat ball are queried. This is proof-safe: applying the newly observed hard avoids can only
remove systems from reachability, never add a region outside the pre-scan envelope. Contract
discovery scope remains exactly what the user selected.

The header reports snapshot age and SDE identity. Snapshot age is derived in the browser from the
persisted `fetched_at` timestamp once per second, so it continues advancing after a scan finishes.
Scanning and solving remain distinct: changing solver inputs never changes the captured market
observation.

### 2. Inspect

Set the start system with autocomplete. Cargo remains an exact m³ input. Collateral accepts either a
plain number with a K/M/B unit selector or an inline suffix such as `750M`, `1.5B`, or `3k`; an
inline suffix wins over the selector so it is never multiplied twice. The session horizon uses
separate integer **hours** and **minutes** fields instead of decimal hours.

Security is a set of three independent toggles: high, low, and null. All seven non-empty
combinations are valid, and the UI prevents an empty selection. Manual system avoids use the same
search-and-chip interaction as regions. Then select **Rank opportunities**. The table is the
iteration-2 standalone baseline. It is useful for human inspection but does not constrain the exact
solver's ordering.

The high toggle says **0.5+ shown; raw >= 0.45** because routing reads the unrounded SDE value. CCP's
documented route-cost example puts raw values below `0.45` in its low-security branch and values at
or above `0.45` in the high branch. The UI exposes both forms so the implementation boundary is not
mistaken for a custom 0.45 gameplay rule.

The route-policy card controls three hard trip-shape rules. **Return to start** is enabled by
default. Turning it off permits a fully open route or an optional autocomplete-selected **Finish
system**. **Required route systems** is a multi-select autocomplete: every selected system must be
visited at least once, while CP-SAT decides the order jointly with pickup/delivery events. Cargo may
be exactly `0`, which intentionally leaves a route-only problem when a required/finish constraint
still defines travel.

**Simultaneous contracts** is independent of cargo volume. Blank means unlimited; an integer limits
how many courier parcels may be picked up but not yet delivered at the same time. `0` means no
courier parcel can be carried, but route-only planning remains valid.

The **Gate threat awareness** toggle is optional. It exposes independent suicide-gank, smartbomb,
heavy-interdictor, carrier, multi-pilot camp, hauler-loss, and any-gate-PvP categories. The minimum
event count means “forbid a system after this many distinct matching gate killmails in the selected
lookback.” Category overlaps on one killmail count once.

Only player-caused losses with an exact SDE gate ID or victim position inside the configured radius
are retained. Pure NPC/CONCORD losses and off-gate kills are discarded. The current start is exempt.
The scan status reports raw rows, retained gate events, coverage, and incomplete regions; 1,000-row
regions are incomplete due to the zKillboard response ceiling. See
[GATE_THREAT_MODEL.md](GATE_THREAT_MODEL.md) before interpreting this as more than observational
route filtering.

Advanced controls expose the deterministic travel assumptions, solver proof time, worker count and
optional candidate cap. Leaving the candidate cap blank is important when full eligible-set proof
scope is the goal. The web defaults are a 60-second full-route proof search with four CP-SAT
workers; presets extend the search through ten minutes. Dense cases may additionally spend up to
20 seconds in the master-guided decomposition phase; each endpoint-system master solve gets at most
ten seconds and stays single-worker. That proof work is included in the certificate wall time. A
multi-minute run can improve hard instances, but elapsed time never makes an incumbent optimal. The
proof badge changes only when a verified feasible reward meets a rigorous upper bound, either through
the master/exact equality or the complete exact fallback.

### 3. Optimize and prove

**Optimize & prove** runs the same proof pipeline described in
[MATHEMATICAL_MODEL.md](MATHEMATICAL_MODEL.md). The proof card makes six facts visible together:

- objective (incumbent reward);
- best solver upper bound;
- relative optimality gap;
- endpoint-system relaxation ceiling/status plus pair/clique strengthening on dense cases;
- whether heuristic candidate truncation changed the proof scope; and
- independent feasibility/reference-verification flags.

`PROVEN GLOBAL OPTIMAL*` in the UI requires both `certificate.status == "proven_optimal"` and
`certificate.scope_untruncated == true`. The asterisk is deliberate: the nearby model note states
that the theorem is conditional on the scanned regions/snapshot, declared endpoint/routing policy,
SDE graph, and deterministic timing model. See [OPTIMALITY.md](OPTIMALITY.md) for the complete scope.

The route manifest then shows every pickup/delivery event, exact system, jump count, cargo and
collateral after the action, and cumulative earned reward. Directly below it, **Gate-by-gate
itinerary** expands canonical `travel_legs`: pickup/delivery travel, required waypoint visits, and
the final return/fixed-finish travel. This itinerary therefore remains useful for a zero-contract
route-only solve. Every leg contains the actual ordered solar-system path and each system's SDE
security status. These are the paths independently verified by Python, not a browser reconstruction.
If threat categories were active, the banner explicitly notes that threat-blocked systems were
unavailable as **transit** as well as pickup/delivery endpoints.

### 4. Arm and execute

A solved route is not automatically a claim about what happened in EVE.

In `locked` collateral mode, selected optional contracts are accepted at modeled time zero. The UI
therefore requires the explicit checkbox confirming that every selected contract was actually
accepted in EVE before it creates execution state. Without that confirmation the server rejects the
transition as well, so bypassing browser validation does not weaken the rule.

In `rolling` mode, a proposed optional job does not become a commitment until its real pickup is
recorded.

Once armed, each route row exposes a pickup/delivery recording control. Python validates the state
transition and atomically replaces `execution.json`. Accepted jobs remain mandatory across future
replans. Delivered IDs remain in the session's completed set so a lagging public ESI page cannot
make the same contract selectable twice.

The original terminal system, pending required route systems, and simultaneous-contract limit also
enter execution state. Required waypoint and final itinerary legs expose **Mark reached** when they
are still pending. This matters for route-only use and prevents replanning from losing the user's own
trip objective after the current system changes.

### 5. Refresh and replan

**Refresh market & replan** captures a fresh observation for the same regions and solves forward
from the live execution state. Existing accepted shipments are mandatory. The revised plan is shown
for review before it is armed; locked mode again requires confirmation for newly selected public
jobs. During that review the existing accepted-contract action controls and Refresh/Replan remain
available. Newly proposed locked-mode rows say **Not armed** until the user confirms acceptance, so
an unavailable proposal cannot trap the session in a non-interactive review screen.

If gate-threat awareness was enabled when execution was armed, replanning captures the same
lookback/radius and re-derives its avoided-system set from the refreshed observation using the same
categories and minimum count. The current system, mandatory accepted-shipment endpoints, pending
required route systems, and the fixed terminal are exempt so a newly matching system cannot strand
an obligation that already exists. The new
observation time, coverage, incomplete regions, and avoid set become part of the revised proof input.
The threat-region envelope is recomputed from the current system and remaining session time.

Threat-aware planning also validates coverage when ranking or solving. If the current start,
security bands, or horizon can reach a region that the recorded zKill observation did not cover, the
server rejects the operation and asks for a fresh threat scan rather than silently treating that
unobserved transit region as safe.

### Execution lifecycle and infeasible replans

While execution state exists, the normal Scan/Rank/Optimize controls are disabled deliberately.
Those actions start a new mathematical problem and could otherwise discard accepted commitments;
**Refresh market & replan** is the safe operation inside a live trip.

Execution survives a browser refresh and a complete program restart. When `execution.json` is
restored, the page shows both a sticky top-bar **Live route** indicator and a persistent execution
banner before the planner grid. The banner reports the current system/commitment count, explains
the planning lock, and provides **Resume current route**. This is intentionally persistent UI, not a
toast that disappears while the controls remain disabled.

If wall-clock time has moved past the stored planning horizon, the banner calls that out explicitly.
Accepted commitments are still not deleted automatically: an expired optimization horizon does not
make an in-game courier obligation disappear.

`proven_infeasible` after a live replan is a statement about the remaining trip, not a broken UI. If
accepted shipments remain, CP-SAT proved that those mandatory commitments cannot all satisfy the
remaining time and route policy. Record real progress if the state has changed and replan again, or
reset only if you deliberately want the optimizer to stop preserving those commitments.

When `active_count == 0`, the UI instead shows **End execution & start new plan**. Ending is then safe
with respect to courier commitments and immediately unlocks Scan/Rank/Optimize. The persistent banner
also exposes **End execution** in this safe state. This provides a clear lifecycle for route-only
sessions and for a program that was closed after the last delivery but before execution was ended.

## Local persistence

By default the control deck stores three auditable JSON artifacts plus the ESI cache outside the
source tree:

| Platform | Default directory |
| --- | --- |
| Linux | `$XDG_DATA_HOME/eve-courier-route-optimizer` or `~/.local/share/eve-courier-route-optimizer` |
| macOS | `~/Library/Application Support/EveCourierRouteOptimizer` |
| Windows | `%LOCALAPPDATA%\EveCourierRouteOptimizer` |

The route card links to `plan.json`; the live panel links to `execution.json`. `snapshot.json` is
kept in the same workspace. The `--workspace` option can point at a separate throwaway or auditable
session directory.

A browser refresh reloads persisted artifacts. A saved plan also restores its recorded model inputs
into the planner controls so the visible cargo, collateral, security, route-shape and threat settings
match the plan being inspected. A plan loaded after a full server restart remains inspectable, but a
new execution commitment must come from a solve in the current server session; this prevents a stale
saved plan from silently arming itself. An already-armed `execution.json`, by contrast, is restored
as live state because forgetting accepted commitments would be unsafe.

## HTTP boundary and threat model

This is a personal localhost application, not a multi-user web service. `webapp.py` still applies a
small defense-in-depth boundary:

- the socket binds to IPv4 `127.0.0.1`, never `0.0.0.0`;
- `Host` must be `127.0.0.1` or `localhost`;
- a supplied browser `Origin` must also be local, reducing DNS-rebinding/cross-origin write risk;
- POST bodies must be JSON objects no larger than 1 MiB;
- static assets are selected from a fixed allowlist, not arbitrary filesystem paths;
- responses send `nosniff`, no-referrer, same-origin framing and a restrictive Content Security
  Policy; and
- API failures return JSON and never expose an interactive debug console.

The standard library server is intentionally single-process and sequential. A long scan or exact
solve makes the UI show a blocking working indicator with a live elapsed-time counter rather than
allowing overlapping mutations of the same session state. This is a correctness choice for a
one-user local tool, not a throughput architecture.

The public-data workflow requires no OAuth client ID, client secret, access token or EVE login.
Nothing in the web assets contains authentication material.

## Local API

The JavaScript control deck uses these same-origin endpoints:

| Method | Endpoint | Purpose |
| --- | --- | --- |
| `GET` | `/api/status` | SDE identity and persisted snapshot/plan/execution summary |
| `GET` | `/api/regions?q=...` | region-name suggestions from bundled SDE |
| `GET` | `/api/systems?q=...` | system-name suggestions from bundled SDE |
| `POST` | `/api/scan` | capture public couriers plus optional gate-threat observations for explicit, security-compatible, NPC-Empire, or all-region scope |
| `POST` | `/api/rank` | prepare and return up to 50 standalone scores |
| `POST` | `/api/solve` | prepare, solve, verify and persist an exact plan |
| `POST` | `/api/execution/start` | turn the current verified plan into execution state |
| `POST` | `/api/action` | record a real pickup, delivery, required waypoint, or finish-system visit |
| `POST` | `/api/replan` | optionally refresh ESI and solve around mandatory state |
| `POST` | `/api/execution/reset` | explicitly discard the persisted live session |
| `GET` | `/download/{snapshot,plan,execution}.json` | download an existing audit artifact |

The API is an internal UI boundary, not a stable remote-service contract. The versioned JSON files
remain the durable data contract described in [JSON_FORMATS.md](JSON_FORMATS.md).

## Proof-oriented UI rules

The interface deliberately avoids a single green “best route” label. It distinguishes:

- a verified feasible incumbent from a proved optimum;
- a zero proof gap from a still-open upper bound;
- solver optimality from full eligible-set scope; and
- mathematical model guarantees from live-market/travel assumptions.

Those distinctions mirror the Python certificate rather than being recomputed from route appearance.
If the exact solve reaches its time limit before closing the bound, the best feasible route remains
usable but the card says that the proof is open and displays the remaining gap.

## Troubleshooting

- **Port already in use:** launch with `eve-courier web --port 9876`.
- **A solve takes a long time:** proof time is intrinsically instance-dependent. A candidate cap can
  accelerate hard cases but the UI will mark the proof scope as truncated.
- **No eligible contracts:** inspect the snapshot count, then review capacity/collateral, security,
  endpoint policy, expiry and horizon assumptions. The ranking scope includes exclusion/reduction
  counts in its API response; plan JSON records the full scope.
- **A live contract vanished:** do not record a pickup that did not happen. Refresh and replan.
- **Replan says proven infeasible:** the execution session still works. Check remaining time and
  mandatory commitments, record any real progress, and replan; if no accepted commitments remain,
  end execution to unlock a fresh plan.
- **Scan/Rank/Optimize are disabled:** an execution session is active. Use Replan, or use **End
  execution & start new plan** when the live panel reports no accepted commitments.
- **Browser says backend offline:** open the URL printed by `eve-courier web`; the standalone UI
  preview is not an optimizer server.
