# Interfaces and persistence

Use this reference to understand saved sessions, inspect exported plans, or integrate with the
local application. For using the browser interface, start with the [README](../README.md).
EVE and planning terms are defined in the [domain glossary](DOMAIN.md#terms-used-in-the-planner).

## From observation to trip

The application keeps three different kinds of information:

| Stage | Question it answers | What changes it |
| --- | --- | --- |
| Snapshot | What public contracts and activity did the scan observe? | A new scan or replan with refreshed observations |
| Plan | What route does the optimizer propose for these inputs? | A solve or replan |
| Execution | What has the pilot actually accepted, picked up and delivered? | Arming, recorded progress and explicit session updates |

A proposed plan is not proof that a contract was accepted in EVE. Once acceptance is recorded,
the saved execution retains that obligation even if the public listing disappears. Keep these
roles separate when building an integration or diagnosing recovery.

## Local API

`web/server.py` serves loopback HTTP and static assets. `web/requests.py` decodes input before
`web/workspace.py` invokes the planner and saves the resulting snapshot, plan or courier trip.
The browser uses native ES modules with no frontend build step. Requests are same-origin JSON;
responses carry either a result or an `error` message. Invalid input returns 400, failed external
feeds return 502, stale revisions or proposals return 409, and internal/storage failures return 500
with server-side diagnostics.
Host/origin checks, a content-security policy,
a fixed asset/download allowlist and a 1 MiB request limit protect the local boundary. At most
16 connections are handled concurrently; socket reads time out after five seconds of inactivity,
and a ten-second deadline closes unfinished headers or request bodies. Reading a slow body does
not hold the workspace mutation lock.

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
| `GET /download/{snapshot,plan,execution}.json` | Portable JSON exports from the committed workspace |

Only one long job runs per application instance. Its spawned worker computes an immutable result;
the parent validates the starting revision and publishes the complete transition once. Cancelling
the worker preserves the previous saved state. Mutations return 409 while a job runs. Reloading the
browser reconnects to it; restarting the server ends it.

Every status or mutation result carries an integer `revision` and a nullable `proposal_id`.
`POST /api/execution/start` requires both `expected_revision` and the exact `proposal_id` reviewed
by the caller, plus `confirm_locked_acceptance: true` when accepting new locked-mode contracts.
A proposal can be applied once. Solving again, recording progress, another writer, or a restart
invalidates its identity. On 409, reload and review the current proposal before retrying.
The browser sends `expected_revision` with every mutation and job input. Other API mutations
accept this precondition optionally; omitting it means acting on the instance's current state.
An atomic disk revision check still prevents an out-of-date instance from overwriting another.

Python response types define the plan, status, and job contracts. Generated browser declarations
and runtime validators use those same definitions; CI checks for drift and type-checks the native
JavaScript. There is no frontend build or browser runtime framework. Job outcomes distinguish
`running`, `completed` with a result, `failed` with an error, and `cancelled`.

## Saved files

The web app commits snapshot, execution and proposal together in **`workspace.json` (schema 1)**.
A short operating-system file lock and revision comparison protect each write across application
instances. The writer flushes and replaces one sibling temporary file before publishing its new
in-memory state. Failed or cancelled work preserves the previous complete state. Atomic visibility
is not a guarantee against hardware or filesystem failure; keep backups of accepted commitments.

On first launch in an older workspace, the app imports the three legacy files once, leaving them
untouched as migration backups. After migration only `workspace.json` is authoritative. A malformed
legacy proposal is ignored with a warning; corrupt authoritative snapshot or execution data fails
visibly rather than silently dropping obligations. Copy the current workspace before downgrading;
the legacy backups do not include progress recorded after migration.

Downloads and standalone CLI commands use these portable formats:

| File | Schema | Contents |
| --- | ---: | --- |
| `snapshot.json` | 2 | Observation time, compatibility date, SDE build, regions, public contracts, optional aggregate activity and full gate-threat evidence |
| `plan.json` | 3 | Selected IDs, integer reward, finish, proof certificate, scope, constraints, route actions and physical travel legs |
| `execution.json` | 3 | Current system/time, session deadline, limits/policy, original terminal, pending required systems, accepted shipments and delivered IDs |

Snapshot schema 1 and execution schemas 1–2 remain readable. Missing historical route-shape fields
mean open routes without required systems or parcel limits. Missing threat observations never imply
complete coverage. An accepted shipment embeds its resolved contract and absolute deadline because
it must remain enforceable after the public listing disappears.

Money uses centi-ISK (100 units = 1 ISK) and volume uses thousandths of m³ (1,000 units = 1 m³).
Decimal strings in plans are display copies.
`travel_legs` records pickup, delivery, waypoint and finish targets with the exact ordered system
path. The UI adds SDE names/security values without calculating another route. Route-only plans may
have no courier actions but still contain travel legs. Undefined ranking ratios serialize as `null`.

A saved plan is validated before decoration and restored for display only when its snapshot/SDE
identity matches. Arming requires an in-memory verified plan, the reviewed proposal identity and
revision, followed by departure-time replay. After an SDE upgrade, the old acquisition metadata
remains available so **Refresh market & replan** can fetch a compatible snapshot while retaining
accepted shipments and deadlines. Missing required systems in the new SDE cause an explicit error.

Standalone CLI writers use the same directory lock. They reject paths reserved by a managed web
workspace; download a portable file into another directory before using CLI state commands.

### Where files are stored

| Platform | Default workspace |
| --- | --- |
| Linux | `$XDG_DATA_HOME/eve-courier-route-optimizer`, or `~/.local/share/eve-courier-route-optimizer` when unset |
| macOS | `~/Library/Application Support/EveCourierRouteOptimizer` |
| Windows | `%LOCALAPPDATA%/EveCourierRouteOptimizer` |

Pass `web --workspace PATH` to choose another directory. Stop the application before copying the
workspace for a backup, so the files are not changing during the copy. Back up `workspace.json`
and retain the matching application version and SDE build for recovery.

## External data

The ESI client pins compatibility date `2026-08-05`. Cache keys include that date and URL; conditional
responses preserve cached pagination headers. Invalid JSON, malformed courier records and exhausted
request retries fail explicitly. Optional aggregate activity outages are recorded as absent.
ESI acquisition shares a 300-second cooperative budget across concurrent regions, pagination and
retries; each region is bounded to 100 pages and 100,000 rows. HTTP bodies are capped at 16 MiB.
Provider retry delays that cannot fit the remaining budget fail with a “try again later” message.

zKill requests are sequential and spaced, use a 15-minute cache and bounded retries, and record
successful/incomplete regional coverage separately. Collection follows full 200-row pages, up to
10 pages per region and 100 page requests overall, with a separate 300-second cooperative budget.
Those page limits exclude bounded retries. Compressed and expanded bodies are capped at 16 MiB.
Timeouts, truncated HTTP/gzip bodies and unfinished pagination produce explicit failure or incomplete
coverage. The configured lookback must be an hourly multiple from one through 168 hours.
See [classification and collection limits](DOMAIN.md#gate-threat-policy).

These deadlines are checked between requests, body chunks and waits. They are not hard real-time
limits: an ongoing socket read or operating-system DNS operation can delay cancellation. Background
job cancellation terminates the worker process when the user needs to stop acquisition immediately.

The SDE builder streams the required official JSONL tables, checks SQLite integrity and replaces
the database only after closing it. An offline rebuild needs matching `--zip`, `--build-number` and
`--release-date`; normal operation uses the packaged subset. Frozen Empire benchmarks carry their
own SDE so an application data refresh cannot silently change their mathematical input.
