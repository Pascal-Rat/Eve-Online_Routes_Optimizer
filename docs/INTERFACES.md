# Interfaces and persistence

## Local API

`webapp.py` owns loopback HTTP and static assets; `session.py` owns the durable planning session.
The browser uses native ES modules with no frontend build step. Requests are same-origin JSON;
responses carry either a result or an `error` message. Invalid input returns 400, failed external
feeds return 502, and internal/storage failures return 500 with server-side diagnostics.
Host/origin checks, a content-security policy,
a fixed asset/download allowlist and a 1 MiB request limit protect the local boundary.

| Method and path | Operation |
| --- | --- |
| `GET /api/status` | Graph identity, snapshot, plan, execution and latest job |
| `GET /api/systems?q=…`, `/api/regions?q=…` | SDE suggestions |
| `POST /api/jobs` | Start `scan`, `rank`, `solve` or `replan` with an `input` object; HTTP 202 |
| `GET /api/jobs/{id}` | Progress, elapsed time, completed result or error |
| `POST /api/jobs/{id}/cancel` | Cancel the whole worker process |
| `POST /api/scan`, `/api/rank`, `/api/solve`, `/api/replan` | Synchronous equivalents |
| `POST /api/execution/start` | Revalidate and arm the current verified plan |
| `POST /api/action` | Record pickup, delivery, waypoint or terminal progress |
| `POST /api/execution/extend` | Add positive integer `minutes` to the planning horizon |
| `POST /api/execution/reset` | Explicitly discard execution state |
| `GET /download/{snapshot,plan,execution}.json` | Existing durable artifacts |

Only one long job runs at a time. It owns a spawned process and temporary workspace. The parent
publishes a completed result; cancelling the worker preserves the previous session. API mutation
is serialized while a worker runs. A browser reload reconnects to the job; a server restart ends it.
The API serves the local UI; the versioned files below are the durable interchange contract.

## Artifact schemas

Writers publish each file by flushing a sibling temporary file and atomically replacing its target.
This prevents partial JSON files; it is not a transaction spanning all three artifacts. Readers
validate integer/boolean fields and reject unknown future schema versions.

| Artifact | Schema | Authoritative contents |
| --- | ---: | --- |
| `snapshot.json` | 2 | Observation time, compatibility date, SDE build, regions, public contracts, optional aggregate activity and full gate-threat evidence |
| `plan.json` | 3 | Selected IDs, integer reward, finish, proof certificate, scope, constraints, route actions and physical travel legs |
| `execution.json` | 3 | Current system/time, session deadline, limits/policy, original terminal, pending required systems, accepted shipments and delivered IDs |

Snapshot schema 1 and execution schemas 1–2 remain readable. Missing historical route-shape fields
mean open routes without required systems or parcel limits. Missing threat observations never imply
complete coverage. An accepted shipment embeds its resolved contract and absolute deadline because
it must remain enforceable after the public listing disappears.

Money uses centi-ISK and volume uses thousandths of m³. Decimal strings in plans are display copies.
`travel_legs` records pickup, delivery, waypoint and finish targets with the exact ordered system
path. The UI adds SDE names/security values without calculating another route. Route-only plans may
have no courier actions but still contain travel legs. Undefined ranking ratios serialize as `null`.

A saved plan is restored for display only when its snapshot/SDE identity matches. Arming requires an
in-memory plan with its prepared input and verified result, followed by departure-time replay.

Default UI storage is `$XDG_DATA_HOME/eve-courier-route-optimizer` (or
`~/.local/share/eve-courier-route-optimizer`) on Linux, `~/Library/Application Support/EveCourierRouteOptimizer`
on macOS, and `%LOCALAPPDATA%/EveCourierRouteOptimizer` on Windows. `web --workspace` overrides it.

## External data

The ESI client pins compatibility date `2026-08-05`. Cache keys include that date and URL; conditional
responses preserve cached pagination headers. Invalid JSON, malformed courier records and exhausted
request retries fail explicitly. Optional aggregate activity outages are recorded as absent.

zKill requests are sequential and spaced, use a 15-minute cache and bounded retries, and record
successful/incomplete regional coverage separately. The configured lookback must be an hourly
multiple from one through 168 hours. Classification rules are in [DOMAIN.md](DOMAIN.md).

The SDE builder streams the required official JSONL tables, checks SQLite integrity and replaces
the database only after closing it. An offline rebuild needs matching `--zip`, `--build-number` and
`--release-date`; normal operation uses the packaged subset. Frozen Empire benchmarks carry their
own SDE so an application data refresh cannot silently change their mathematical input.
