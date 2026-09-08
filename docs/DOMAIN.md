# Domain rules

This guide explains what a plan promises and which assumptions it uses. Start here when choosing
limits or checking why a contract was excluded. For installation, use the [README](../README.md);
for an explanation of the search, read [how the solver works](HOW_THE_SOLVER_WORKS.md).

## Terms used in the planner

| Term | Meaning |
| --- | --- |
| Courier contract | An offer to pay for moving one indivisible parcel from its pickup station to its delivery station. |
| ISK / reward | EVE's currency / the contract payout before expenses. `M` means million and `B` means billion. |
| Collateral | Money committed when accepting a contract, unavailable for another contract until delivery releases it in the model. It is a budget constraint, not an expense deducted from reward. |
| NPC station | A station run by the game, rather than a player-owned structure. Both contract endpoints must resolve to supported NPC stations. |
| System / jump | A solar system / travel through a normal stargate to a neighboring system. Regions group systems. |
| Snapshot | A saved observation of public listings and any requested activity data, with its collection time and data versions. |
| ESI / SDE | EVE Swagger Interface, CCP's public data API / Static Data Export, the bundled source of systems, stations and stargate connections. |
| Horizon | The time available for the modeled trip. A contract's expiry and delivery deadline are separate limits. |
| Active shipment | A contract already accepted in EVE and recorded as an obligation, whether or not its parcel has been picked up. |
| Feasible / optimal | Satisfies all modeled rules / earns the greatest reward among routes satisfying those rules. |

## Contracts, resources and time

A public courier becomes routable only when both endpoints resolve to NPC stations in the pinned
SDE. Player structures, wormholes, Ansiblex networks, filaments and jump drives are outside the model.

Money is stored in 0.01 ISK units and cargo in 0.001 m³ units. Input conversion uses decimal
arithmetic; cargo demand rounds upward and capacity downward. The reward objective includes every
mandatory active shipment plus the selected optional contracts, each exactly once.

### Collateral modes

Choose the mode that matches when you intend to accept new contracts. With **locked collateral**,
you secure the whole selected set before leaving. With **rolling collateral**, the planner can
reuse the same funds after each delivery, but a future pickup depends on that listing still being
available when you arrive.

For example, with a 1B ISK budget and two contracts requiring 700M each, you cannot accept both at
departure. Rolling mode can allow both if you deliver the first before accepting the second and
the route still meets every other limit. Existing commitments consume budget in either mode.

| Rule | Locked collateral | Rolling collateral |
| --- | --- | --- |
| Optional acceptance | All selected jobs at departure | Each job on arrival for pickup |
| Collateral | Active plus all selected collateral must fit at departure | Active funds start locked; optional pickup locks funds and delivery releases them |
| Listing expiry | Must be unexpired at departure | Pickup arrival must be strictly before expiry |
| Optional delivery deadline | Days to complete after departure | Days to complete after pickup arrival |

### Deadlines, cargo and travel

Listing expiry is the last opportunity to accept a new contract; the delivery deadline is when an
accepted parcel must be delivered. Giving the planner more flying time does not extend either.

Delivery completion at the deadline is allowed. Already accepted shipments retain absolute
in-game deadlines across every replan. Integer solver windows round exclusive expiry down to the
last valid integer second and inclusive deadlines down to the last permitted second. Replay checks
actual datetimes independently, including fractional-second boundaries.

Each pickup/delivery consumes the configured service time. Travel uses shortest permitted gate
paths times seconds per jump. There is no discretionary waiting. Cargo and picked-but-undelivered
parcel counts must fit after each action; the optional parcel limit may be zero. An accepted but
unpicked shipment already locks collateral, but occupies no cargo or parcel slot until pickup.
Each courier parcel is indivisible: pickup loads its full volume and delivery unloads it in one
action. Splitting a parcel across separate trips is never a feasible plan.

Required systems belong to the optimized trip, and may be visited in any order. A loop includes the
return to its original start; a fixed finish includes final travel there. A fully open route may
end at its last event. These rules also apply when no courier is selected.

## Security and observations

Security bands describe EVE's classification of a system. They are route restrictions here, not
estimates of the chance of losing a ship. An allowed pickup and delivery are not enough: every
system along the connecting path must also be allowed.

The raw SDE security bands are high `>= 0.45`, low `> 0 and < 0.45`, and null `<= 0`.
Any nonempty band combination is allowed. Security restrictions and manual avoids apply to every
transit system as well as contract endpoints.

Contract scans use each region's original first-page count, sequentially within a region and with
a bounded pool across regions. A disappearing trailing page ends that region's scan. Contract IDs
are deduplicated, but page movement can still omit listings. The proof covers the recorded
observation, not a simultaneous view of the market. A failed required contract request fails the scan.

Security-compatible region scope retains mixed regions whenever any system matches. NPC Empire
additionally requires SDE faction ownership and a high/low system. These acquisition presets cannot
exclude a permitted pickup region under their declared policy. Threat coverage is separate from
contract acquisition scope.

## Gate-threat policy

Gate-threat filtering turns recorded combat reports into additional systems to avoid. A *killmail*
is a record of a ship loss; a *gate camp* here is a category inferred from those reports. A system
with no matching report is not established to be safe.

The optional zKill feed retains player-caused losses tied to a known gate. `zkb.npc=true` or no
attacker character ID excludes a row. Exact `zkb.locationID` in the correct system takes precedence;
otherwise the nearest gate must lie within the chosen radius of a finite victim position.
Default lookback is two hours and fallback radius is 250 km; neither implies a safety guarantee.

| Category | Observed rule |
| --- | --- |
| Suicide gank | zKill `ganked` label |
| Smartbomb | Player attacker's weapon group 72 |
| Heavy interdictor | Player attacker's ship group 894 |
| Carrier | Player attacker's ship group 547, 659 or 5120 |
| Gate camp | At least two attacker character IDs |
| Hauler loss | Victim group 28, 380, 513, 883, 902, 941 or 1202 |
| Any gate PvP | Every retained gate loss |

A system becomes forbidden when enough distinct killmails match any selected category. A killmail
matching several categories counts once. This changes feasibility, not objective coefficients.

The initial start is exempt from observed threat avoids. Replanning also exempts remaining required
systems, the terminal, mandatory deliveries and unpicked mandatory pickups. These exemptions do
not override manual avoids or security restrictions.

Coverage must include every region reachable within `horizon // seconds_per_jump` jumps under the
stable security/manual policy, with observed avoids removed. Jump time must be positive. Newly
derived avoids can only shrink that envelope. A failed
reachable region blocks threat-aware planning. The proof remains conditional on the observed
avoid set, even when every requested region responded successfully.

The collector follows the [zKillboard API's](https://zkillboard.com/api/docs/) newest-first,
200-row pages and deduplicates killmail IDs. Default limits are 10 pages per region, 100 page
requests per collection and a 300-second acquisition budget. A full final page, repeated page,
malformed response, request failure or exhausted budget marks the region incomplete; observations
already collected remain available. Threat-aware planning rejects incomplete reachable coverage.
Even a completed traversal describes the provider's observed feed: delayed reports, page movement
and unreported losses can still hide current danger.

The older ESI aggregate ship-kill threshold remains usable through the CLI. It does not classify
suicide ganks or locate gate combat. Missing aggregate data rejects that policy while allowing
ordinary planning.

## Execution and recovery

Execution is the application's record of your real trip. You accept contracts and move the ship
in EVE, then record those actions in the planner. A proposed route does not itself change the game.

Arming replays the proposed itinerary at actual departure. Locked mode requires confirmation of
real acceptance. Rolling proposals become commitments only when real pickup is recorded.
Accepted contracts are embedded in execution state, so disappearance from public ESI cannot erase
them. Delivered IDs prevent cached listings from being selected twice in the same session.

Replanning retains active shipments, original terminal, remaining required systems, resource limits
and session deadline. A refreshed replan proposes a new threat policy. In both collateral modes,
review it and choose **Apply revised plan to execution** before recording any newly proposed
pickup. Until then the accepted trip keeps its existing policy; existing commitments remain usable.
Applying a rolling proposal changes route policy without accepting its future optional jobs.
Extending the horizon
changes the session deadline only. Real progress can be recorded after that horizon,
subject to shipment deadlines and resources. An infeasible proposal does not discard obligations.

## Current beta limitations

The proof describes a frozen observation and the configured travel model. It cannot guarantee
live contract availability, docking access, travel time or safety in EVE.

- **Threat intel has bounded coverage.** Busy or unavailable regions can exhaust acquisition
  limits. The UI reports incomplete regions, and threat-aware solving requires complete coverage
  of the route's reachable region set. Successful collection still cannot establish live safety.
- **A restored plan is for inspection.** After restarting the server, solve or replan again before
  arming. Existing accepted commitments survive independently of the displayed proposal.
- **Conflicting work requires another review.** If another tab or application instance changes
  the workspace, stale mutations are rejected. Reload and review the current route before applying
  it; the application does not merge independent proposals automatically.
- **Recovery preserves obligations.** An SDE upgrade can be followed by a refreshed replan. If a
  required endpoint no longer exists in the new graph, resolve that incompatibility before applying
  another route. A corrupt authoritative workspace must be recovered from a valid backup.

Preserve accepted obligations when recovering from an error; resetting the tracker does not cancel
any contract in EVE.
