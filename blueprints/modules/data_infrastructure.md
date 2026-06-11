# data_infrastructure — Append-Only Event Stream

## Module Purpose
Immutable event stream infrastructure (FIX-20260611-021). Replaces mutable JSON read-modify-write with append-only event sourcing.

## Source Files
- `core/data/__init__.py` — Package init
- `core/data/event_writer.py` — Single-process, thread-safe, append-only EventWriter
- `core/data/projections.py` — Pure-function projection engine (event stream → governance state)
- `scripts/migration/migrate_to_event_stream.py` — One-shot brain_pnl_ledger → ledger_events.jsonl migration

## Fix History

| FIX-20260611-021 | 2026-06-11 | cursor-agent | 49610cd | Bug fixes: UUID ordering (line-based checkpoint) + checkpoint key mismatch (_ensure_brain_state). Both found by Hypothesis PBT. | contract-violation |
| FIX-20260611-021 | 2026-06-11 | cursor-agent | — | **Event Sourcing Foundation**: Append-only event stream architecture. Contracts (Pydantic extra=forbid), Unified Writer (threading.Lock), Migration (source=migration tag), Projection Engine (checkpoint + incremental replay), Dual-Write Hook (opt-in EventWriter in BrainPnLStore). | RC-06 |
