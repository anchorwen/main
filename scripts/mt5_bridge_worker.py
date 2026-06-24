"""Minimal MT5 bridge worker.

Consumes handoff files from mt5 outbox and writes receipt ack files.

Envelope.payload contract (Phase B): volume/lots, action open|close|modify_sltp — see docs/LIVE_EXECUTION_CONTRACT.md
"""

from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.protocol.live_execution_contract import (
    coerce_position_ticket,
    effective_volume,
    execution_route,
    normalize_action,
)
from core.runtime.fault_handler import (
    _MT5_TIMEOUT_SENTINEL,
    FaultLevel,
    FaultTolerantContext,
    mt5_call_with_timeout,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mt5_bridge_worker")
    parser.add_argument("--outbox-dir", default="data/mt5_outbox")
    parser.add_argument("--receipt-dir", default="data/receipts")
    parser.add_argument("--archive-dir", default="data/mt5_outbox_processed")
    parser.add_argument("--default-volume", type=float, default=0.01)
    parser.add_argument("--deviation", type=int, default=20)
    parser.add_argument("--magic", type=int, default=90001)
    parser.add_argument(
        "--default-symbol", default="XAUUSDc", help="Trading symbol for reconnect symbol_select"
    )
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--journal-path", default="data/live_trade_journal.jsonl")
    parser.add_argument("--protection-flag-path", default="data/live_dispatch_block.flag")
    parser.add_argument(
        "--mt5-terminal-path",
        default=None,
        help="MT5 terminal64.exe path (initialize once at startup)",
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--health-path",
        default=None,
        help="Explicit path for mt5_bridge_health.json (SSOT; derived from --receipt-dir if unset)",
    )
    # ── ZMQ mode ──
    parser.add_argument(
        "--zmq",
        action="store_true",
        help="Use ZeroMQ PUSH/PULL instead of file polling (sub-ms latency)",
    )
    parser.add_argument(
        "--zmq-order-endpoint",
        default="tcp://127.0.0.1:5556",
        help="ZMQ PULL endpoint for receiving orders",
    )
    parser.add_argument(
        "--zmq-ack-endpoint",
        default="tcp://127.0.0.1:5557",
        help="ZMQ PUB endpoint for broadcasting ACK receipts",
    )
    return parser


def _utc_now() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat().replace("+00:00", "Z")


def _list_pending(outbox_dir: Path) -> list[Path]:
    if not outbox_dir.exists():
        return []
    return sorted(outbox_dir.rglob("*.mt5.json"))


# ── DQAF-20260621-034 Phase 1: Persisted WAL processed-ID watermark ──
# Replaces the in-memory _processed_ids set (lost on Bridge restart) with
# an append-only JSONL file so dedup survives crashes.  File is capped at
# ~5000 lines via periodic truncation in the main loop.

_WAL_PROCESSED_MAX_LINES = 5000
_WAL_PROCESSED_TRUNCATE_KEEP = 2000


def _load_processed_ids(path: Path) -> set[str]:
    """Load persisted message IDs from an append-only JSONL file.

    Returns an empty set if the file is missing, unreadable, or contains
    only malformed lines — in-memory dedup still works for the session.
    """
    ids: set[str] = set()
    if not path.exists():
        return ids
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                mid = rec.get("msg_id", "")
                if mid:
                    ids.add(mid)
            except (json.JSONDecodeError, KeyError):
                continue
    except OSError:
        pass
    return ids


def _persist_processed_id(path: Path, msg_id: str) -> None:
    """Append a single processed message ID to the WAL watermark file.

    Best-effort: if the write fails the in-memory set still holds the ID
    for the current session.
    """
    try:
        rec = json.dumps({"msg_id": msg_id, "processed_at": _utc_now()}, ensure_ascii=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(rec + "\n")
    except OSError:
        pass


def _truncate_processed_wal(path: Path, keep_last: int = _WAL_PROCESSED_TRUNCATE_KEEP) -> None:
    """Truncate the WAL watermark file, keeping only the most recent entries.

    Called periodically so the file doesn't grow unbounded.  The in-memory
    set is the authoritative dedup source; the file is a durability fallback.
    """
    if not path.exists():
        return
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
        if len(lines) <= _WAL_PROCESSED_MAX_LINES:
            return
        kept = [ln for ln in lines[-keep_last:] if ln.strip()]
        path.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    except OSError:
        pass


def _load_message(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
        if not raw.strip():
            return None
        return json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return None


def _safe_move(src: Path, dst: Path) -> bool:
    """Move src to dst, tolerating missing source (e.g. already moved by prior run)."""
    try:
        shutil.move(str(src), str(dst))
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


# ── DQAF-20260621-034 Phase 3: Overflow merge ───────────────────────
# Periodically drains journal_overflow_*.jsonl sidecar files into the
# main journal.  Called every 60 s from the Bridge main loop so overflow
# entries are merged in near-real-time instead of waiting for daily_ops.


def _merge_overflow_files(journal_path: Path) -> int:
    """Merge all ``journal_overflow_*.jsonl`` files into the main journal.

    Reads each overflow file line-by-line, appends to the main journal
    under advisory FileLock protection to serialise with concurrent writers
    (live_cycle reconciliation, position_close_adapter, backfill scripts),
    then removes the overflow file.  Returns the number of entries merged.

    FIX-20260622-057 P0-2: Added FileLock — the original "no lock needed"
    comment was incorrect because the overflow merge writes to the SAME
    shared journal file as every other writer.  Without a lock, overflow
    entries and concurrent live_cycle reconciliation closes produce
    timestamp inversions and/or interleaved corruption.
    """
    import glob as _glob

    _overflow_pattern = str(journal_path.parent / "journal_overflow_*.jsonl")
    _overflow_files = sorted(_glob.glob(_overflow_pattern))
    if not _overflow_files:
        return 0

    from core.infrastructure.distributed_lock import FileLock

    _merge_lock = FileLock(
        "live_trade_journal",
        lock_dir=str(journal_path.parent / "locks"),
        ttl_seconds=10,
    )
    _merge_acquired = _merge_lock.acquire(blocking=True, timeout_seconds=5)
    if not _merge_acquired.acquired:
        return 0  # retry on next tick — another writer holds the lock

    try:
        _merged = 0
        for _of_path in _overflow_files:
            _of = Path(_of_path)
            try:
                _lines = _of.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            if not _lines:
                try:
                    _of.unlink()
                except OSError:
                    pass
                continue
            try:
                with journal_path.open("a", encoding="utf-8") as _main:
                    for _line in _lines:
                        if _line.strip():
                            _main.write(_line.strip() + "\n")
                            _merged += 1
                _of.unlink()
            except OSError:
                pass  # retry on next tick
    finally:
        _merge_lock.release()
    if _merged:
        print(
            json.dumps(
                {
                    "event": "journal_overflow_merged",
                    "time": _utc_now(),
                    "merged_entries": _merged,
                    "overflow_files": len(_overflow_files),
                    "recovery_source": "DQAF-20260621-034/Phase3",
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    return _merged


def _build_receipt_payload(
    *, message_id: str, ack_status: str, detail: dict[str, Any] | None = None
) -> dict[str, Any]:
    return {
        "message_id": message_id,
        "ack_status": ack_status,
        "received_at": _utc_now(),
        "detail": detail or {},
    }


def _write_receipt(
    receipt_dir: Path, *, date_key: str, target: str, message_id: str, payload: dict[str, Any]
) -> Path:
    target_dir = receipt_dir / date_key / target
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{message_id}.ack.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _append_journal(journal_path: Path, record: dict[str, Any]) -> None:
    """Append a record to the trade journal with advisory file locking.

    The lock serialises concurrent writes from the bridge worker, live_cycle
    reconciliation, and any other journal producer, eliminating the
    read-then-write race that caused duplicate entries and corruption.

    DQAF-20260621-034 Phase 3: Exponential-backoff retry (100/200/400 ms)
    instead of long-blocking lock acquisition.  If all 4 attempts fail the
    entry is written to a per-process overflow file.  A 60-second merge tick
    in the Bridge main loop periodically drains the overflow into the main
    journal — zero data loss, zero hot-path blocking.
    """
    # ── FIX-20260617-101/P0: Entry boundary assertion ──
    _action = record.get("action")
    if _action in ("open", None) and "eq_" in str(record.get("message_id", "")):
        _ctx = record.get("entry_context")
        if isinstance(_ctx, dict) and not _ctx.get("vector"):
            import logging as _logging

            _logging.getLogger("BridgeJournal").error(
                "REJECTED: open entry missing entry_context.vector — "
                "ticket=%s message_id=%s. See DLR-001.",
                record.get("position_ticket"),
                record.get("message_id"),
            )
            return

    from core.infrastructure.distributed_lock import FileLock

    journal_path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(
        "live_trade_journal", lock_dir=str(journal_path.parent / "locks"), ttl_seconds=10
    )
    # ── DQAF-20260621-034 Phase 3: exponential backoff [0, 100, 200, 400] ms ──
    _acquired = False
    for _attempt, _backoff_s in enumerate((0, 0.1, 0.2, 0.4)):
        if _attempt > 0:
            time.sleep(_backoff_s)
        result = lock.acquire(blocking=False)
        if result.acquired:
            _acquired = True
            break
    if not _acquired:
        # ── Overflow sidecar — never silently drop a journal entry ──
        overflow_path = journal_path.parent / f"journal_overflow_{os.getpid()}.jsonl"
        try:
            with overflow_path.open("a", encoding="utf-8") as _of:
                _of.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
            print(
                json.dumps(
                    {
                        "event": "journal_overflow_written",
                        "message_id": record.get("message_id", ""),
                        "overflow_path": str(overflow_path),
                        "retry_attempts": 4,
                        "action": "bridge_merge_tick will drain within 60s",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            print(
                json.dumps(
                    {
                        "event": "journal_overflow_failed",
                        "message_id": record.get("message_id", ""),
                        "overflow_path": str(overflow_path),
                        "error": "overflow write failed — journal entry LOST",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
        return
    try:
        mid = record.get("message_id", "")
        if mid and journal_path.exists():
            try:
                for line in journal_path.read_text(encoding="utf-8").splitlines():
                    if mid in line:
                        return  # duplicate, skip
            except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                pass  # journal dedup is best-effort
        with journal_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    finally:
        lock.release()


def _is_protection_active(protection_flag_path: Path) -> bool:
    return protection_flag_path.exists()


# ── Retry / resilience helpers ──

_MAX_RETRIES = 3
# FIX-20260531-016: Dedup guard — fingerprint → last_sent_time
_DEDUP_CACHE: dict[tuple, float] = {}

# MT5 retcodes that are transient (deserve a retry)
_TRANSIENT_RETCODES: set[int] = {
    10004,  # TRADE_RETCODE_REQUOTE
    10015,  # TRADE_RETCODE_INVALID_PRICE
    10016,  # TRADE_RETCODE_PRICE_CHANGED
    10018,  # TRADE_RETCODE_OFF_QUOTES
    10019,  # TRADE_RETCODE_CONNECTION
    10028,  # TRADE_RETCODE_TIMEOUT
}

# Permanent rejection reasons (no retry)
_PERMANENT_REASONS: set[str] = {
    "position_not_found",
    "position_ticket_required",
    "modify_requires_sl_or_tp",
    "invalid_symbol_or_side",
    "symbol_not_found",
    "tick_unavailable",
    "symbol_not_selected",
}


def _should_retry(retcode: int, reason: str) -> bool:
    """Decide if a rejected modify_sltp should be retried."""
    if reason in _PERMANENT_REASONS:
        return False
    if retcode in _TRANSIENT_RETCODES:
        return True
    # Generic rejection (10010) without a permanent reason — retry once
    if retcode == 10010 and not reason:
        return True
    return False


def _verify_position_exists(mt5: Any, ticket: int) -> bool:
    """Post-fill check: confirm the position actually exists after mt5.order_send()."""
    positions = None
    with FaultTolerantContext(
        level=FaultLevel.CRASH,
        component="MT5_IPC:positions_get:verify_exists",
    ):
        positions = mt5.positions_get(ticket=ticket)
    return positions is not None and len(positions) > 0


def _normalize_side(value: Any) -> str:
    text = str(value or "").lower()
    if text in {"long", "buy"}:
        return "buy"
    if text in {"short", "sell"}:
        return "sell"
    return "unknown"


def _coerce_positive_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed <= 0:
        return None
    return parsed


def _send_to_mt5(
    mt5: Any, payload: dict[str, Any], *, default_volume: float, deviation: int, magic: int
) -> tuple[str, dict[str, Any]]:
    """Execute order via already-initialized MT5 module.

    mt5.initialize() is called ONCE in run_worker(); this function should NOT
    call initialize() or shutdown() — doing so tears down the shared IPC
    connection and starves the live intent loop of tick data.
    """
    envelope = payload.get("envelope", {})
    msg_payload = envelope.get("payload", {})
    action = normalize_action(msg_payload.get("action"))
    route = execution_route(action)

    if route == "unsupported":
        return "acknowledged", {"reason": "unsupported_action", "action": action}

    if route == "market_open":
        # DQAF-20260614-010: Use strategy magic from payload, not bridge default.
        # Previously the bridge --magic arg (90401) was passed unconditionally,
        # causing ALL ZMQ orders to open under the wrong magic number.
        _strategy_magic = msg_payload.get("magic") or envelope.get("payload", {}).get("magic")
        _effective_magic = int(_strategy_magic) if _strategy_magic is not None else magic
        return _mt5_market_open(
            mt5,
            envelope=envelope,
            msg_payload=msg_payload,
            default_volume=default_volume,
            deviation=deviation,
            magic=_effective_magic,
        )
    if route == "close":
        return _mt5_close_position(
            mt5, envelope=envelope, msg_payload=msg_payload, deviation=deviation, magic=magic
        )
    if route == "modify_sltp":
        return _mt5_modify_sltp(mt5, envelope=envelope, msg_payload=msg_payload)
    return "acknowledged", {"reason": "unknown_route", "route": route}


def _mt5_market_open(
    mt5: Any,
    *,
    envelope: dict[str, Any],
    msg_payload: dict[str, Any],
    default_volume: float,
    deviation: int,
    magic: int,
) -> tuple[str, dict[str, Any]]:
    symbol = msg_payload.get("symbol")
    side = _normalize_side(msg_payload.get("side"))
    stop_loss = _coerce_positive_float(msg_payload.get("stop_loss"))
    if stop_loss is None:
        stop_loss = _coerce_positive_float(msg_payload.get("sl"))
    take_profit = _coerce_positive_float(msg_payload.get("take_profit"))
    if take_profit is None:
        take_profit = _coerce_positive_float(msg_payload.get("tp"))
    volume = effective_volume(msg_payload, default_volume=default_volume)

    # ── FIX-20260531-016: Dedup guard — prevent duplicate orders within 2s ──
    _now = time.time()
    _fingerprint = (
        symbol,
        side,
        round(volume, 4),
        round(stop_loss or 0, 2),
        round(take_profit or 0, 2),
    )
    _last = _DEDUP_CACHE.get(_fingerprint, 0.0)
    if _now - _last < 2.0:
        return "rejected", {
            "reason": "duplicate_order_dedup_guard",
            "last_sent_s": round(_now - _last, 3),
            "symbol": symbol,
            "side": side,
        }
    _DEDUP_CACHE[_fingerprint] = _now
    # Periodic cleanup: evict entries older than 10s
    if len(_DEDUP_CACHE) > 50:
        _DEDUP_CACHE.clear()

    if side == "unknown" or not symbol:
        return "rejected", {"reason": "invalid_symbol_or_side", "symbol": symbol, "side": side}
    symbol_info = mt5.symbol_info(symbol)
    if symbol_info is None:
        return "rejected", {"reason": "symbol_not_found", "symbol": symbol}
    if not symbol_info.visible:
        mt5.symbol_select(symbol, True)
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return "rejected", {"reason": "tick_unavailable", "symbol": symbol}
    price = tick.ask if side == "buy" else tick.bid
    order_type = mt5.ORDER_TYPE_BUY if side == "buy" else mt5.ORDER_TYPE_SELL
    comment = str(msg_payload.get("message_id", ""))[:31]
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": float(volume),
        "type": order_type,
        "price": float(price),
        "deviation": int(deviation),
        "magic": int(magic),
        "comment": comment,
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    if stop_loss is not None:
        request["sl"] = stop_loss
    if take_profit is not None:
        request["tp"] = take_profit
    result = mt5.order_send(request)
    if result is None:
        return "rejected", {"reason": "order_send_failed", "last_error": mt5.last_error()}
    retcode = int(getattr(result, "retcode", -1))
    done_code = int(getattr(mt5, "TRADE_RETCODE_DONE", 10009))
    if retcode == done_code:
        # Spin-wait for MT5 Positions Pool sync (陷阱一)
        # order_send is async — local DB may lag 100-500ms
        order_ticket = getattr(result, "order", None)
        confirmed_sl, confirmed_tp = 0.0, 0.0
        if order_ticket:
            for _ in range(5):
                pos = mt5.positions_get(ticket=order_ticket)
                if pos and len(pos) > 0 and (pos[0].sl > 0 or pos[0].tp > 0):
                    confirmed_sl = float(pos[0].sl)
                    confirmed_tp = float(pos[0].tp)
                    break
                time.sleep(0.1)
        return "accepted", {
            "retcode": retcode,
            "order": order_ticket,
            "request": request,
            "confirmed_sl": confirmed_sl,
            "confirmed_tp": confirmed_tp,
        }
    return "rejected", {
        "retcode": retcode,
        "comment": getattr(result, "comment", None),
        "request": request,
    }


def _mt5_close_position(
    mt5: Any,
    *,
    envelope: dict[str, Any],
    msg_payload: dict[str, Any],
    deviation: int,
    magic: int,
) -> tuple[str, dict[str, Any]]:
    ticket = coerce_position_ticket(msg_payload)
    if ticket is None:
        return "rejected", {"reason": "position_ticket_required"}

    positions = mt5.positions_get(ticket=ticket)
    if not positions:
        # ── DQAF-20260621-034 Phase 2: Idempotent state verification ──
        # Position not in MT5 live portfolio.  Two possibilities:
        #   a) Crash AFTER close execution but BEFORE journal write
        #      → recover fill details from deal history (ghost-close repair)
        #   b) Genuinely invalid ticket → reject
        # Query deal history to disambiguate.  If exit deals exist the
        # position was already closed — recover fill without re-executing,
        # preventing both MIA and "position not found" noise.
        try:
            deals = mt5.history_deals_get(position=ticket)
        except Exception:
            deals = None
        if deals:
            exit_deals = [d for d in deals if getattr(d, "entry", -1) == 1]
            if exit_deals:
                last_exit = max(exit_deals, key=lambda d: getattr(d, "time", 0))
                _rec_price = getattr(last_exit, "price", None)
                _rec_profit = getattr(last_exit, "profit", None)
                _rec_volume = getattr(last_exit, "volume", None)
                _rec_reason = getattr(last_exit, "reason", -1)
                _rec_pos_id = getattr(last_exit, "position_id", ticket)
                print(
                    json.dumps(
                        {
                            "event": "close_position_already_closed_recovered",
                            "time": _utc_now(),
                            "ticket": ticket,
                            "close_price": float(_rec_price) if _rec_price else None,
                            "profit": float(_rec_profit) if _rec_profit else None,
                            "deal_reason": (
                                int(_rec_reason)
                                if _rec_reason is not None and int(_rec_reason) >= 0
                                else -1
                            ),
                            "recovery_source": "DQAF-20260621-034/Phase2",
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                return "closed", {
                    "reason": "position_already_closed_recovered",
                    "ticket": ticket,
                    "close_price": float(_rec_price) if _rec_price is not None else None,
                    "profit": float(_rec_profit) if _rec_profit is not None else None,
                    "fill_volume": float(_rec_volume) if _rec_volume is not None else None,
                    "deal_reason": (
                        int(_rec_reason)
                        if _rec_reason is not None and int(_rec_reason) >= 0
                        else -1
                    ),
                    "position_identifier": _rec_pos_id,
                    "recovery_source": "DQAF-20260621-034/Phase2",
                }
        return "rejected", {"reason": "position_not_found", "ticket": ticket}

    pos = positions[0]
    symbol = pos.symbol
    pos_vol = float(getattr(pos, "volume", 0.0))
    pos_identifier = getattr(pos, "identifier", ticket)  # 陷阱二: immutable anchor
    req_vol = _coerce_positive_float(msg_payload.get("volume"))
    close_vol = min(req_vol, pos_vol) if req_vol is not None else pos_vol
    if close_vol <= 0 or close_vol > pos_vol + 1e-9:
        return "rejected", {
            "reason": "invalid_close_volume",
            "position_volume": pos_vol,
            "requested": req_vol,
        }

    if mt5.symbol_info(symbol) is None:
        return "rejected", {"reason": "symbol_not_found", "symbol": symbol}
    tick = mt5.symbol_info_tick(symbol)
    if tick is None:
        return "rejected", {"reason": "tick_unavailable", "symbol": symbol}

    pos_type = int(getattr(pos, "type", -1))
    order_type = (
        mt5.ORDER_TYPE_SELL
        if pos_type == int(getattr(mt5, "POSITION_TYPE_BUY", 0))
        else mt5.ORDER_TYPE_BUY
    )
    price = float(tick.bid if order_type == mt5.ORDER_TYPE_SELL else tick.ask)

    close_comment = str(msg_payload.get("message_id", ""))[:31]
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": symbol,
        "volume": close_vol,
        "type": order_type,
        "position": ticket,
        "price": price,
        "deviation": int(deviation),
        "magic": int(magic),
        "comment": close_comment,
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    result = mt5.order_send(request)
    if result is None:
        return "rejected", {"reason": "order_send_failed", "last_error": mt5.last_error()}
    retcode = int(getattr(result, "retcode", -1))
    done_code = int(getattr(mt5, "TRADE_RETCODE_DONE", 10009))
    if retcode == done_code:
        detail: dict[str, Any] = {
            "retcode": retcode,
            "order": getattr(result, "order", None),
            "request": request,
            "position_identifier": pos_identifier,
        }
        # ── FIX-20260612-004 + FIX-20260613-077: Deal history for actual fill PnL ──
        # After a successful close, query MT5 deal history to capture the
        # actual fill price.  1s timeout prevents blocking the bridge loop.
        # When this succeeds, reconciliation can skip writing a duplicate close.
        try:
            import time as _time

            _deal_start = _time.time()
            deals = mt5.history_deals_get(position=ticket)
            _deal_elapsed = _time.time() - _deal_start
            if _deal_elapsed > 0.5:
                print(
                    json.dumps(
                        {
                            "event": "bridge_deal_history_slow",
                            "ticket": ticket,
                            "elapsed_s": round(_deal_elapsed, 3),
                            "time": _utc_now(),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            if deals:
                exit_deals = [d for d in deals if getattr(d, "entry", -1) == 1]
                if exit_deals:
                    last_exit = max(exit_deals, key=lambda d: getattr(d, "time", 0))
                    _fill_price = getattr(last_exit, "price", None)
                    _fill_profit = getattr(last_exit, "profit", None)
                    _fill_volume = getattr(last_exit, "volume", None)
                    if _fill_price is not None and float(_fill_price) > 0:
                        detail["close_price"] = float(_fill_price)
                    if _fill_profit is not None:
                        detail["profit"] = float(_fill_profit)
                    if _fill_volume is not None:
                        detail["fill_volume"] = float(_fill_volume)
                    # DQAF-20260621-033: capture MT5 deal reason for
                    # downstream audit and reconciliation alignment.
                    _deal_reason = getattr(last_exit, "reason", -1)
                    if _deal_reason is not None and int(_deal_reason) >= 0:
                        detail["deal_reason"] = int(_deal_reason)
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            pass  # Non-blocking: estimated PnL survives as fallback
        # 陷阱二: Partial close creates new ticket — capture via identifier
        if close_vol < pos_vol - 1e-9:
            for _ in range(5):
                remaining = mt5.positions_get(symbol=symbol)
                if remaining:
                    for rp in remaining:
                        if (
                            getattr(rp, "identifier", None) == pos_identifier
                            and rp.ticket != ticket
                        ):
                            detail["new_ticket"] = rp.ticket
                            detail["old_ticket"] = ticket
                            break
                if "new_ticket" in detail:
                    break
                time.sleep(0.1)
        return "accepted", detail
    return "rejected", {
        "retcode": retcode,
        "comment": getattr(result, "comment", None),
        "request": request,
    }


def _mt5_modify_sltp(
    mt5: Any, *, envelope: dict[str, Any], msg_payload: dict[str, Any]
) -> tuple[str, dict[str, Any]]:
    ticket = coerce_position_ticket(msg_payload)
    if ticket is None:
        return "rejected", {"reason": "position_ticket_required"}

    positions = mt5.positions_get(ticket=ticket)
    if not positions:
        return "rejected", {"reason": "position_not_found", "ticket": ticket}

    pos = positions[0]
    symbol = pos.symbol
    sl = _coerce_positive_float(msg_payload.get("stop_loss"))
    if sl is None:
        sl = _coerce_positive_float(msg_payload.get("sl"))
    tp = _coerce_positive_float(msg_payload.get("take_profit"))
    if tp is None:
        tp = _coerce_positive_float(msg_payload.get("tp"))

    if sl is None and tp is None:
        return "rejected", {"reason": "modify_requires_sl_or_tp"}

    request: dict[str, Any] = {
        "action": mt5.TRADE_ACTION_SLTP,
        "symbol": symbol,
        "position": ticket,
    }
    if sl is not None:
        request["sl"] = sl
    if tp is not None:
        request["tp"] = tp

    result = mt5.order_send(request)
    if result is None:
        return "rejected", {"reason": "order_send_failed", "last_error": mt5.last_error()}
    retcode = int(getattr(result, "retcode", -1))
    done_code = int(getattr(mt5, "TRADE_RETCODE_DONE", 10009))
    if retcode == done_code:
        return "accepted", {"retcode": retcode, "request": request}
    return "rejected", {
        "retcode": retcode,
        "comment": getattr(result, "comment", None),
        "request": request,
    }


def _derive_label(action: str, msg_payload: dict[str, Any], detail: dict[str, Any]) -> str | None:
    """Derive a human-readable label from the action/payload/detail for journal entries."""
    if action == "open":
        return None
    if action == "modify_sltp":
        return "trail" if msg_payload.get("sl") else None
    if action == "close":
        # Label by PnL outcome (win/loss), NOT by close reason.
        # The close reason stays in the `comment` field for audit + restart
        # bootstrap classification.  Previously the comment was used as the
        # label, which leaked "exit_watchdog:..." into the label field and
        # caused the bootstrap to skip the entry (non-standard label).
        pnl = msg_payload.get("pnl")
        if pnl is not None:
            try:
                return "win" if float(pnl) > 0 else ("loss" if float(pnl) < 0 else "breakeven")
            except (ValueError, TypeError):
                pass
        if isinstance(detail, dict):
            retcode = detail.get("retcode")
            if retcode and retcode != 10009:
                return f"close_failed_rc{retcode}"
        return "close_accepted"
    return None


def process_one(
    message_path: Path,
    *,
    outbox_dir: Path,
    receipt_dir: Path,
    archive_dir: Path,
    journal_path: Path,
    protection_flag_path: Path,
    default_volume: float,
    deviation: int,
    magic: int,
    dry_run: bool,
    mt5: Any = None,
) -> dict[str, Any]:
    payload = _load_message(message_path)
    if payload is None:
        # File missing, empty, or invalid JSON — skip silently
        return {
            "message_id": message_path.stem,
            "ack_status": "skipped",
            "reason": "file_missing_or_invalid",
        }
    envelope = payload.get("envelope", {})
    msg_payload = envelope.get("payload", {})
    message_id = str(envelope.get("message_id", message_path.stem))
    target = str(envelope.get("target", "exec_bridge"))
    date_key = (
        message_path.parent.parent.name
        if len(message_path.parents) >= 2
        else datetime.now().date().isoformat()
    )

    if _is_protection_active(protection_flag_path):
        ack_status = "rejected"
        detail = {"reason": "protection_guard_active"}
    elif dry_run:
        ack_status = "acknowledged"
        detail = {"reason": "dry_run"}
    else:
        order_magic = int(msg_payload.get("magic", magic))
        if mt5 is None:
            ack_status = "rejected"
            detail = {"reason": "mt5_module_unavailable_no_terminal_path"}
        else:
            ack_status, detail = _send_to_mt5(
                mt5, payload, default_volume=default_volume, deviation=deviation, magic=order_magic
            )

    # order_magic must be resolved for all code paths (dry_run/protection guard use default)
    if "order_magic" not in dir():
        order_magic = int(msg_payload.get("magic", magic))

    action = normalize_action(msg_payload.get("action"))
    retcode = int(detail.get("retcode", 0)) if isinstance(detail, dict) else 0
    reject_reason = str(detail.get("reason", "")) if isinstance(detail, dict) else ""

    # ── Retry: transient modify_sltp failure → requeue ──
    retry_count = int(msg_payload.get("_retry_count", 0))
    if (
        action == "modify_sltp"
        and ack_status == "rejected"
        and _should_retry(retcode, reject_reason)
        and retry_count < _MAX_RETRIES
    ):
        msg_payload["_retry_count"] = retry_count + 1
        retry_msg_path = outbox_dir / f"{message_id}.mt5.json"
        retry_msg_path.parent.mkdir(parents=True, exist_ok=True)
        retry_msg_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        # Archive the old message so it doesn't pile up
        rel_path = (
            message_path.relative_to(outbox_dir)
            if message_path.is_relative_to(outbox_dir)
            else Path(message_path.name)
        )
        archive_path = archive_dir / rel_path
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        _safe_move(message_path, archive_path)
        return {
            "message_id": message_id,
            "ack_status": "retrying",
            "retry_count": retry_count + 1,
            "archive_path": str(archive_path),
        }

    # ── Post-fill verification: market_open accepted → verify position ──
    if action == "open" and ack_status == "accepted":
        ticket = detail.get("order") if isinstance(detail, dict) else None
        if ticket and mt5 is not None:
            if not _verify_position_exists(mt5, int(ticket)):
                ack_status = "rejected"
                _fallback_retcode: Any = retcode
                detail = {
                    "reason": "post_fill_position_not_found",
                    "order": ticket,
                    "retcode": _fallback_retcode,
                }

    receipt_payload = _build_receipt_payload(
        message_id=message_id, ack_status=ack_status, detail=detail
    )
    receipt_path = _write_receipt(
        receipt_dir,
        date_key=date_key,
        target=target,
        message_id=message_id,
        payload=receipt_payload,
    )

    rel_path = (
        message_path.relative_to(outbox_dir)
        if message_path.is_relative_to(outbox_dir)
        else Path(message_path.name)
    )
    archive_path = archive_dir / rel_path
    archive_path.parent.mkdir(parents=True, exist_ok=True)
    _safe_move(message_path, archive_path)

    vol_disp = msg_payload.get("volume", msg_payload.get("lots"))
    _comment = msg_payload.get("comment", "")
    _label = _derive_label(action, msg_payload, detail)
    # ── DQAF-033 P0: Inject close_reason into detail so audit dashboards
    # can attribute every close.  The comment already flows from
    # dispatch_managed_close(reason=...) → payload["comment"].  This
    # copies it into detail.reason for downstream consumers (journal
    # queries, audit scripts, MIA forensics).
    if action == "close" and _comment and isinstance(detail, dict) and "reason" not in detail:
        detail = {**detail, "reason": _comment}
    _magic = order_magic
    _strategy = ""
    try:  # BLE001:FOG (was: FOG/LAC)
        from core.contracts.strategy_magic import MAGIC_TO_STRATEGY

        _strategy = MAGIC_TO_STRATEGY.get(_magic, "")
    except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
        pass
    _open_msg_id = msg_payload.get("open_message_id", "")
    position_ticket = detail.get("order") or coerce_position_ticket(msg_payload)
    # ── FIX-20260612-004: Prefer actual fill PnL over mid-price estimate ──
    _actual_profit = detail.get("profit") if isinstance(detail, dict) else None
    _pnl = _actual_profit if _actual_profit is not None else msg_payload.get("pnl")
    journal_record = {
        "schema_version": "live_trade_journal.v2",
        "recorded_at": _utc_now(),
        "message_id": message_id,
        "target": target,
        "ack_status": ack_status,
        "detail": detail,
        "symbol": msg_payload.get("symbol"),
        "action": action,
        "side": msg_payload.get("side"),
        "volume": vol_disp,
        "pnl": _pnl,
        "label": _label,
        "comment": _comment,
        "magic": _magic,
        "strategy": _strategy,
        "effective_volume_hint": effective_volume(msg_payload, default_volume=default_volume),
        "position_ticket": position_ticket,
        "position_identifier": detail.get("position_identifier", position_ticket)
        if isinstance(detail, dict)
        else position_ticket,
        "execution_payload_schema": msg_payload.get("execution_payload_schema"),
        "sl": msg_payload.get("sl", msg_payload.get("stop_loss")),
        "tp": msg_payload.get("tp", msg_payload.get("take_profit")),
        "outbox_path": str(message_path),
        "archive_path": str(archive_path),
        "receipt_path": str(receipt_path),
        "brain_ids": msg_payload.get("brain_ids"),
        "brain_votes": msg_payload.get("brain_votes"),
        "confidence": msg_payload.get("confidence"),
        "p_win": msg_payload.get("p_win"),
        "p_win_source": msg_payload.get("p_win_source", "unknown"),
        "p_win_degraded": msg_payload.get("p_win_degraded", False),
        "kelly_mult": msg_payload.get("kelly_mult"),
        "entry_context": msg_payload.get("entry_context"),
    }
    if _open_msg_id:
        journal_record["open_message_id"] = _open_msg_id
    _append_journal(journal_path, journal_record)

    result = {
        "message_id": message_id,
        "ack_status": ack_status,
        "receipt_path": str(receipt_path),
        "archive_path": str(receipt_path),
    }
    return result


# ── Heartbeat + reconnection ───────────────────────────────────────────

_HEARTBEAT_INTERVAL = 30.0
_MAX_RECONNECT_ATTEMPTS = 5
_RECONNECT_BACKOFF_SEQUENCE = [1.0, 2.0, 4.0, 8.0, 16.0, 30.0]


def _check_mt5_heartbeat(mt5: Any) -> bool:
    """Return True if MT5 terminal responds to terminal_info()."""
    try:
        info = mt5.terminal_info()
        return info is not None
    except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
        return False


def _reconnect_mt5(mt5_module: Any, terminal_path: str, *, symbol: str = "XAUUSDc") -> bool:
    """Reconnect to MT5 with exponential backoff.  Returns True on success.

    symbol is a keyword-only parameter for the post-reconnect symbol_select.
    """
    for attempt, delay in enumerate(_RECONNECT_BACKOFF_SEQUENCE, start=1):
        print(
            json.dumps(
                {
                    "event": "bridge_mt5_reconnect_attempt",
                    "attempt": attempt,
                    "delay": delay,
                    "time": _utc_now(),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
        time.sleep(delay + random.uniform(0, 1.0))  # jitter: break rate-limit sync
        try:  # BLE001:FOG (was: FOG/LAC)
            mt5_module.shutdown()
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            pass
        try:
            if mt5_module.initialize(path=terminal_path):
                print(
                    json.dumps(
                        {
                            "event": "bridge_mt5_reconnect_success",
                            "attempt": attempt,
                            "time": _utc_now(),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
                # Re-select symbol after reconnect (config-driven, not hardcoded)
                try:  # BLE001:FOG (was: FOG/LAC)
                    mt5_module.symbol_select(symbol, True)
                except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                    pass
                return True
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            pass
    return False


def run_worker(args: argparse.Namespace) -> int:
    outbox_dir = Path(args.outbox_dir)
    receipt_dir = Path(args.receipt_dir)
    archive_dir = Path(args.archive_dir)
    journal_path = Path(args.journal_path)
    protection_flag_path = Path(args.protection_flag_path)

    # ── Initialize MT5 once at startup ──
    mt5 = None
    terminal_path = args.mt5_terminal_path
    if terminal_path:
        try:
            import MetaTrader5 as _mt5

            if _mt5.initialize(path=str(terminal_path)):
                mt5 = _mt5
                print(f"[bridge] MT5 initialized: {terminal_path}", flush=True)
            else:
                print(f"[bridge] WARN: MT5 init failed: {_mt5.last_error()}", flush=True)
        except (RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:  # BLE001:FOG
            print(f"[bridge] WARN: MT5 unavailable: {exc}", flush=True)
    health_path = (
        Path(args.health_path)
        if args.health_path
        else Path(args.receipt_dir).parent / "reports" / "mt5_bridge_health.json"
    )
    health_path.parent.mkdir(parents=True, exist_ok=True)
    _last_health_write = 0.0
    _last_heartbeat_check = 0.0
    _last_overflow_merge = 0.0
    _consecutive_hb_failures = 0

    # ── FIX-20260616-099: OFI TickPoller → atomic IPC file ──
    # Bridge subprocess polls ticks → OFI collector → atomic write to
    # ofi_snapshot.json.  Live cycle reads this file in Feature Lake.
    _ofi_collector = None
    _ofi_path = health_path.parent / "ofi_snapshot.json"
    if mt5 is not None and args.default_symbol:
        import threading as _thr2

        from core.features.ofi_collector import OFICollector

        _ofi_collector = OFICollector()

        def _ofi_poller() -> None:
            _last_msc = 0
            _sym = str(args.default_symbol)
            print(f"[bridge] OFI TickPoller started (symbol={_sym}, 1s)", flush=True)
            _ev = _thr2.Event()
            while True:
                try:
                    _t = mt5.symbol_info_tick(_sym)
                    if _t is not None:
                        _msc = int(getattr(_t, "time_msc", 0) or 0)
                        if _msc > _last_msc:
                            _last_msc = _msc
                            _ofi_collector.on_tick(
                                price=float(getattr(_t, "last", 0) or 0),
                                bid=float(getattr(_t, "bid", 0) or 0),
                                ask=float(getattr(_t, "ask", 0) or 0),
                                volume=float(getattr(_t, "volume", 0) or 0),
                            )
                except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                    pass
                _ev.wait(1.0)

        _thr2.Thread(target=_ofi_poller, daemon=True, name="ofi-tick-poller").start()

    _last_ofi_write = 0.0

    try:
        while True:
            processed = []
            for path in _list_pending(outbox_dir):
                try:
                    processed.append(
                        process_one(
                            path,
                            outbox_dir=outbox_dir,
                            receipt_dir=receipt_dir,
                            archive_dir=archive_dir,
                            journal_path=journal_path,
                            protection_flag_path=protection_flag_path,
                            default_volume=args.default_volume,
                            deviation=args.deviation,
                            magic=args.magic,
                            dry_run=bool(args.dry_run),
                            mt5=mt5,
                        )
                    )
                except (
                    RuntimeError,
                    ValueError,
                    KeyError,
                    TypeError,
                    OSError,
                ) as exc:  # BLE001:FOG
                    processed.append(
                        {
                            "message_id": path.stem,
                            "ack_status": "error",
                            "reason": f"{type(exc).__name__}: {str(exc)[:200]}",
                        }
                    )
            if processed:
                print(json.dumps({"processed": processed}, ensure_ascii=False, default=str))

            # ── DQAF-20260621-034 Phase 3: Overflow merge tick ─────────
            _now = time.time()
            if _now - _last_overflow_merge > 60:
                _merge_overflow_files(journal_path)
                _last_overflow_merge = _now

            # ── Periodic health heartbeat (StateWriter gate, DQAF-046 Plan B) ──
            if _now - _last_health_write > 30:
                _hb = {
                    "last_heartbeat_utc": datetime.now(UTC).replace(tzinfo=None).isoformat(),
                    "pid": os.getpid(),
                    "mt5_connected": mt5 is not None,
                    "outbox_pending": len(_list_pending(outbox_dir)),
                }
                try:
                    from core.state.catalog import lookup
                    from core.state.writer import StateWriter

                    _w = StateWriter.from_state_path(health_path)
                    _w.write_artifact(lookup("MT5_BRIDGE_HEALTH"), _w._symbol, _hb)
                except OSError as _he:
                    print(
                        json.dumps(
                            {
                                "event": "bridge_health_write_failed",
                                "error": str(_he)[:200],
                                "health_path": str(health_path),
                                "time": _utc_now(),
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
                _last_health_write = _now

                # ── FIX-20260616-099: OFI snapshot → atomic IPC file ──
                if _ofi_collector is not None:
                    try:
                        _ofi_data = _ofi_collector.settle_m5_bar()
                        if _ofi_data:
                            _ofi_tmp = _ofi_path.with_suffix(".tmp")
                            _ofi_tmp.write_text(
                                json.dumps(_ofi_data, ensure_ascii=False), encoding="utf-8"
                            )
                            os.replace(str(_ofi_tmp), str(_ofi_path))
                    except OSError:
                        pass  # OFI best-effort

            # ── MT5 heartbeat + exponential backoff reconnect ──
            if _now - _last_heartbeat_check > _HEARTBEAT_INTERVAL:
                _last_heartbeat_check = _now
                if mt5 is not None:
                    if _check_mt5_heartbeat(mt5):
                        _consecutive_hb_failures = 0
                    else:
                        _consecutive_hb_failures += 1
                        print(
                            json.dumps(
                                {
                                    "event": "bridge_mt5_heartbeat_lost",
                                    "consecutive_failures": _consecutive_hb_failures,
                                    "time": _utc_now(),
                                },
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )
                        if _consecutive_hb_failures >= _MAX_RECONNECT_ATTEMPTS:
                            print(
                                json.dumps(
                                    {
                                        "event": "bridge_mt5_fatal",
                                        "consecutive_failures": _consecutive_hb_failures,
                                        "action": "exiting",
                                        "time": _utc_now(),
                                    },
                                    ensure_ascii=False,
                                ),
                                flush=True,
                            )
                            sys.exit(1)
                        terminal_path = args.mt5_terminal_path
                        if terminal_path:
                            if _reconnect_mt5(mt5, str(terminal_path), symbol=args.default_symbol):
                                _consecutive_hb_failures = 0
                            else:
                                print(
                                    json.dumps(
                                        {
                                            "event": "bridge_mt5_reconnect_failed",
                                            "consecutive_failures": _consecutive_hb_failures,
                                            "time": _utc_now(),
                                        },
                                        ensure_ascii=False,
                                    ),
                                    flush=True,
                                )

            if args.once:
                return 0
            time.sleep(args.poll_seconds)
    finally:
        if mt5 is not None:
            try:  # BLE001:FOG (was: FOG/LAC)
                mt5.shutdown()
                print("[bridge] MT5 shutdown", flush=True)
            except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                pass


# ═══════════════════════════════════════════════════════════════════════════════
# ZMQ Bridge Worker
# ═══════════════════════════════════════════════════════════════════════════════


def _write_zmq_health(health_path: Path, mt5: Any, order_endpoint: str) -> None:
    """Write bridge health heartbeat indicating ZMQ transport mode."""
    _hb = {
        "last_heartbeat_utc": _utc_now(),
        "pid": os.getpid(),
        "mt5_connected": mt5 is not None,
        "outbox_pending": 0,
        "transport": "zmq",
        "order_endpoint": order_endpoint,
    }
    try:
        from core.state.catalog import lookup
        from core.state.writer import StateWriter

        _w = StateWriter.from_state_path(health_path)
        _w.write_artifact(lookup("MT5_BRIDGE_HEALTH"), _w._symbol, _hb)
    except OSError:
        pass  # health file is best-effort; main loop continues


def _zmq_send_ack(pub: Any, message_id: str, ack: dict[str, Any]) -> None:
    """Publish an ACK receipt via ZMQ PUB socket."""
    ack["message_id"] = message_id
    pub.send_string(f"ack {json.dumps(ack, ensure_ascii=False)}")


def _write_zmq_journal_entry(
    *,
    journal_path: Path,
    message_id: str,
    msg_payload: dict[str, Any],
    action: str,
    ack_status: str,
    detail: Any,
    default_volume: float = 0.01,
    target: str = "exec_bridge",
    mt5: Any = None,
) -> None:
    """Build and write a journal entry for a ZMQ-processed order.

    FIX-20260613-062: The ZMQ worker path was missing journal writes entirely.
    This function builds the same journal record as the file-mode path.

    DQAF-20260622-059 / P1-a: When *mt5* is provided and strategy resolution
    from the payload fails, this function falls back to querying MT5 for the
    position's original magic number (via positions_get or history_deals_get).
    """
    _magic = msg_payload.get("magic")
    if isinstance(_magic, int):
        from core.contracts.strategy_magic import MAGIC_TO_STRATEGY as _M2S

        _strategy = _M2S.get(_magic, "")
    else:
        _strategy = ""
    # DQAF-20260614-009: For open orders, the ticket is in MT5's response
    # (detail["order"]), NOT in the request (msg_payload).  coerce_position_ticket
    # only checks the request, so open orders always got ticket=None.
    position_ticket = coerce_position_ticket(msg_payload)
    if position_ticket is None and isinstance(detail, dict):
        import contextlib

        _order_ticket = detail.get("order")
        if _order_ticket is not None:
            with contextlib.suppress(TypeError, ValueError):
                position_ticket = int(_order_ticket)

    # ── DQAF-20260622-059 / P1-a: MT5 position magic fallback ──
    # If strategy is still empty after payload magic lookup, attempt
    # reverse-lookup from MT5.  Tier 1: positions_get (works for open/modify).
    # Tier 2: history_deals_get (works for recently-closed positions).
    #
    # DQAF-20260622-059 / P1: Both MT5 calls are wrapped with
    # mt5_call_with_timeout (default 5s) to prevent the journal write path
    # from blocking indefinitely on MT5 IPC hangs.  On timeout, the fallback
    # is skipped — the journal entry is written with empty strategy, which
    # is acceptable for this cold-backup path (Step 1 payload magic is the
    # primary attribution mechanism).
    _FALLBACK_TIMEOUT = 3.0  # seconds — shorter than default since this is a cold backup
    if not _strategy and mt5 is not None and position_ticket is not None:
        _fallback_magic: int | None = None
        try:  # BLE001:FOG (was: FOG/LAC)
            # Tier 1: live position lookup (timeout-guarded)
            _live_positions = mt5_call_with_timeout(
                mt5.positions_get,
                ticket=position_ticket,
                timeout=_FALLBACK_TIMEOUT,
            )
            if _live_positions is not _MT5_TIMEOUT_SENTINEL and _live_positions:
                try:
                    if len(_live_positions) > 0:
                        _fb_magic = getattr(_live_positions[0], "magic", None)
                        if _fb_magic is not None:
                            _fallback_magic = int(_fb_magic)
                except Exception:
                    pass
            # Tier 2: deal history — position already closed (timeout-guarded)
            if _fallback_magic is None:
                _deals = mt5_call_with_timeout(
                    mt5.history_deals_get,
                    position=position_ticket,
                    timeout=_FALLBACK_TIMEOUT,
                )
                if _deals is not _MT5_TIMEOUT_SENTINEL and _deals:
                    try:
                        for _d in _deals:
                            _d_magic = getattr(_d, "magic", None)
                            if _d_magic is not None and int(_d_magic) != 0:
                                _fallback_magic = int(_d_magic)
                                break
                    except Exception:
                        pass
            if _fallback_magic is not None:
                _strategy = _M2S.get(_fallback_magic, "") if "_M2S" in dir() else ""
                if not _strategy:
                    from core.contracts.strategy_magic import MAGIC_TO_STRATEGY as _M2S_FB

                    _strategy = _M2S_FB.get(_fallback_magic, "")
                if _strategy:
                    print(
                        json.dumps(
                            {
                                "event": "strategy_resolved_via_mt5_fallback",
                                "time": _utc_now(),
                                "ticket": position_ticket,
                                "fallback_magic": _fallback_magic,
                                "resolved_strategy": _strategy,
                                "source": "DQAF-20260622-059/P1-a",
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            pass
    _label = _derive_label(action, msg_payload, detail) if detail else None
    _actual_profit = detail.get("profit") if isinstance(detail, dict) else None
    _pnl = _actual_profit if _actual_profit is not None else msg_payload.get("pnl")
    vol_disp = detail.get("volume") if isinstance(detail, dict) else None
    if vol_disp is None:
        vol_disp = msg_payload.get("volume") or default_volume

    record: dict[str, Any] = {
        "schema_version": "live_trade_journal.v2",
        "recorded_at": _utc_now(),
        "message_id": message_id,
        "target": target,
        "ack_status": ack_status,
        "detail": detail,
        "symbol": msg_payload.get("symbol"),
        "action": action,
        "side": msg_payload.get("side"),
        "volume": vol_disp,
        "pnl": _pnl,
        "label": _label,
        "comment": msg_payload.get("comment", ""),
        "magic": _magic,
        "strategy": _strategy,
        "effective_volume_hint": effective_volume(msg_payload, default_volume=default_volume),
        "position_ticket": position_ticket,
        "position_identifier": detail.get("position_identifier", position_ticket)
        if isinstance(detail, dict)
        else position_ticket,
        "execution_payload_schema": msg_payload.get("execution_payload_schema"),
        "sl": msg_payload.get("sl", msg_payload.get("stop_loss")),
        "tp": msg_payload.get("tp", msg_payload.get("take_profit")),
        "outbox_path": "",  # ZMQ mode — no file
        "archive_path": "",  # ZMQ mode — no file
        "receipt_path": "",  # ZMQ mode — no file
        "brain_ids": msg_payload.get("brain_ids"),
        "brain_votes": msg_payload.get("brain_votes"),
        "confidence": msg_payload.get("confidence"),
        "p_win": msg_payload.get("p_win"),
        "p_win_source": msg_payload.get("p_win_source", "unknown"),
        "p_win_degraded": msg_payload.get("p_win_degraded", False),
        "kelly_mult": msg_payload.get("kelly_mult"),
        "entry_context": msg_payload.get("entry_context"),
    }
    _append_journal(journal_path, record)


def run_zmq_worker(
    *,
    order_endpoint: str = "tcp://127.0.0.1:5556",
    ack_endpoint: str = "tcp://127.0.0.1:5557",
    receipt_dir: Path = Path("data/receipts"),
    journal_path: Path = Path("data/live_trade_journal.jsonl"),
    protection_flag_path: Path = Path("data/live_dispatch_block.flag"),
    default_volume: float = 0.01,
    deviation: int = 20,
    magic: int = 90001,
    dry_run: bool = False,
    mt5: Any = None,
    terminal_path: str = "",
    default_symbol: str = "XAUUSDc",
    health_path: Path = Path("data/reports/mt5_bridge_health.json"),
    once: bool = False,
    # ── Phase 3: WAL dual-write — file outbox as durability fallback ──
    outbox_dir: Path = Path("data/mt5_outbox"),
    archive_dir: Path = Path("data/mt5_outbox_processed"),
) -> int:
    """Run the bridge worker in ZeroMQ mode.

    Phase 3 (DQAF-20260615-010/Phase3): Non-blocking ZMQ poll (1s timeout)
    with a 5-second file-outbox fallback scan.  This closes the durability
    gap: if the dispatcher's WAL write succeeded but ZMQ PUSH failed (crash,
    network, breaker), the bridge finds the orphaned outbox file and
    processes it.  Message-level dedup prevents double-execution when both
    ZMQ and file deliver the same order.

    ZMQ_PULL delivers orders from ZMQCommunicationAdapter.
    ZMQ_PUB publishes ACK receipts for all SUB consumers.
    """
    import zmq

    ctx = zmq.Context.instance()  # type: ignore[attr-defined]

    # ZMQ_PULL: receive orders from ZMQCommunicationAdapter
    pull = ctx.socket(zmq.PULL)  # type: ignore[attr-defined]
    pull.bind(order_endpoint)

    # ZMQ_PUB: publish ACK receipts for all SUB consumers
    pub = ctx.socket(zmq.PUB)  # type: ignore[attr-defined]
    pub.bind(ack_endpoint)

    print(f"[zmq_bridge] PULL bound to {order_endpoint}", flush=True)
    print(f"[zmq_bridge] PUB  bound to {ack_endpoint}", flush=True)
    print(f"[zmq_bridge] File fallback: outbox={outbox_dir} archive={archive_dir}", flush=True)

    _last_health_write = 0.0
    _last_heartbeat_check = 0.0
    _last_file_poll = 0.0
    _last_wal_truncate = 0.0
    _last_overflow_merge = 0.0
    _consecutive_hb_failures = 0
    # ── DQAF-20260621-034 Phase 1: Persisted dedup watermark ──────────
    # Replaces the old in-memory-only set (lost on restart → ghost replays).
    # Loaded from an append-only JSONL file so dedup survives Bridge crashes.
    # The file is truncated periodically to bound disk usage.
    _wal_processed_path = outbox_dir.parent / "bridge_processed_wal.jsonl"
    _processed_ids: set[str] = _load_processed_ids(_wal_processed_path)
    if _processed_ids:
        print(
            f"[zmq_bridge] Loaded {len(_processed_ids)} processed IDs from {_wal_processed_path}",
            flush=True,
        )

    # Write initial health heartbeat immediately so monitoring can confirm ZMQ mode
    _write_zmq_health(health_path, mt5, order_endpoint)

    # ── FIX-20260616-099: OFI TickPoller for ZMQ mode ──
    _ofi_collector_zmq = None
    _ofi_path_zmq = health_path.parent / "ofi_snapshot.json"
    if mt5 is not None:
        import threading as _thr3

        from core.features.ofi_collector import OFICollector

        _ofi_collector_zmq = OFICollector()

        def _ofi_poller_zmq() -> None:
            _last_msc = 0
            _sym = str(default_symbol)
            print(f"[zmq_bridge] OFI TickPoller started (symbol={_sym}, 1s)", flush=True)
            _ev = _thr3.Event()
            while True:
                try:
                    _t = mt5.symbol_info_tick(_sym)
                    if _t is not None:
                        _msc = int(getattr(_t, "time_msc", 0) or 0)
                        if _msc > _last_msc:
                            _last_msc = _msc
                            _ofi_collector_zmq.on_tick(
                                price=float(getattr(_t, "last", 0) or 0),
                                bid=float(getattr(_t, "bid", 0) or 0),
                                ask=float(getattr(_t, "ask", 0) or 0),
                                volume=float(getattr(_t, "volume", 0) or 0),
                            )
                except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                    pass
                _ev.wait(1.0)

        _thr3.Thread(target=_ofi_poller_zmq, daemon=True, name="ofi-tick-poller-zmq").start()

    try:
        while True:
            # ── Phase 3: Non-blocking ZMQ poll (1s timeout) ──────────────
            # Previously used blocking recv_string() which prevented file
            # polling.  Now polls with 1s timeout so we can periodically
            # scan the file outbox for orphaned WAL entries.
            _zmq_msg = None
            try:
                if pull.poll(timeout=1000):  # 1s
                    _zmq_msg = pull.recv_string()
            except zmq.ZMQError:  # type: ignore[attr-defined]
                pass

            _now = time.time()

            if _zmq_msg is not None:
                try:
                    payload: dict[str, Any] = json.loads(_zmq_msg)
                    envelope = payload.get("envelope", {})
                    msg_payload: dict[str, Any] = envelope.get("payload", {})
                    msg_id = envelope.get("message_id", "unknown")
                except json.JSONDecodeError:
                    print("[zmq_bridge] Invalid JSON received", flush=True)
                else:
                    # ── Guard: MT5 must be initialized ──
                    if mt5 is None:
                        _zmq_send_ack(
                            pub,
                            msg_id,
                            {
                                "ack_status": "rejected",
                                "detail": {
                                    "reason": "MT5 not initialized — pass --mt5-terminal-path"
                                },
                                "received_at": _utc_now(),
                            },
                        )
                        print(
                            json.dumps(
                                {
                                    "zmq_error": {
                                        "message_id": msg_id,
                                        "error": "MT5 not initialized",
                                    }
                                },
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )
                        if once:
                            return 1
                    else:
                        # ── Execute via existing MT5 logic ──
                        try:
                            action = normalize_action(msg_payload.get("action"))
                            ack_status, detail = _send_to_mt5(
                                mt5,
                                payload,
                                default_volume=default_volume,
                                deviation=deviation,
                                magic=magic,
                            )
                            _write_zmq_journal_entry(
                                journal_path=journal_path,
                                message_id=msg_id,
                                msg_payload=msg_payload,
                                action=action,
                                ack_status=ack_status,
                                detail=detail,
                                default_volume=default_volume,
                                target="exec_bridge",
                                mt5=mt5,
                            )
                            _zmq_send_ack(
                                pub,
                                msg_id,
                                {
                                    "ack_status": ack_status,
                                    "detail": detail,
                                    "received_at": _utc_now(),
                                },
                            )
                            print(
                                json.dumps(
                                    {
                                        "zmq_processed": {
                                            "message_id": msg_id,
                                            "ack_status": ack_status,
                                            "detail": detail,
                                        }
                                    },
                                    ensure_ascii=False,
                                ),
                                flush=True,
                            )
                            # ── DQAF-20260621-034 Phase 1: Persist + track processed ID ──
                            _processed_ids.add(msg_id)
                            _persist_processed_id(_wal_processed_path, msg_id)
                        except (
                            RuntimeError,
                            ValueError,
                            KeyError,
                            TypeError,
                            OSError,
                        ) as exc:  # BLE001:FOG
                            _zmq_send_ack(
                                pub,
                                msg_id,
                                {
                                    "ack_status": "error",
                                    "detail": {"reason": f"{type(exc).__name__}: {str(exc)[:200]}"},
                                    "received_at": _utc_now(),
                                },
                            )
                            print(
                                json.dumps(
                                    {
                                        "zmq_error": {
                                            "message_id": msg_id,
                                            "error": str(exc)[:200],
                                        }
                                    },
                                    ensure_ascii=False,
                                ),
                                flush=True,
                            )
            # ── Phase 3: File outbox fallback — 5s slow poll ─────────────
            # Picks up orders that were WAL-persisted to the file outbox but
            # never arrived via ZMQ (crash, breaker OPEN, network partition).
            if _now - _last_file_poll > 5.0:
                _last_file_poll = _now
                _pending = _list_pending(outbox_dir)
                if _pending:
                    _MAX_FILE_AGE_SEC = 3600  # 1 hour — skip stale orders
                    for _path in _pending:
                        # ── Age guard: skip files older than 1 hour ──────
                        # Historical outbox files (from before Phase 3 or
                        # from a prior bridge session) should be archived
                        # without re-execution.  Their orders have long
                        # since expired and re-sending them would be wrong.
                        try:
                            _file_age = _now - _path.stat().st_mtime
                        except OSError:
                            _file_age = 0.0
                        if _file_age > _MAX_FILE_AGE_SEC:
                            _rel = (
                                _path.relative_to(outbox_dir)
                                if outbox_dir in _path.parents
                                else Path(_path.name)
                            )
                            _arc = archive_dir / _rel
                            _arc.parent.mkdir(parents=True, exist_ok=True)
                            _safe_move(_path, _arc)
                            print(
                                json.dumps(
                                    {
                                        "file_fallback_stale_archived": {
                                            "path": str(_path),
                                            "age_hours": round(_file_age / 3600, 1),
                                        }
                                    },
                                    ensure_ascii=False,
                                ),
                                flush=True,
                            )
                            continue
                        _file_msg = _load_message(_path)
                        if _file_msg is None:
                            continue
                        _f_env = _file_msg.get("envelope", {})
                        _f_msg_id = str(_f_env.get("message_id", _path.stem))
                        # Dedup: skip if already processed via ZMQ
                        if _f_msg_id in _processed_ids:
                            # File is orphaned duplicate — archive it
                            _rel = (
                                _path.relative_to(outbox_dir)
                                if outbox_dir in _path.parents
                                else Path(_path.name)
                            )
                            _arc = archive_dir / _rel
                            _arc.parent.mkdir(parents=True, exist_ok=True)
                            _safe_move(_path, _arc)
                            continue
                        if mt5 is None:
                            continue
                        try:
                            _ = process_one(
                                _path,
                                outbox_dir=outbox_dir,
                                receipt_dir=receipt_dir,
                                archive_dir=archive_dir,
                                journal_path=journal_path,
                                protection_flag_path=protection_flag_path,
                                default_volume=default_volume,
                                deviation=deviation,
                                magic=magic,
                                dry_run=bool(dry_run),
                                mt5=mt5,
                            )
                            _processed_ids.add(_f_msg_id)
                            _persist_processed_id(_wal_processed_path, _f_msg_id)
                            print(
                                json.dumps(
                                    {
                                        "file_fallback_processed": {
                                            "message_id": _f_msg_id,
                                            "path": str(_path),
                                        }
                                    },
                                    ensure_ascii=False,
                                ),
                                flush=True,
                            )
                        except (
                            RuntimeError,
                            ValueError,
                            KeyError,
                            TypeError,
                            OSError,
                        ) as _f_exc:  # BLE001:FOG
                            print(
                                json.dumps(
                                    {
                                        "file_fallback_error": {
                                            "message_id": _f_msg_id,
                                            "error": str(_f_exc)[:200],
                                        }
                                    },
                                    ensure_ascii=False,
                                ),
                                flush=True,
                            )
            # ── DQAF-20260621-034 Phase 1: Periodic WAL truncation ─────
            if _now - _last_wal_truncate > 600:  # every 10 minutes
                _truncate_processed_wal(_wal_processed_path)
                _last_wal_truncate = _now

            # ── DQAF-20260621-034 Phase 3: Overflow merge tick ─────────
            if _now - _last_overflow_merge > 60:  # every 60 seconds
                _merge_overflow_files(journal_path)
                _last_overflow_merge = _now

            # ── Periodic health heartbeat ──
            if _now - _last_health_write > 30:
                _hb = {
                    "last_heartbeat_utc": _utc_now(),
                    "pid": os.getpid(),
                    "mt5_connected": mt5 is not None,
                    "outbox_pending": len(_list_pending(outbox_dir)),
                    "transport": "zmq",
                    "order_endpoint": order_endpoint,
                    "phase3_wal": True,
                    "processed_ids": len(_processed_ids),
                    "wal_processed_path": str(_wal_processed_path),
                }
                try:
                    from core.state.catalog import lookup
                    from core.state.writer import StateWriter

                    _w = StateWriter.from_state_path(health_path)
                    _w.write_artifact(lookup("MT5_BRIDGE_HEALTH"), _w._symbol, _hb)
                except OSError:
                    pass
                _last_health_write = _now
                # ── OFI snapshot → atomic IPC file ──
                if _ofi_collector_zmq is not None:
                    try:
                        _ofi_data = _ofi_collector_zmq.settle_m5_bar()
                        if _ofi_data:
                            _ofi_tmp = _ofi_path_zmq.with_suffix(".tmp")
                            _ofi_tmp.write_text(
                                json.dumps(_ofi_data, ensure_ascii=False), encoding="utf-8"
                            )
                            os.replace(str(_ofi_tmp), str(_ofi_path_zmq))
                    except OSError:
                        pass
            # ── MT5 heartbeat + reconnect ──
            if _now - _last_heartbeat_check > _HEARTBEAT_INTERVAL:
                _last_heartbeat_check = _now
                if mt5 is not None:
                    if _check_mt5_heartbeat(mt5):
                        _consecutive_hb_failures = 0
                    else:
                        _consecutive_hb_failures += 1
                        print(
                            json.dumps(
                                {
                                    "event": "zmq_bridge_mt5_heartbeat_lost",
                                    "consecutive_failures": _consecutive_hb_failures,
                                    "time": _utc_now(),
                                },
                                ensure_ascii=False,
                            ),
                            flush=True,
                        )
                        if _consecutive_hb_failures >= _MAX_RECONNECT_ATTEMPTS:
                            print(
                                json.dumps(
                                    {
                                        "event": "zmq_bridge_mt5_fatal",
                                        "action": "exiting",
                                        "time": _utc_now(),
                                    },
                                    ensure_ascii=False,
                                ),
                                flush=True,
                            )
                            sys.exit(1)
                        if terminal_path:
                            if _reconnect_mt5(mt5, str(terminal_path), symbol=default_symbol):
                                _consecutive_hb_failures = 0

            if once:
                return 0

    finally:
        try:  # BLE001:FOG (was: FOG/LAC)
            pull.close()
            pub.close()
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            pass
        if mt5 is not None:
            try:  # BLE001:FOG (was: FOG/LAC)
                mt5.shutdown()
                print("[zmq_bridge] MT5 shutdown", flush=True)
            except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                pass


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # ── DQAF-20260622-059 / P1: Bootstrap magic↔strategy mappings from YAML ──
    _cfg_guess = (
        "configs/live_btc.yaml"
        if args.journal_path and "btc" in str(args.journal_path)
        else "configs/live.yaml"
    )
    try:
        from core.contracts.strategy_magic import init_magic_mappings

        init_magic_mappings(_cfg_guess)
    except Exception:
        pass  # hardcoded fallback already loaded on import

    if args.zmq:
        # ── Initialize MT5 once ──
        mt5 = None
        terminal_path = args.mt5_terminal_path
        if terminal_path:
            try:
                import MetaTrader5 as _mt5

                if _mt5.initialize(path=str(terminal_path)):
                    mt5 = _mt5
                    print(f"[zmq_bridge] MT5 initialized: {terminal_path}", flush=True)
                else:
                    print(f"[zmq_bridge] WARN: MT5 init failed: {_mt5.last_error()}", flush=True)
            except (RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:  # BLE001:FOG
                print(f"[zmq_bridge] WARN: MT5 unavailable: {exc}", flush=True)
        health_path = (
            Path(args.health_path)
            if args.health_path
            else Path(args.receipt_dir).parent / "reports" / "mt5_bridge_health.json"
        )
        health_path.parent.mkdir(parents=True, exist_ok=True)

        return run_zmq_worker(
            order_endpoint=args.zmq_order_endpoint,
            ack_endpoint=args.zmq_ack_endpoint,
            receipt_dir=Path(args.receipt_dir),
            journal_path=Path(args.journal_path),
            protection_flag_path=Path(args.protection_flag_path),
            default_volume=args.default_volume,
            deviation=args.deviation,
            magic=args.magic,
            dry_run=bool(args.dry_run),
            mt5=mt5,
            terminal_path=terminal_path or "",
            default_symbol=args.default_symbol,
            health_path=health_path,
            once=bool(args.once),
            outbox_dir=Path(args.outbox_dir),
            archive_dir=Path(args.archive_dir),
        )

    return run_worker(args)


if __name__ == "__main__":
    raise SystemExit(main())
