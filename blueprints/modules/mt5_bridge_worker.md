# MT5 Bridge Worker — Module Blueprint

> Auto-generated per Iron Law #6 (Pre-Fix Protocol).
> Module: `scripts/mt5_bridge_worker.py`
> Purpose: MT5 order execution bridge — consumes handoff files from outbox / ZMQ,
>          executes orders via MT5 API, writes journal entries and ACK receipts.

## Architecture

Two operational modes:
- **File-based** (`run_worker`): Polls `data/mt5_outbox/` for `*.mt5.json` files
- **ZMQ-based** (`run_zmq_bridge`): Receives orders via ZMQ PULL, with 5s file-outbox fallback (Phase 3 WAL dual-write)

Key internal pipeline: `_send_to_mt5()` → route to `_mt5_market_open()` / `_mt5_close_position()` / `_mt5_modify_sltp()` → `_write_zmq_journal_entry()` / `_append_journal()`

## DQAF-034 Hardening (FIX-20260621-041)

Three-phase fix for the MIA (Missing In Action) root cause — crash window between MT5 execution and journal write:

1. **Persisted Processed IDs**: `bridge_processed_wal.jsonl` replaces in-memory `_processed_ids` set.
   Functions: `_load_processed_ids()`, `_persist_processed_id()`, `_truncate_processed_wal()`.

2. **State Verification Gateway**: `_mt5_close_position()` now queries `history_deals_get()` when
   `positions_get()` returns empty. If exit deals exist → returns `"closed"` with recovered fill
   details instead of `"rejected"`.

3. **Resilient Sync Journal I/O**: `_append_journal()` uses exponential backoff [0,100,200,400]ms
   instead of long-blocking lock. `_merge_overflow_files()` drains overflow sidecars every 60s.

## Fix History

| FIX ID | Date | Description | Root Cause |
|--------|------|-------------|------------|
| FIX-20260621-041 | 2026-06-21 | DQAF-034 MIA Root Cause Fix — Bridge Idempotent WAL Gateway (3-Phase) | RC-04 |
| FIX-20260621-040 | 2026-06-21 | DQAF-033 P0 Addendum — close_accepted detail.reason fix | RC-07 |
| FIX-20260613-057 | 2026-06-13 | Startup Race Condition — MT5 sync barrier | RC-04 |
| FIX-20260531-016 | 2026-05-31 | Dedup guard — fingerprint cache (2s window) | RC-04 |
| FIX-20260522-004 | 2026-05-22 | Journal confidence always null | RC-06 |
| FIX-20260523-001 | 2026-05-23 | P(win) feedback loop — journal wiring | RC-12 |
| FIX-20260612-004 | 2026-06-12 | Deal history for actual fill PnL | RC-08 |

## Known Issues

- 47 historical MIA trades permanently unattributable (MT5 DEAL_REASON_CLIENT carries no comment)
- `_DEDUP_CACHE` is still in-memory (2s window, 50-entry cap) — sufficient for hot-path dedup;
  cross-restart dedup is handled by persisted `_processed_ids`
- `run_worker` (file-based mode) does not have `_processed_ids` dedup — only used in ZMQ mode in production

## Cross-Module Contracts

- **Input**: ZMQ PULL (`tcp://127.0.0.1:5558`) or file outbox (`data/mt5_outbox/*.mt5.json`)
- **Output**: ZMQ PUB ACK (`tcp://127.0.0.1:5557`), journal entries (`live_trade_journal.jsonl`),
  ACK receipts (`data/mt5_outbox_processed/.../*.ack.json`)
- **Dependents**: `live_order_sender.py` → `CommunicationDispatcher` → ZMQ/File adapter → Bridge
- **Depends on**: MT5 terminal (via `MetaTrader5` Python package), `FileLock` from `core.infrastructure.distributed_lock`
