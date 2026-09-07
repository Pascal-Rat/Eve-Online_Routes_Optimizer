# Development

Install Python 3.12+ and an editable development environment:

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e ".[dev,browser]"
.venv/bin/python -m playwright install chromium
```

## Find the code

All Python application modules are in `src/eve_courier_optimizer`.

| Responsibility | Entry points |
| --- | --- |
| Contracts, constraints, route events and certificates | `domain.py` |
| SDE graph, permitted shortest paths and database build | `sde.py`, `sde_build.py` |
| Public observations | `scanner.py`, `esi.py`, `threat_intel.py`; shared transport/cache in `http.py` |
| Observed route policy and preparation | `route_policy.py`, `planning.py` |
| Search orchestration and budgets | `solver.py`, `search_config.py` |
| Complete mathematical model | `event_model.py` |
| Master, exact selection checks and strengthening | `decomposition.py`, `bounds.py`, `subset_search.py`, `batch_search.py` |
| Verified constructive incumbents | `construction.py` |
| Independent replay and exhaustive reference | `verification.py`, `reference_solver.py` |
| Accepted commitments and transitions | `execution.py` |
| Shared scan/solve/replan/arm workflow | `service.py` |
| Durable local session and cancellable jobs | `session.py`, `jobs.py` |
| CLI and HTTP boundaries | `cli.py`, `webapp.py`, `web_options.py` |
| Serialization and display | `snapshot.py`, `reporting.py`, `presentation.py`, `jsonio.py` |
| Browser controller, form, route display and autocomplete | `web/app.js`, `planner_form.js`, `route_view.js`, `autocomplete.js` |

The main flow is observation → policy/constraints → prepared problem → search → independent replay
→ plan. `RoutePlan` keeps a prepared input together with its result. Arming turns a revalidated plan
into execution state; replanning combines a fresh observation with those persistent obligations.
Search never calls external APIs. Graph topology is immutable to callers; query caches are bounded
and discarded when a graph crosses the spawned-worker boundary.

## Validate

```bash
.venv/bin/ruff check src tools tests benchmarks browser_tests
.venv/bin/ruff format --check src tools tests benchmarks browser_tests
.venv/bin/mypy src tools tests benchmarks browser_tests
.venv/bin/pytest
.venv/bin/pytest browser_tests --no-cov --browser chromium --tracing retain-on-failure
.venv/bin/python -m benchmarks.run_frozen --time-limit 10
.venv/bin/python -m benchmarks.run_empire --time-limit 60 --workers 4
.venv/bin/python -m pip wheel . --no-deps -w dist
```

Tests enforce at least 85% branch-inclusive coverage, including spawned workers. Browser tests use
synthetic local responses and exercise scanning, ranking, solving, arming, persistence, infeasible
recovery, cancellation and asynchronous autocomplete. No frontend compiler or runtime dependency
is needed beyond the browser. Live-network tests must remain opt-in.

For solver or preprocessing changes, follow [benchmark methodology](docs/BENCHMARKS.md). Protect
small hand-checkable optima and feasibility boundaries as well as representative workloads.
Keep generated benchmark results, diagnostic dumps and validation logs out of commits.
A safe reduction needs a mathematical reason it cannot remove an optimum. Heuristic caps must
remain explicit and mark proof scope. An infeasible core needs an actual infeasibility proof; a
relaxation incumbent must never be reported as a reward ceiling.

Keep independent verification independent: do not share solver state propagation or production
search with the reference implementation. New mathematical inputs belong in the problem fingerprint
and persisted model. Preserve accepted-state compatibility when modifying artifact schemas.

[Domain rules](docs/DOMAIN.md), [optimization](docs/OPTIMIZATION.md), and
[interfaces](docs/INTERFACES.md) document the non-obvious contracts. Keep explanations there concise;
source code should reveal ordinary control flow. See [SECURITY.md](SECURITY.md) and
[third-party notices](THIRD_PARTY_NOTICES.md) before publishing data or changing integrations.
