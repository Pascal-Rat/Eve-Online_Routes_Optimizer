# JSON artifact contracts

Every user-facing JSON artifact carries an integer `schema_version`. Exact cargo and money values are
stored as scaled integers, not binary floating point:

- `volume_units`: 0.001 m³ per unit;
- `collateral_units` and `reward_units`: 0.01 ISK per unit.

Human-readable decimal strings in plans are presentation copies. The integer fields are the proof
inputs and outputs.

## Contract snapshot -- schema 2

Created by `eve-courier scan`. Schema 2 adds auditable gate-threat observations; the reader still
accepts schema 1 snapshots and treats their threat observation as absent.

| Field | Meaning |
| --- | --- |
| `schema_version` | currently `2` |
| `fetched_at` | timezone-aware observation completion time |
| `compatibility_date` | ESI behavior date sent with requests |
| `sde_build_number` | route/type data build used by the scan |
| `region_ids` | exact regional contract scan scope |
| `contracts` | observed public couriers ordered by contract ID |
| `system_kills_fetched_at` | legacy ESI aggregate observation time, or `null` |
| `system_kill_activity` | legacy per-system ship/pod/NPC aggregate rows |
| `threat_intel` | zKillboard observation object, or `null` when not requested |

`threat_intel` contains:

| Field | Meaning |
| --- | --- |
| `source` | fixed string `zkillboard` |
| `fetched_at` | UTC observation time |
| `window_seconds` | requested lookback |
| `gate_radius_m` | fallback victim-position radius |
| `coverage_region_ids` | regions whose request returned successfully |
| `incomplete_region_ids` | failed regions or regions hitting the 1,000-row ceiling |
| `killmails_seen` | number of raw regional rows processed before gate filtering |
| `gate_events` | deterministic retained events ordered by time and killmail ID |

Each gate event records killmail/time/system/region/gate IDs, distance, localization evidence,
categories, victim ship type, attacker ship/weapon types, player-attacker count, and zKill labels.
Those raw facts make the later hard-avoid derivation reviewable without re-querying zKillboard.

## Solve result -- schema 3

Created by `solve` or `replan`. Schema 3 retains schema 2's explicit route-shape/resource fields and
canonical travel legs, and adds auditable upper-bound strengthening metadata to the certificate.

The major objects are:

- `summary`: selected contract IDs, exact/human reward and finish seconds;
- `certificate`: solver status, exact objective/bound/gap, SHA-256 fingerprint, solver metadata,
  scope/verification flags, bound-strengthening metadata, and human proof claim;
- `scope`: snapshot/SDE provenance plus observed/eligible/excluded/reduced counts;
- `model`: start/resources/horizon/time model/collateral mode and complete routing policy;
- `route`: ordered pickup/delivery steps; and
- `travel_legs`: ordered physical travel targets for pickup/delivery, required waypoints, and the
  final return/fixed finish.

Every route step includes sequence/action/contract/system/location IDs, arrival/completion seconds,
cargo and collateral after the action, cumulative reward, and a concrete shortest `jump_path`.
`jump_path` is the canonical ordered list of solar-system IDs used by independent verification, so
the plan contains the exact transit route rather than only a jump count. The localhost API decorates
those IDs in memory as `jump_path_systems` (name, security status and band) for the gate-by-gate UI;
that convenience decoration is derived from the pinned SDE and is not a second routing calculation.

Every `travel_legs` entry contains `sequence`, `kind`, `from_system_id`, `to_system_id`, arrival and
completion seconds, optional `contract_id`, and canonical `jump_path`. `kind` is one of `pickup`,
`delivery`, `waypoint`, or `finish`. The localhost API decorates the two endpoint names and the path
systems for display. A plan may therefore have an empty `route` but non-empty `travel_legs` when it
is being used only as a route finder.

Modern routing-policy fields inside `model` are:

| Field | Meaning |
| --- | --- |
| `allowed_security_bands` | exact high/low/null set |
| `avoided_system_ids` | manual avoids |
| `return_to_start` | whether the route must close at its original start |
| `required_system_ids` | systems that must be visited at least once |
| `finish_system_id` | explicit non-loop finish requested by the user, or `null` |
| `terminal_system_id` | effective final system after applying loop/fixed-finish semantics |
| `max_simultaneous_contracts` | picked-but-undelivered parcel cap, or `null` for unlimited |
| `threat_avoided_system_ids` | derived hard avoids for this solve |
| `threat_categories` | selected gate signatures |
| `threat_min_events` | distinct matching events required per system |
| `threat_intel_fetched_at` | observation identity |
| `threat_window_seconds` | observation lookback |
| `threat_gate_radius_m` | localization radius |
| `threat_coverage_region_ids` | successful observation scope |
| `threat_incomplete_region_ids` | explicitly incomplete scope |

Legacy `minimum_security`, `gank_ship_kill_threshold`, `gank_activity_fetched_at`, and
`gank_avoided_system_ids` remain for reading early v1 plans and the compatibility CLI option. All
active routing fields enter `problem_sha256`.

`certificate.bound_strengthening` records proof-performance facts that do not alter the mathematical
problem fingerprint:

| Field | Meaning |
| --- | --- |
| `system_relaxation_status` | CP-SAT status of the auxiliary endpoint-system solve, or `null` when skipped |
| `system_relaxation_bound_units` | rigorous auxiliary reward ceiling in centi-ISK, or `null` |
| `system_relaxation_bound_isk` | decimal presentation copy of that ceiling |
| `system_relaxation_wall_time_seconds` | CP-SAT wall time spent in the auxiliary solve |
| `system_relaxation_systems` | number of distinct endpoint/mandatory systems in the relaxation |
| `incompatibility_pairs` | number of valid two-contract incompatibilities derived |
| `incompatibility_cliques` | number of deterministic clique cuts derived from those pairs |
| `decomposition_status` | master/exact phase outcome such as `bound_matched`, or `null` when not applicable |
| `decomposition_iterations` | number of endpoint-master iterations attempted |
| `decomposition_learned_cuts` | number of rigorously proven exact assumption-core cuts fed back |
| `decomposition_subproblem_wall_time_seconds` | CP-SAT wall time spent in reduced exact/core and fixed-selection refinement solves |
| `decomposition_proof_closed` | whether the composite master/exact path itself closed the result |

When the full exact fallback has an incumbent, final `best_bound_units` is its bound after these valid
constraints are attached. A master/exact proof instead reports the matching master ceiling and
verified exact reward directly. If fallback search stops `UNKNOWN` without an incumbent, a separately
valid system-relaxation ceiling may still be reported as the only known bound. A relaxation incumbent
is never serialized as an upper bound.

## Execution state -- schema 3

Created by `solve --state-output`, `replan --state-output`, `advance`, or the localhost control deck.
The reader accepts schema 1 and 2 state. Older states predate route-shape persistence, so they are
read as open routes with no required systems and no simultaneous-contract limit.

Execution state contains:

- current time/system and immutable original session deadline;
- cargo/collateral limits and collateral mode;
- `terminal_system_id`, preserving the original loop return or fixed finish across replans;
- `remaining_required_system_ids`, removing systems as actual progress reaches them;
- `max_simultaneous_contracts`, or `null` when the parcel count is unlimited;
- deterministic travel assumptions;
- the complete security/manual/threat policy, including coverage and derived avoids;
- active shipment list; and
- `completed_contract_ids`, preventing a stale public observation from reintroducing an already
  delivered courier during the same execution session.

Each active shipment embeds the immutable public contract, resolved origin/destination system,
absolute deadline, and `picked` flag. This duplication is intentional: an accepted job remains
replannable even when it disappears from every later public snapshot.

## Compatibility and validation

The localhost UI consumes these schemas through Python serialization and service boundaries. It does
not reproduce resource/routing semantics in JavaScript.

Readers reject unknown future schema versions rather than guessing. Schema-1 snapshot and legacy
execution readers supply explicit defaults for fields that did not yet exist; they never fabricate
zero threat event counts or complete coverage. A future incompatible interpretation must increment
the relevant schema and add a deliberate migration path.

Writers use a temporary sibling file and atomic replacement so a process interruption does not leave
half-written audit state.
