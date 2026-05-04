# ADR: Replace `datetime.utcnow()` with `datetime.now(UTC).replace(tzinfo=None)`

- **Status**: Accepted
- **Date**: 2026-05-04

## Motivation

Python 3.12 deprecated `datetime.utcnow()` (PEP 706). The replacement
`datetime.now(UTC)` returns a **timezone-aware** datetime, but the codebase
consistently uses **naive** datetimes assumed to be UTC. Mixing aware and
naive datetimes causes `TypeError: can't compare offset-naive and
offset-aware datetimes` — confirmed in 55 test failures across the
dispatch, risk, and ledger subsystems.

## Decision

Replace `datetime.utcnow()` with `datetime.now(UTC).replace(tzinfo=None)`
everywhere. This preserves naive-datetime semantics (no comparison
breakage) while removing the deprecated API call.

Import: `from datetime import UTC, datetime`

## Scope

105 files across all subsystems: `core/`, `apps/engine/`, `tests/`,
`scripts/`, `conftest.py`.

## Validation

`python -m pytest tests/ -q` — 1304 passed, 0 failed.
