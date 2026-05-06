"""Minimal MT5 bridge worker.

Consumes handoff files from mt5 outbox and writes receipt ack files.

Envelope.payload contract (Phase B): volume/lots, action open|close|modify_sltp — see docs/LIVE_EXECUTION_CONTRACT.md
"""

from __future__ import annotations

import argparse
import json
import shutil
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mt5_bridge_worker")
    parser.add_argument("--outbox-dir", default="data/mt5_outbox")
    parser.add_argument("--receipt-dir", default="data/receipts")
    parser.add_argument("--archive-dir", default="data/mt5_outbox_processed")
    parser.add_argument("--default-volume", type=float, default=0.01)
    parser.add_argument("--deviation", type=int, default=20)
    parser.add_argument("--magic", type=int, default=90001)
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    parser.add_argument("--journal-path", default="data/live_trade_journal.jsonl")
    parser.add_argument("--protection-flag-path", default="data/live_dispatch_block.flag")
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


def _load_message(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


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
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    mid = record.get("message_id", "")
    if mid and journal_path.exists():
        try:
            for line in journal_path.read_text(encoding="utf-8").splitlines():
                if mid in line:
                    return  # duplicate, skip
        except Exception:
            pass
    with journal_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")


def _is_protection_active(protection_flag_path: Path) -> bool:
    return protection_flag_path.exists()


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
    payload: dict[str, Any], *, default_volume: float, deviation: int, magic: int
) -> tuple[str, dict[str, Any]]:
    try:
        import MetaTrader5 as mt5  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on local env
        return "rejected", {"reason": "mt5_module_unavailable", "error": str(exc)}

    mt5_info = payload.get("mt5", {})
    terminal_path = mt5_info.get("terminal_path")
    if not terminal_path:
        return "rejected", {"reason": "terminal_path_missing"}
    if not mt5.initialize(
        path=str(terminal_path)
    ):  # pragma: no cover - depends on local env  # type: ignore[reportAttributeAccessIssue]
        return "rejected", {"reason": "mt5_initialize_failed", "last_error": mt5.last_error()}  # type: ignore[reportAttributeAccessIssue]
    try:
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
    finally:
        mt5.shutdown()  # pragma: no cover - depends on local env  # type: ignore[reportAttributeAccessIssue]


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
        return "accepted", {
            "retcode": retcode,
            "order": getattr(result, "order", None),
            "request": request,
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
        return "accepted", {
            "retcode": retcode,
            "order": getattr(result, "order", None),
            "request": request,
        }
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
) -> dict[str, Any]:
    payload = _load_message(message_path)
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
        # Per-order magic override from brain (multi-brain attribution).
        order_magic = int(msg_payload.get("magic", magic))
        ack_status, detail = _send_to_mt5(
            payload, default_volume=default_volume, deviation=deviation, magic=order_magic
        )

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
    shutil.move(str(message_path), str(archive_path))

    vol_disp = msg_payload.get("volume", msg_payload.get("lots"))
    action = normalize_action(msg_payload.get("action"))
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
        "pnl": None,
        "label": None,
        "effective_volume_hint": effective_volume(msg_payload, default_volume=default_volume),
        "position_ticket": detail.get("order") or coerce_position_ticket(msg_payload),
        "execution_payload_schema": msg_payload.get("execution_payload_schema"),
        "sl": msg_payload.get("sl", msg_payload.get("stop_loss")),
        "tp": msg_payload.get("tp", msg_payload.get("take_profit")),
        "outbox_path": str(message_path),
        "archive_path": str(archive_path),
        "receipt_path": str(receipt_path),
    }
    _append_journal(journal_path, journal_record)

    result = {
        "message_id": message_id,
        "ack_status": ack_status,
        "receipt_path": str(receipt_path),
        "archive_path": str(receipt_path),
    }
    return result


def run_worker(args: argparse.Namespace) -> int:
    outbox_dir = Path(args.outbox_dir)
    receipt_dir = Path(args.receipt_dir)
    archive_dir = Path(args.archive_dir)
    journal_path = Path(args.journal_path)
    protection_flag_path = Path(args.protection_flag_path)
    while True:
        processed = []
        for path in _list_pending(outbox_dir):
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
                )
            )
        if processed:
            print(json.dumps({"processed": processed}, ensure_ascii=False, default=str))
        if args.once:
            return 0
        time.sleep(args.poll_seconds)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run_worker(args)


if __name__ == "__main__":
    raise SystemExit(main())
