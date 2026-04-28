# Institution-Grade Adaptive Trading System

A greenfield build for an institution-grade adaptive quantitative trading platform.

## CI

Push and pull requests against `main` run GitHub Actions on **`windows-latest`** (`.github/workflows/ci-windows.yml`): editable install with dev dependencies, then `tests/engine` and `--governance-contracts`. The workflow links the checkout at **`D:\cursor`** and prepares `data/` fixtures for tests that rely on that path.

GitHub **Re-run failed jobs** replays the **old workflow definition** from the failed run; to validate CI fixes, trigger a **new run** (push to `main` or **Actions → CI (Windows) → Run workflow**).

## Testing

Run the fast communication and V9 shadow contract suite with:

```bash
python -m pytest --fast-communication-v9-shadow-contracts
```

Or use the marker directly:

```bash
python -m pytest -m fast_contracts
```

This fast entrypoint focuses on the contract layer, including:
- session manager terminal event contract
- SSE message and client terminal payload alignment
- stable communication summary mirror fields in runtime contract tests

Run the staged communication and V9 shadow regression suite with:

```bash
python -m pytest --communication-v9-shadow-regression
```

Or use the marker directly:

```bash
python -m pytest -m staged_regression
```

This entrypoint covers the current communication and V9 shadow stability surface, including:
- stable communication summary contract projection
- communication operations / replay service chain
- session manager / SSE / client terminal payload alignment
- V9 shadow smoke, contracts, integration, and SSE utility coverage

## Governance Summary Contract

Deployment/governance outputs expose a unified governance summary shape:

- `summary.governance_focus` (normalized to `list[dict]`; non-dict entries are dropped)
- `summary.governance_warning_count` (derived from `governance_focus` warn items)

For services that emit top-level artifacts (for example release certificate and
release registry summary), the same two fields are exposed at their existing
contract level while preserving backwards compatibility.

Normalization is centralized in `core/deployment/governance_summary.py`:

- `extract_governance_summary(...)` for normalizing governance fields extracted
  from existing payloads.
- `build_governance_summary(...)` for constructing and normalizing governance
  summary fields at source emitters. The `warning_count` argument is kept only
  for backwards compatibility; normalized `governance_warning_count` is derived
  from warn items in `focus`.
- `count_governance_warnings(...)` is the single shared implementation of the
  warn count semantics, reused by the helpers and the governance contract tests.
  The module also exports a small public surface via `__all__` to keep imports
  predictable.

Recommended contract checks:

```bash
python -m pytest --governance-contracts
```

Or use the marker directly:

```bash
python -m pytest -m governance_contracts
```

