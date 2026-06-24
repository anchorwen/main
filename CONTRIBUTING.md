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

After cloning the repository, run the bootstrap script:
```bash
bash scripts/bootstrap-dev-env.sh
```
This installs pre-commit, pre-push, and commit-msg hooks, and initializes the blueprint baseline.

## Pre-commit Workflow

Before committing, run auto-fixes to prevent stash conflicts:
```bash
bash scripts/pre_commit_auto_fix.sh   # ruff check --fix + ruff format
git add -u
git commit
```

## Push Gate

Pre-push runs the same checks as CI: ruff (full codebase), mypy (baseline), omega scan.
Checks are defined in `.pre-commit-config.yaml` (stages: [pre-push]).
Emergency override: `git push --no-verify` (requires reason per Iron Law #0-bis).

## Commit Conventions

- One logical change per commit
- Message format: `<imperative verb> <subsystem> <summary>`
- Example: `Harden engine layer with runtime safety and feature source passthrough`

## CI

GitHub Actions on `windows-latest`. Workflow: `.github/workflows/ci-windows.yml`.
Pushes and PRs against `main` run the full test suite.
