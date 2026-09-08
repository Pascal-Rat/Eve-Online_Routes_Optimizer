# EVE Online Courier Route Optimizer

**Find the contracts that work best together. Leave with a plan for the whole trip.**

In EVE Online, a courier contract pays you to move a parcel between stations. Choosing what to haul
means balancing the payout against your cargo space, the money tied up as collateral, and the time
you have to fly. The biggest individual reward is not always the best use of your trip.

This planner chooses **which public courier contracts to take and the order to collect and deliver
them**, together. Give it your ship's limits, a starting system and a time budget. It searches for
the greatest total contract reward, then shows the route and whether a better result is still
possible under those rules.

![Desktop planner showing ship limits, selected courier contracts, a gate itinerary and reward proof](docs/assets/ui-planner.png)

*Plan in your browser; accept contracts and fly the route yourself in EVE.*

## Why use it?

- **Make contracts work together.** Combine pickups and deliveries that share travel, while keeping
  cargo, collateral and deadlines within your limits.
- **Plan the trip you want to fly.** Return home, finish in a chosen system, or leave the destination
  open. Add required stops, security restrictions and systems to avoid.
- **See how much room for improvement remains.** Each returned route is checked independently of
  the search. A reward ceiling tells you whether the search has proved the best result or still
  leaves an open gap.
- **Track a trip already underway.** Record actual pickups and deliveries, then replan around
  accepted contracts and their deadlines.
- **Keep the workflow local.** Run a desktop browser interface or use the command line. Public data
  is enough: no EVE account login or API credentials are required.

For example, a 9M ISK contract can be a worse choice than two compatible 8M contracts. The
[worked example](docs/HOW_THE_SOLVER_WORKS.md#a-small-example-you-can-check-yourself) shows why,
and how the planner can prove the combined 16M result is best. ISK is EVE's in-game currency.

## Run it locally

You need **Python 3.12 or newer** and a desktop browser at least 1024 pixels wide. Installation and
live scans need internet access; planning from compatible saved snapshots and running the included
benchmarks work offline. The map data needed for normal use is bundled.

Get the source with Git, then open its directory:

```bash
git clone https://github.com/Pascal-Rat/Eve-Online_Routes_Optimizer.git
cd Eve-Online_Routes_Optimizer
```

You can also choose **Code → Download ZIP** on GitHub, extract it, and open a terminal in the
extracted directory. Run the following commands there.

**Linux or macOS**

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/eve-courier web
```

**Windows PowerShell**

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install .
.\.venv\Scripts\eve-courier.exe web
```

Use the equivalent Python command if you installed a newer version. These commands create a
separate Python environment for the application; you do not need to activate it.

The browser opens at `http://127.0.0.1:8765/`. Keep the terminal running while you use the planner;
press `Ctrl+C` there to stop it. If the browser does not open, visit that address yourself. Add
`--port 8766` if the default port is already in use, or `--no-browser` to suppress automatic opening.

## Plan your first trip

1. **Choose where to look.** Select regions and click **Scan public contracts**. A scan saves a
   *snapshot*: the listings observed at that time. Region presets help you choose a search area.
2. **Describe your ship and session.** Set the starting system, cargo space in m³, collateral budget
   in ISK, and available flying time. Choose the security bands you allow. Routes return to the
   start by default; turn the loop off for a fixed or open finish.
3. **Choose when to commit your collateral.** **Locked at start** assumes you accept all selected
   contracts before departure. **Rolling at pickup** assumes you accept each new contract when you
   reach its pickup, reusing collateral released by deliveries. Future listings may disappear;
   [the domain guide](docs/DOMAIN.md#collateral-modes) explains the tradeoff.
4. **Find a route.** Use **Rank opportunities** to inspect individual contracts, then
   **Optimize & prove** to choose a combination and visit order. Ranking is optional and does not
   limit which eligible contracts the optimizer considers.
5. **Review before committing.** Check the selected contracts, reward, proof status and gate
   itinerary. In locked mode, accept those contracts in EVE before confirming their acceptance and
   choosing **Arm this plan for execution**. Arming starts the planner's trip tracker; it does not
   accept contracts or control your ship in EVE.

Money fields accept amounts such as `750M` and `1.5B`. The flying-time budget and solver time limit
are different: one limits your trip; the other controls how long the program searches for a plan.
You can also set cargo to zero to plan a route through required systems without taking contracts.

After refreshing an active trip, review the proposal and choose **Apply revised plan to execution**
in either collateral mode. Newly proposed pickups stay disabled until that policy is applied.

While flying, record actual pickups, deliveries and required stops. Use **Replan** to search again
while retaining your accepted obligations. Saved execution state survives normal restarts. If more
flying time is needed, extend the planning horizon; contract deadlines stay unchanged. An infeasible
replan does not erase accepted jobs or prevent recording real progress.

## Read the result

The **reward** is what the returned route would pay before expenses. The **reward ceiling** is the
most any route could earn according to the search's current proof. Their difference is the
remaining gap. A 16M route with a 19M ceiling has at most 3M of possible improvement; that extra
reward may or may not be achievable.

| Status | What it means for your plan |
| --- | --- |
| `proven_optimal` | The checked route reaches the reward ceiling. No allowed route in the recorded problem earns more. |
| `feasible_not_proven` | A checked route exists, but the search has not ruled out a better one. |
| `proven_infeasible` | The mandatory trip requirements cannot all fit the current rules. Review limits and obligations. |
| `unknown` | The search found neither a usable route nor a proof of impossibility. This is not an infeasibility verdict. |

The planner maximizes **total gross reward**, not net profit or ISK per hour. Fuel, losses, taxes
and opportunity cost are outside the calculation. Proving the reward does not by itself prove
the shortest trip among equally rewarding routes.

### Scope and current limits

This is a **beta**. Read the [current limitations](docs/DOMAIN.md#current-beta-limitations) before
using the trip tracker or live threat filtering.

The model covers NPC-station contracts and normal stargates. It uses your estimates for jump and
pickup/delivery time. It does not route through player structures, wormholes, Ansiblex jump gates,
filaments or jump drives. The proof applies to the recorded listings and declared rules: it cannot
guarantee live contract availability or actual flight time. An explicit candidate cap narrows the
search and is marked as a truncated proof scope.

Optional gate-threat filtering avoids systems based on recorded zKillboard losses. Those reports
are observations, not a prediction of safety, and the current collector can miss reports in busy
regions. Check the [observation limits](docs/DOMAIN.md#gate-threat-policy) when using that option.

## Prefer the command line?

From the source directory on Linux or macOS:

```bash
.venv/bin/eve-courier scan --region "The Forge" --region "The Citadel" --output contracts.json
.venv/bin/eve-courier rank --snapshot contracts.json --start Jita \
  --cargo-m3 62500 --collateral-isk 10B --hours 1 --planning-time now --limit 20
.venv/bin/eve-courier solve --snapshot contracts.json --output plan.json \
  --start Jita --cargo-m3 62500 --collateral-isk 10B --hours 1 --planning-time now \
  --security highsec --loop --time-limit 300 --workers 4
```

This example searches two regions and plans a one-hour high-security loop from Jita, allowing up to
five minutes of optimization. Your results depend on the observed contracts. On Windows, use
`.\.venv\Scripts\eve-courier.exe` and put each command on one line.

Run the executable with `COMMAND --help` for options, including `replan`, `advance` and `extend`.
Omit `--planning-time now` to use the snapshot's timestamp for reproducible historical replay.
The browser interface uses the current time and rechecks the itinerary when you arm it.

## Explore the project

| If you want to… | Start here |
| --- | --- |
| Understand how a route can be proved best | [How the solver works](docs/HOW_THE_SOLVER_WORKS.md) — a small example, then the search explained |
| Check exactly what the planner assumes | [Domain rules](docs/DOMAIN.md) — terms, collateral, deadlines, routes and observations |
| Inspect reproducible results | [Benchmarks](docs/BENCHMARKS.md) — fixed inputs, known best rewards and comparison commands |
| Review the mathematics | [Optimization and proof](docs/OPTIMIZATION.md) — models, bounds, correctness arguments and budgets |
| Integrate with saved data or the local API | [Interfaces](docs/INTERFACES.md) — file formats, storage and endpoints |
| Report a bug or improve the project | [Contributing](CONTRIBUTING.md) — useful contributions, code map and validation |

Project-owned code and documentation are [MIT licensed](LICENSE). EVE Online and related game data
belong to CCP Games; this independent project is not affiliated with CCP. See
[third-party notices](THIRD_PARTY_NOTICES.md) and the [security policy](SECURITY.md).
