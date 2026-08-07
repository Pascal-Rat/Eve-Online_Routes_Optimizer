# Contributing

Changes are welcome when they preserve the project's narrow proof claims and reproducible inputs.

## Development setup

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install -e ".[dev]"
```

Run every release gate before submitting a change:

```bash
.venv/bin/ruff check src tools tests benchmarks
.venv/bin/mypy src tools tests benchmarks
.venv/bin/pytest
PYTHONPATH=src .venv/bin/python -m benchmarks.run_frozen --time-limit 10
```

The test command enforces branch-aware coverage of at least 85%.

## Proof-sensitive changes

Any change to preprocessing, arc generation, constraints, objectives, bound conversion, or route
extraction needs tests that would fail if a feasible route were incorrectly removed or an infeasible
route accepted.

- Safe preprocessing must include a mathematical reason it cannot lower the optimum.
- Heuristic deletion must be opt-in and set `scope_untruncated=false`.
- A solution hint must not constrain the feasible set.
- New solver state must enter independent verification when it affects route feasibility.
- New mathematical inputs must enter `problem_sha256` and the plan artifact.
- Status text must distinguish a feasible incumbent from a closed optimality bound.

For small locked-mode cases, compare CP-SAT to `reference_solver.py`. Add or extend a frozen
benchmark for performance work that targets a specific operating profile.

## Data/API changes

- Never query live ESI or zKillboard from inside the optimizer.
- Persist observations first, including timestamps, scope, failure/incompleteness metadata, and source
  identity.
- Keep request behavior sequential, cache-aware, and within the provider's published rules.
- Do not commit OAuth secrets, tokens, personal route artifacts, or runtime cache databases.
- When the SDE schema changes, keep the builder atomic and retain an integrity check.

Live-network tests must be explicitly marked and optional. Default CI uses frozen fixtures.

## Code style

Python is 3.12+, Ruff-clean, and checked with strict Mypy. Domain records are immutable where
practical. Keep transport, persistence, mathematical, verification, and presentation concerns in
their existing modules rather than adding solver logic to CLI or JavaScript code.

## Documentation and license

Update the mathematical model, proof scope, JSON formats and relevant operator guide when behavior
changes. `CHANGELOG.md` stays release-level rather than becoming a commit diary; add an entry only
when the change belongs in operator-facing release notes. Use `$$` blocks for display math so
formulas render in GitHub and common Markdown viewers, and explain important equations in plain
language immediately nearby.

Contributions are distributed under the [MIT License](LICENSE). Third-party EVE/CCP material remains
subject to the notices and terms described in [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
