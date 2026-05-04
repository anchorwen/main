# Contributing

## Setup

```bash
git clone <repo-url>
cd future
pip install -e .[dev]
```

## Tests

```bash
python -m pytest tests/ -q          # full suite (1304 tests)
python -m pytest -m fast_contracts   # communication + V9 shadow contracts
python -m pytest -m staged_regression # communication + V9 shadow regression
```

## Pre-commit Hooks

```bash
pip install pre-commit
pre-commit install
```

Hooks: ruff (lint), ruff-format (formatter), architecture-gate (roadmap auto-refresh).

## Commit Conventions

- One logical change per commit
- Message format: `<imperative verb> <subsystem> <summary>`
- Example: `Harden engine layer with runtime safety and feature source passthrough`

## CI

GitHub Actions on `windows-latest`. Workflow: `.github/workflows/ci-windows.yml`.
Pushes and PRs against `main` run the full test suite.
