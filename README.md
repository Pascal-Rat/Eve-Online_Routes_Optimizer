# EVE Online Courier Route Optimizer

Choose public courier contracts and their pickup/delivery order together, subject to your ship,
collateral, deadlines and route policy. The optimizer maximizes **gross courier reward** and reports
an independently verified route, a rigorous reward ceiling, and any remaining optimality gap.

The application runs locally on a desktop, uses public ESI data and a bundled SDE, and needs no EVE
login. Its browser interface uses a desktop layout with a minimum window width of 1024 pixels.

![Planner with a route and its proof certificate](docs/assets/ui-planner.png)

## Run

Python 3.12+ is required. Live scans need internet access; saved snapshots and benchmarks run offline.

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/eve-courier web
```

The browser opens at `http://127.0.0.1:8765/`. Use `web --no-browser`, `--port`, or `--workspace`
to change startup behavior. The server binds only to IPv4 loopback.

1. Select regions and scan public couriers. Security-compatible and NPC Empire presets are available.
2. Set cargo, collateral, time, allowed security bands and route shape. Routes loop by default;
   required systems and a fixed or unrestricted finish are also supported.
3. Rank contracts for inspection, then optimize. Contracts can share travel and cargo space;
   the individual ranking does not restrict the solve.
4. Review reward, bound, proof scope and the named gate itinerary. In locked mode, accept the chosen
   contracts in EVE before confirming and arming execution.
5. Record actual pickups, deliveries and waypoint progress. Refresh and replan as conditions change.

Money accepts exact ISK or suffixes such as `750M` and `1.5B`. A zero-cargo route can still visit
required systems and a destination. Optional gate-threat filtering excludes systems based on recorded
zKill observations; it is an explicit route policy, not a prediction of safety.

Execution survives browser and program restarts. While it is active, use Replan to preserve accepted
jobs. An infeasible replan still permits recording real progress. Extend the planning horizon when
needed; this never extends a contract deadline. End execution when no commitments remain.

## Understand the result

`proven_optimal` means verified reward equals a rigorous upper bound for the recorded problem.
`feasible_not_proven` means a usable route exists with an open gap; `proven_infeasible` means the
mandatory trip cannot satisfy the model; `unknown` means neither conclusion was established.
A candidate cap can exclude the true optimum and is always marked as truncated scope.

The model uses NPC-station endpoints, normal stargates, deterministic jump/service times and a
recorded contract observation. ESI pagination is not a transactional market snapshot. Reward excludes
fuel, losses, taxes and opportunity cost. Read [domain rules](docs/DOMAIN.md) and
[optimization and proof](docs/OPTIMIZATION.md) for the exact scope.

## CLI

```bash
.venv/bin/eve-courier scan --region "The Forge" --region "The Citadel" --output contracts.json
.venv/bin/eve-courier rank --snapshot contracts.json --start Jita \
  --cargo-m3 62500 --collateral-isk 10B --hours 1 --limit 20
.venv/bin/eve-courier solve --snapshot contracts.json --output plan.json \
  --start Jita --cargo-m3 62500 --collateral-isk 10B --hours 1 \
  --security highsec --loop --time-limit 300 --workers 4
```

Use `eve-courier COMMAND --help` for options, including `replan`, `advance` and `extend`.
CLI planning uses the snapshot clock for reproducible replay; `--planning-time now` uses the current
clock. The UI uses current time and rechecks a proposed itinerary when it is armed.

## Develop

[Contributing](CONTRIBUTING.md) covers entry points and validation.
[Interfaces](docs/INTERFACES.md) covers local API and persisted artifacts.
[Benchmarks](docs/BENCHMARKS.md) provides frozen inputs, known optima and reproducible comparisons.
The realistic Empire fixture preserves 58M ISK for DST and 25.651527M ISK for BR, with closed proofs.

Refresh the bundled route data with `.venv/bin/python tools/build_sde.py`, then inspect
`eve-courier sde-info` and rerun validation. A snapshot and route graph must use the same SDE build.

Project-owned code is [MIT licensed](LICENSE). EVE Online and related game data belong to CCP Games;
this independent project is not affiliated with CCP. See [third-party notices](THIRD_PARTY_NOTICES.md)
and the [local security boundary](SECURITY.md). Runtime route artifacts are ignored by Git.
