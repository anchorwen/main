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
from core.runtime.fault_handler import FaultLevel, FaultTolerantContext, log_and_continue


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mt5_bridge_worker")
    parser.add_argument("--outbox-dir", default="data/mt5_outbox")
    parser.add_argument("--receipt-dir", default="data/receipts")
    parser.add_argument("--archive-dir", default="data/mt5_outbox_processed")
    parser.add_argument("--default-volume", type=float, default=0.01)
    parser.add_argument("--deviation", type=int, default=20)
    parser.add_argument("--magic", type=int, default=90001)
    parser.add_argument("--default-symbol", default="XAUUSDc", help="Trading symbol for reconnect symbol_select")
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
    return parser


def _utc_now() -> str:
    return (
        datetime.now(UTC)
        .replace(tzinfo=None)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _list_pending(outbox_dir: Path) -> list[Path]:
    if not outbox_dir.exists():
        return []
    return sorted(outbox_dir.rglob("*.mt5.json"))


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
    """
    from core.infrastructure.distributed_lock import FileLock

    journal_path.parent.mkdir(parents=True, exist_ok=True)
    lock = FileLock(
        "live_trade_journal", lock_dir=str(journal_path.parent / ".locks"), ttl_seconds=10
    )
    acquired = lock.acquire(blocking=True, timeout_seconds=5)
    if not acquired.acquired:
        print(
            json.dumps(
                {
                    "event": "journal_lock_failed",
                    "message_id": record.get("message_id", ""),
                    "error": acquired.error or "timeout",
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
            except Exception:  # noqa: BLE001
                pass  # journal dedup is best-effort — skip malformed lines
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
        return _mt5_market_open(
            mt5,
            envelope=envelope,
            msg_payload=msg_payload,
            default_volume=default_volume,
            deviation=deviation,
            magic=magic,
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
    _fingerprint = (symbol, side, round(volume, 4), round(stop_loss or 0, 2), round(take_profit or 0, 2))
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
        }
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
    _magic = order_magic
    _strategy = ""
    with log_and_continue(component="Bridge:magic_resolve"):
        from core.contracts.strategy_magic import MAGIC_TO_STRATEGY

        _strategy = MAGIC_TO_STRATEGY.get(_magic, "")
    _open_msg_id = msg_payload.get("open_message_id", "")
    position_ticket = detail.get("order") or coerce_position_ticket(msg_payload)
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
        "pnl": msg_payload.get("pnl"),
        "label": _label,
        "comment": _comment,
        "magic": _magic,
        "strategy": _strategy,
        "effective_volume_hint": effective_volume(msg_payload, default_volume=default_volume),
        "position_ticket": position_ticket,
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
    except Exception:  # noqa: BLE001
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
        with log_and_continue(component="Bridge:shutdown"):
            mt5_module.shutdown()
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
                with log_and_continue(component="Bridge:symbol_select"):
                    mt5_module.symbol_select(symbol, True)
                return True
        except Exception:  # noqa: BLE001
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
        except Exception as exc:  # noqa: BLE001
            print(f"[bridge] WARN: MT5 unavailable: {exc}", flush=True)

    health_path = Path(args.receipt_dir).parent / "reports" / "mt5_bridge_health.json"
    health_path.parent.mkdir(parents=True, exist_ok=True)
    _last_health_write = 0.0
    _last_heartbeat_check = 0.0
    _consecutive_hb_failures = 0

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
                except Exception as exc:  # noqa: BLE001
                    processed.append(
                        {
                            "message_id": path.stem,
                            "ack_status": "error",
                            "reason": f"{type(exc).__name__}: {str(exc)[:200]}",
                        }
                    )
            if processed:
                print(json.dumps({"processed": processed}, ensure_ascii=False, default=str))

            # ── Periodic health heartbeat ──
            _now = time.time()
            if _now - _last_health_write > 30:
                _hb = {
                    "last_heartbeat_utc": datetime.now(UTC).replace(tzinfo=None).isoformat(),
                    "pid": os.getpid(),
                    "mt5_connected": mt5 is not None,
                    "outbox_pending": len(_list_pending(outbox_dir)),
                }
                with log_and_continue(component="Bridge:health_write"):
                    health_path.write_text(json.dumps(_hb, ensure_ascii=False), encoding="utf-8")
                _last_health_write = _now

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
            with log_and_continue(component="Bridge:final_shutdown"):
                mt5.shutdown()
                print("[bridge] MT5 shutdown", flush=True)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run_worker(args)


if __name__ == "__main__":
    raise SystemExit(main())
