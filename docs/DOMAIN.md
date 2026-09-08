# Domain rules

## Contracts, resources and time

A public courier becomes routable only when both endpoints resolve to NPC stations in the pinned
SDE. Player structures, wormholes, Ansiblex networks, filaments and jump drives are outside the model.

Money is stored in 0.01 ISK units and cargo in 0.001 m³ units. Input conversion uses decimal
arithmetic; cargo demand rounds upward and capacity downward. The reward objective includes every
mandatory active shipment plus the selected optional contracts, each exactly once.

| Rule | Locked collateral | Rolling collateral |
| --- | --- | --- |
| Optional acceptance | All selected jobs at departure | Each job on arrival for pickup |
| Collateral | Active plus all selected collateral must fit at departure | Active funds start locked; optional pickup locks funds and delivery releases them |
| Listing expiry | Must be unexpired at departure | Pickup arrival must be strictly before expiry |
| Optional delivery deadline | Days to complete after departure | Days to complete after pickup arrival |

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
reachable region blocks threat-aware planning. A successful response containing 1,000 rows is
covered but explicitly incomplete; the proof remains conditional on that observed avoid set.

The older ESI aggregate ship-kill threshold remains usable through the CLI. It does not classify
suicide ganks or locate gate combat. Missing aggregate data rejects that policy while allowing
ordinary planning.

## Execution and recovery

Arming replays the proposed itinerary at actual departure. Locked mode requires confirmation of
real acceptance. Rolling proposals become commitments only when real pickup is recorded.
Accepted contracts are embedded in execution state, so disappearance from public ESI cannot erase
them. Delivered IDs prevent cached listings from being selected twice in the same session.

Replanning retains active shipments, original terminal, remaining required systems, resource limits
and session deadline. Refreshed observations update the declared threat policy. Extending the
horizon changes the session deadline only. Real progress can be recorded after that horizon,
subject to shipment deadlines and resources. An infeasible proposal does not discard obligations.
