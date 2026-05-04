# Institution-Grade Adaptive Trading System

A greenfield build for an institution-grade adaptive quantitative trading platform.

## Live operations (MT5 / bridge)

Operated from the repo root on Windows. See [**docs/LIVE_OPS.md**](docs/LIVE_OPS.md) (anchor intent loop vs V9 ONNX, gate semantics, exit codes) and [**docs/LIVE_EXECUTION_CONTRACT.md**](docs/LIVE_EXECUTION_CONTRACT.md) (generic MT5 payload: volume, close, modify SL/TP). Run **`scripts/ops_acceptance_check.ps1`** for a single diagnostic + health pass; use **`scripts/live_stack_diagnostic.py --output ...`** for UTF-8 JSON on Chinese-locale consoles.

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

### Runtime Contract Baseline (DBAC)

The current runtime baseline treats `operations_summary` as the stable source for replay/runtime contract fields. Top-level mirror fields are projected from `operations_summary` for compatibility consumers.

Stable replay/runtime mirrors include:
- `execution_mode`
- `executed_message_ids`
- `skipped_message_ids`
- `blocked_message_ids`
- `skip_reasons`
- `block_reasons`

For runtime paths where payloads do not already carry `operations_summary`, the engine backfills from `result.communication_operations` before stable projection.

Recommended baseline guard checks:

```bash
python -m pytest tests/engine/test_runtime_contract_guard.py
python -m pytest tests/engine/test_communication_replay_executor.py::test_replay_executor_priority_contract_matrix
python -m pytest tests/engine/test_v9_shadow_smoke.py::test_v9_shadow_apply_stable_output_contract_mirrors_replay_execution_fields
```

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

