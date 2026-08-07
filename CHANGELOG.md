# Changelog

This file is intentionally release-level rather than commit-level. Git records implementation
history; this page records changes an operator should care about. GitHub Releases can reuse these
notes when tagged versions are published.

## 1.5.0 - 2026-08-07

Proof-guided decomposition release.

- Promote the endpoint-system relaxation from a passive ceiling into a reusable master problem. An
  optimal master selection is now handed to the exact pickup/dropoff model as a much smaller routing
  subproblem before the monolithic event model is attempted.
- Close a global proof immediately when the independently verified exact route reaches the rigorous
  master optimum. The frozen 96-contract DST benchmark now proves 58.000000M ISK globally optimal
  instead of stopping at a 41.251527M incumbent and a 58M ceiling.
- Learn proof-safe higher-order selection cuts when a master-optimal contract set is exactly
  infeasible. CP-SAT assumption cores are deletion-shrunk only after additional proven-infeasible
  checks; UNKNOWN shrink attempts never justify a cut.
- Keep the monolithic full pickup/delivery CP-SAT model as a fallback whenever the master is not
  solved optimally, an exact subproblem times out, or the bounded decomposition phase does not close.
- Extend plan certificates and localhost proof diagnostics with decomposition status, iteration and
  learned-core counts, exact-subproblem time, and whether the composite proof closed directly.
- On the unchanged frozen Empire snapshot, the release run proved DST at 58.000000M ISK in 7.769 s
  and BR at 25.651527M ISK in 0.614 s on this environment. Both passed the independent route verifier.

## 1.4.1 - 2026-08-07

First publication-ready release candidate.

- Make restored execution state impossible to miss. A persistent live-route banner and sticky
  top-bar indicator now explain why Scan, Rank and Solve are locked while accepted courier
  commitments are preserved across restarts.
- Add direct **Resume current route** and safe **End execution** actions to restored sessions. An
  expired planning horizon is called out without silently discarding recorded in-game commitments.
- Restore the saved mathematical model into planner controls when reopening a plan, so displayed
  cargo, collateral, security, route shape and threat settings match the result being inspected.
- Replace the developer-heavy README with a user-first project page, authentic UI screenshots,
  quick start, proof semantics, benchmark results and a compact documentation map.
- Add public-repository hygiene: project URLs, issue forms, pull-request template, security policy,
  Dependabot configuration, EditorConfig and a tag-triggered distribution/release workflow.
- Keep the v1.4.0 solver model and frozen benchmark baseline unchanged. The BR frozen Empire profile
  remains globally proven at 25.651527M ISK; the DST profile retains a 58M rigorous ceiling and
  40.601% recorded proof gap after its 60-second exact search.

## Pre-public development - 1.0.0 through 1.4.0

The pre-public iterations established the core product:

- public ESI courier acquisition with bounded parallel region scans, caching and rate-limit-aware
  retries;
- a bundled CCP SDE stargate graph with NPC station resolution, security policies, system avoids,
  exact shortest gate paths and proof-safe region presets;
- standalone opportunity ranking plus exact OR-Tools CP-SAT pickup/delivery optimization with cargo,
  collateral, deadlines, required waypoints, loop/open routes, fixed finish and simultaneous-parcel
  constraints;
- optimality certificates with exact integer objectives, best bounds, proof gaps, mathematical-input
  fingerprints and independent route simulation;
- persistent locked/rolling execution state, pickup/delivery/waypoint transitions and replanning that
  cannot silently drop accepted courier commitments;
- gate-focused zKill threat evidence for suicide ganks, smartbombs, heavy interdictors, carriers,
  camps, hauler losses and generic gate PvP, with explicit coverage requirements;
- a localhost-only web control deck with autocomplete selectors, human-readable ISK/time inputs,
  gate-by-gate pilot directions and proof diagnostics; and
- proof strengthening for dense cases using resource-aware incompatibility pairs, clique cuts, a
  global elapsed-time equality and a deterministic endpoint-system reward relaxation.

Detailed mathematical, API, benchmark and architecture history lives in the versioned documentation
and source alongside the code that implements it.
