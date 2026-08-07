# ESI and SDE integration

The optimizer needs only public EVE data. Its normal scan/solve path does not perform SSO, request an access
token, or require a client ID/secret.

## Authoritative sources

The implementation is based on CCP's current developer documentation:

- [ESI overview and compatibility dates](https://developers.eveonline.com/docs/services/esi/overview/)
- [ESI rate limiting](https://developers.eveonline.com/docs/services/esi/rate-limiting/)
- [route calculation](https://developers.eveonline.com/docs/guides/route-calculation/)
- [ESI API Explorer](https://developers.eveonline.com/api-explorer)
- [Static Data Export](https://developers.eveonline.com/docs/services/static-data/)
- [Developer License Agreement](https://developers.eveonline.com/license-agreement)

The CP solver dependency is documented separately by
[Google OR-Tools](https://developers.google.com/optimization/cp/cp_solver).

## Public courier discovery

For each requested region, `EsiClient` calls the public regional contract route:

```text
GET https://esi.evetech.net/contracts/public/{region_id}/?page={page}
```

It filters rows whose `type` is `courier` and records the fields needed by the optimizer:

- contract ID;
- start/end location IDs;
- volume;
- collateral and reward;
- listing expiry and issue time;
- days to complete;
- title.

JSON floating values are parsed directly to `Decimal`; money/cargo conversion then happens once at
the domain boundary.

ESI's public data is enough for contract discovery. The application deliberately does not try to accept contracts
through ESI. Real accept/pickup/delivery actions are recorded into execution state by the user.

### Proof-safe regional acquisition scopes

The public-contract resource is partitioned by the contract's **start/pickup region**. That matters
because the route policy already requires a pickup system to be in an allowed security band. Before
making an ESI request, the web UI can therefore ask the local SDE which regions contain at least one
system in the selected bands and omit every other region. A mixed region is retained when *any*
system matches; the exact pickup, delivery and transit systems are still checked later.

This is a coarse acquisition optimization, not a contract heuristic: for the same security policy it
cannot remove a contract whose pickup endpoint could pass the policy. As a live sanity check of the
regional API semantic, the 2026-08-06 cached observation contained 42,446 public records whose start
locations resolved to SDE NPC stations; all 42,446 starts belonged to the region endpoint that
returned them, including 353 cross-region deliveries in that sample.

With bundled SDE build 3458726 the presets resolve to:

| Contract scope | Regions requested |
| --- | ---: |
| High only | 25 of 114 |
| Low only | 23 of 114 |
| Null only | 89 of 114 |
| High + low | 25 of 114 |
| NPC Empire | 24 before intersection with the selected bands |
| All SDE regions | 114 |

`NPC Empire` is deliberately stricter than “security status above zero.” `mapRegions.jsonl` supplies
`factionID`; the preset requires an SDE faction owner **and** a high/low system, then intersects that
set with the selected security bands. This excludes player-sovereign and NPC nullsec as well as a
special high-security SDE region with no faction owner. It includes faction-owned Exordium. The
separate **all SDE regions** preset remains available when the operator explicitly wants the widest
observation.

These presets change only contract acquisition. Threat collection has its own, usually smaller,
proof-safe transit envelope described below and in
[GATE_THREAT_MODEL.md](GATE_THREAT_MODEL.md).

## Legacy public system-kill activity

Normal scans retain one cacheable public request to:

```text
GET https://esi.evetech.net/universe/system_kills/
```

The response is stored alongside the contract observation as system ID plus `ship_kills`,
`pod_kills`, and `npc_kills`, with its fetch timestamp. It is retained for backward-compatible
snapshots, CLI policy support, and diagnostics.

The semantic boundary matters: this endpoint is aggregate kill activity. It contains no field that
attributes a kill to suicide ganking or says where inside the system it happened. The current UI
therefore uses optional gate-focused zKillboard observations instead of presenting this aggregate as
a gank classifier.

If this auxiliary request fails, the scanner still persists the valid public-contract observation
and leaves `system_kills_fetched_at` null. Ordinary solving remains available; enabling legacy
aggregate awareness is rejected until a scan/refresh captures activity successfully. This keeps an
optional feed from becoming a hard dependency while preventing silent “zero kills” assumptions.

## zKillboard gate observations

When the operator enables threat collection, the scanner also fetches recent loss rows for a separate
threat-region scope from zKillboard. In the localhost UI that scope is automatically restricted to
regions reachable from the start inside the declared security/time envelope. This is a separate
third-party public API with its own local cache, request spacing, retry behavior, and explicit
coverage/incompleteness metadata. The solver never contacts zKillboard directly; it reads the
recorded snapshot.

Killmails contribute to route policy only when they are player-caused and resolve to an exact SDE
gate ID or a victim position inside the configured gate radius. Type/group tables in the SDE make
smartbomb, HIC, carrier, and hauler classification deterministic. Full rules and limitations are in
[GATE_THREAT_MODEL.md](GATE_THREAT_MODEL.md).

## Pagination is a bounded observation, not a transaction

The first response's `X-Pages` defines the bounded range for one region. Pages **inside that region**
are requested sequentially. Independent regions are fetched with a small bounded worker pool (four
workers by default, maximum eight), so network latency in one region no longer serializes the entire
requested scope. If the page count changes, the scanner finishes the original range; if a shrinking
market makes a trailing page disappear with HTTP 404, it stops that region cleanly. Duplicate IDs
created by moving page boundaries are removed deterministically.

This protects the scanner from normal live-market churn but cannot make ESI pagination atomic. A
contract that moves across an unread/read boundary can still be missed. The snapshot is therefore
correctly described as **the set observed by this scan**, not “every contract that existed at one
instant.” This distinction is part of the optimality scope.

## Compatibility-date pin

V1 sends:

```text
X-Compatibility-Date: 2026-08-05
```

CCP documents the compatibility date as the application's declared ESI behavior version. Pinning it
makes the response contract explicit instead of silently inheriting an evolving API. If CCP later
raises the oldest supported compatibility date, update the constant only after reviewing the current
contract schema and rerunning the suite.

## HTTP cache and rate behavior

The default ESI cache is a small local SQLite database. For every URL it stores response bytes,
headers, `ETag`, and expiry. Behavior is:

1. return an unexpired cached response without network I/O;
2. after expiry, send `If-None-Match` when an ETag exists;
3. on 304, reuse the body and merge new response headers with cached pagination metadata;
4. on 2xx, replace the cache record atomically at the SQLite transaction level;
5. on 429, sleep for at least the server's `Retry-After` value and retry within the configured limit;
6. on legacy 420 error limiting, respect `X-Esi-Error-Limit-Reset`;
7. on 5xx, use bounded exponential retry;
8. propagate other HTTP failures instead of creating partial silent data.

CCP's current guidance recommends respecting cache times, avoiding constant hammering, handling 429
`Retry-After`, and treating current rate-limit headers/OpenAPI as the authority. Region-level
parallelism is therefore deliberately small and bounded; pagination within a region remains
sequential and every individual request goes through the same cache/retry path.
`scan --scan-workers 1` restores fully sequential acquisition when needed.

## Bundled SDE subset

The current archive contains `src/eve_courier_optimizer/data/route_sde.sqlite3`, generated from
official CCP JSONL SDE build **3458726**, released **2026-08-06T11:07:36Z**.

| Table | Rows |
| --- | ---: |
| systems | 8,490 |
| directed stargate edges | 13,978 |
| NPC stations | 5,210 |
| regions | 114 |
| stargate positions | 13,978 |
| item type groups | 1,610 |
| item-to-group mappings | 52,848 |

The raw SDE is intentionally not bundled. The route subset is sufficient for this model and keeps the
archive small.

CCP's static-data documentation exposes a machine-readable latest build and immutable build ZIP URLs.
`tools/build_sde.py` follows that route. It streams only:

- `mapRegions.jsonl`;
- `mapSolarSystems.jsonl`;
- `npcStations.jsonl`;
- `mapStargates.jsonl`;
- `groups.jsonl`; and
- `types.jsonl`.

Normal stargate links are stored in both directions, matching the normal-gate route model described
by CCP. The builder writes to a temporary SQLite file, commits the complete dataset, runs
`PRAGMA integrity_check`, and only then atomically replaces the target DB.

Region rows also retain the SDE's optional `factionID`. It is used for the NPC-Empire acquisition
preset; security bands still come from individual solar-system status, so mixed regions remain
visible.

To refresh:

```bash
.venv/bin/python tools/build_sde.py
.venv/bin/eve-courier sde-info
.venv/bin/pytest
```

`tools/build_sde.py --zip ...` refuses to pair a ZIP whose filename does not identify the declared
build with unrelated metadata. For an intentional offline rebuild of an older immutable ZIP, pass
its `--build-number` and `--release-date` together. This guard exists because a mislabeled route DB
would defeat the snapshot/SDE identity check used by the proof boundary.

Snapshots record their SDE build. `prepare_problem()` refuses to prove a route when the snapshot's
build and the loaded route graph disagree; refresh the scan or graph rather than mixing universes.

## Local route calculation

CCP's route-calculation guide explicitly describes using the SDE system/stargate graph for local
pathfinding. V1 uses unweighted BFS because its base route objective between action systems is minimum
number of normal stargate jumps.

Security routing classifies the unrounded SDE value into high (`>= 0.45`), low (`> 0` and `< 0.45`),
and null (`<= 0`). The `0.45` boundary is not an application approximation: CCP's documented
`Safer` route-cost example uses `securityStatus < 0.45` for its low-security branch and the `else`
branch for high security. This raw threshold corresponds to the familiar 0.5+ high-sec band shown
to players, which is why the web selector shows both forms. The user may allow any non-empty
combination of the three bands; the restriction applies to endpoints **and transit systems**.
`--avoid-system` removes explicitly named systems as an additional policy. The gate-threat policy
(or legacy aggregate threshold) removes its recorded derived system set in the same way, so local
BFS and post-solve verification operate over exactly the graph represented in the proof fingerprint.

The route graph is static. Structure access, wormholes, Ansiblex networks and other non-normal-gate
travel are outside v1 and are never silently approximated by a stargate edge.

## Endpoint policy

The public contract API can reference locations that are not NPC stations. V1 accepts a courier into
the optimization universe only when both endpoint IDs resolve in the SDE NPC-station table.

This is an explicit policy exclusion because player structures introduce mutable docking/access and
location semantics that the static route DB cannot honestly prove. The number excluded appears in
the plan's `scope.policy_exclusions` under `unsupported_non_npc_station_endpoint`.

Supporting structures later should be a deliberate model extension with live structure resolution,
access/error handling, and a proof-scope flag--not an ID heuristic.
