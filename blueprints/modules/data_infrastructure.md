# data_infrastructure — Append-Only Event Stream

## Module Purpose
Immutable event stream infrastructure (FIX-20260611-021). Replaces mutable JSON read-modify-write with append-only event sourcing.

## Source Files
- `core/data/__init__.py` — Package init
- `core/data/event_writer.py` — Single-process, thread-safe, append-only EventWriter
- `core/data/projections.py` — Pure-function projection engine (event stream → governance state)
- `scripts/migration/migrate_to_event_stream.py` — One-shot brain_pnl_ledger → ledger_events.jsonl migration

## Fix History

| FIX-20260615-006 | 2026-06-15 | cursor-agent | — | **XAU/BTC L3 交叉感染: get_event_writer(base_dir) 移除默认值** — 现在调用方必须显式提供 base_dir。 | L3 — base_dir="data" 默认值 |
| FIX-20260613-067 | 2026-06-13 | cursor-agent | c992678 | FileLock Atomic Exclusive Create: replaced os.replace() (always overwrites) with os.O_CREAT|O_EXCL for true cross-process mutual exclusion. Added same-instance re-acquire guard. 23/23 tests pass. | contract-violation |
| FIX-20260611-021 | 2026-06-11 | cursor-agent | 49610cd | Bug fixes: UUID ordering (line-based checkpoint) + checkpoint key mismatch (_ensure_brain_state). Both found by Hypothesis PBT. | contract-violation |
| FIX-20260611-021 | 2026-06-11 | cursor-agent | — | **Event Sourcing Foundation**: Append-only event stream architecture. Contracts (Pydantic extra=forbid), Unified Writer (threading.Lock), Migration (source=migration tag), Projection Engine (checkpoint + incremental replay), Dual-Write Hook (opt-in EventWriter in BrainPnLStore). | RC-06 |
| FIX-20260607-145 | 2026-06-07 | cursor-agent | — | **Journal compaction: atomic prune of old rejected entries (>30d)**: `compact_journal()` in `journal_cleanup.py` with `os.replace()` atomic swap + FileLock. | RC-11 |
| FIX-20260612-014 | 2026-06-12 | cursor-agent | — | **Temp-file + atomic swap for repair_journal**: replaced write_text() overwrite with temp-file + os.replace() pattern (same as compact_journal). Lock acquired BEFORE re-reading — eliminates stale-snapshot window permanently. Consolidates duplicate removal. Closes Deferred Architecture Fix #1. | RC-03 |
