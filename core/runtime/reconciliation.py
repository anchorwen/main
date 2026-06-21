"""Position reconciliation — detect MT5 closes and build journal entries.

Extracted from live_cycle.py per the Strangler Fig pattern.
Compares Python-tracked known_open_tickets against MT5 ground truth
to detect positions closed externally (SL/TP hit, manual close).
"""

from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.runtime.fault_handler import FaultLevel, FaultTolerantContext, fail_open_guard
from core.runtime.time_utils import _utc_iso  # consolidated


def _load_settled_keys(ledger_path: str) -> set[tuple[int, str]]:
    """Scan ledger_events.jsonl for existing (position_ticket, brain_id) pairs.

    DQAF-20260621-030: Idempotency gate — prevents duplicate SignalSettled
    events on system restart.  Returns a set of (ticket, brain_id) tuples
    that have already been settled.

    Perf note: reads the full file.  At 2.7MB / ~7k lines this is fast.
    If the ledger grows beyond ~50MB, consider an in-memory cache that
    survives within the process lifetime (loop_iteration > 1 bypasses the
    startup reconciliation path entirely, so this function is called at most
    once per system start).
    """
    import json as _json

    _keys: set[tuple[int, str]] = set()
    _path = Path(ledger_path) if not isinstance(ledger_path, Path) else ledger_path
    if not _path.exists():
        return _keys
    try:
        for _line in _path.read_text(encoding="utf-8").splitlines():
            _line = _line.strip()
            if not _line or "SignalSettled" not in _line:
                continue
            try:
                _rec = _json.loads(_line)
                _ticket = _rec.get("position_ticket", 0)
                if not isinstance(_ticket, int):
                    try:
                        _ticket = int(_ticket)
                    except (TypeError, ValueError):
                        _ticket = 0
                _bid = str(_rec.get("brain_id", ""))
                if _bid:
                    _keys.add((_ticket, _bid))
            except (_json.JSONDecodeError, KeyError):
                continue
    except OSError:
        logging.getLogger(__name__).warning(
                "Failed to read ledger for idempotency check: %s", _path
            )
    return _keys


def reconcile_closed_positions(
    mt5_worker: Any,
    symbol: str,
    journal_path: str,
    known_tickets: dict[int, dict[str, Any]],
    state: Any = None,
) -> list[dict[str, Any]]:
    """Detect positions closed by SL/TP and return close journal entries.

    Uses ThreadPoolExecutor (not daemon threads) so that MT5 timeouts can be
    logged with structured reason-codes and affected tickets.
    """
    closed_entries: list[dict[str, Any]] = []
    if mt5_worker is None:
        return closed_entries

    # ── positions_get ──
    with FaultTolerantContext(
        level=FaultLevel.CRASH,
        component="MT5_IPC:positions_get:reconciliation",
    ):
        current_positions = mt5_worker.positions_get(symbol=symbol)

    current_tickets = {p.ticket for p in (current_positions or [])}

    for ticket, open_entry in list(known_tickets.items()):
        if ticket in current_tickets:
            continue

        deals = None
        with FaultTolerantContext(
            level=FaultLevel.CRASH,
            component="MT5_IPC:history_deals_get:reconciliation",
        ):
            deals = mt5_worker.history_deals_get(position=ticket)

        close_price = None
        close_time = None
        close_reason: int | None = None
        close_volume = open_entry.get("volume") or open_entry.get("effective_volume_hint", 0.0)

        if deals:
            for deal in deals:
                deal_reason = getattr(deal, "reason", -1)
                if deal_reason in (4, 5):  # DEAL_REASON_SL=4, DEAL_REASON_TP=5
                    close_price = getattr(deal, "price", None)
                    close_time = getattr(deal, "time", None)
                    close_reason = deal_reason

            if close_price is None and deals and len(deals) >= 2:
                exit_deals = [d for d in deals if getattr(d, "entry", -1) == 1]
                if exit_deals:
                    last_exit = max(exit_deals, key=lambda d: getattr(d, "time", 0))
                    close_price = getattr(last_exit, "price", None)
                    close_time = getattr(last_exit, "time", None)
                if close_price is None:
                    last_deal = max(deals, key=lambda d: getattr(d, "time", 0))
                    close_price = getattr(last_deal, "price", None)
                    close_time = getattr(last_deal, "time", None)

        side = str(open_entry.get("side", ""))
        # ── Resolve entry price from MT5 deal history (actual fill) ──
        entry_price: float | None = None
        if deals:
            entry_deals = [d for d in deals if getattr(d, "entry", -1) == 0]
            if entry_deals:
                _entry_fill = getattr(entry_deals[0], "price", None)
                if _entry_fill is not None and _entry_fill > 0:
                    entry_price = float(_entry_fill)
        # Fallback L1: open journal entry's order request price
        if entry_price is None:
            detail = open_entry.get("detail", {})
            if isinstance(detail, dict):
                req = detail.get("request", {})
                _req_price = req.get("price")
                if _req_price is not None and _req_price > 0:
                    entry_price = float(_req_price)
        # Fallback L2: engine-registered entry_price
        if entry_price is None:
            _reg_ep = open_entry.get("entry_price")
            if _reg_ep is not None and float(_reg_ep) > 0:
                entry_price = float(_reg_ep)

        pnl = None
        if entry_price is not None and close_price is not None and close_volume:
            if side == "long":
                pnl = round((close_price - entry_price) * close_volume, 2)
            elif side == "short":
                pnl = round((entry_price - close_price) * close_volume, 2)

        # ── PnL fallback: engine PnL or mid-price estimate ──
        if pnl is None and entry_price is not None and close_volume:
            _engine_pnl = open_entry.get("_engine_close_pnl")
            if _engine_pnl is not None:
                pnl = float(_engine_pnl)
            elif state is not None and getattr(state, "_recent_mid_prices", None):
                try:
                    _fallback_close = state._recent_mid_prices[-1]
                    if _fallback_close > 0:
                        if side == "long":
                            pnl = round((_fallback_close - entry_price) * close_volume, 2)
                        elif side == "short":
                            pnl = round((entry_price - _fallback_close) * close_volume, 2)
                        close_price = close_price or _fallback_close
                except (IndexError, ValueError):
                    pass

        # ── FIX-20260612-003: Trail-aware SL label ──
        # When close_reason == 4 (SL hit), check if the position had trail
        # activity via state.position_manager → pos.trail_advances.
        # If trail was actively tightening the SL, label as sl_hit_trailed
        # instead of sl_hit_first — closes the TRAIL_TELEMETRY_BLINDSPOT.
        trail_active = False
        if state is not None:
            _pm = getattr(state, "position_manager", None)
            if _pm is not None:
                _pos = _pm.get_position(ticket) if hasattr(_pm, "get_position") else None
                if _pos is not None and getattr(_pos, "trail_advances", 0) > 0:
                    trail_active = True

        label = None
        if close_reason in (4,):
            label = "sl_hit_trailed" if trail_active else "sl_hit_first"
        elif close_reason in (5,):
            label = "tp_hit_first"
        elif pnl is not None:
            if pnl > 0:
                label = "win"
            elif pnl < 0:
                label = "loss"
            else:
                label = "breakeven"
        else:
            label = "manual_close"

        # ── DQAF-20260614-012: Full MT5 deal reason mapping ──
        # Previously only SL(4) and TP(5) were mapped — all other deal
        # reasons fell into "unknown_close" (42% of all exits).
        # MT5 DEAL_REASON codes: 0=CLIENT, 1=MOBILE, 2=WEB, 3=SIGNAL,
        # 4=SL, 5=TP, 6=SO (Stop Out), 7=RO (Risk Out).
        _DEAL_REASON_MAP = {
            0: "client_close",
            1: "mobile_close",
            2: "web_close",
            3: "signal_close",
            4: "sl_hit",
            5: "tp_hit",
            6: "stop_out",
            7: "risk_out",
        }
        close_reason_str = _DEAL_REASON_MAP.get(
            close_reason or 0,
            f"mt5_deal_reason_{close_reason}" if close_reason else "unknown_close",
        )

        # ── Resolve strategy name and magic with fallback ──
        _resolved_strategy = open_entry.get("strategy", "")
        _resolved_magic = open_entry.get("magic")
        if _resolved_magic is None:
            _resolved_magic = open_entry.get("detail", {}).get("request", {}).get("magic", 0)
        if not _resolved_strategy and _resolved_magic:
            with fail_open_guard("Reconciliation:MagicResolve"):
                from core.contracts.strategy_magic import MAGIC_TO_STRATEGY
                _resolved_strategy = MAGIC_TO_STRATEGY.get(int(_resolved_magic), "")

        if close_price is None:
            print(
                json.dumps(
                    {
                        "event": "reconciliation_deals_unresolved",
                        "time": _utc_iso(),
                        "ticket": ticket,
                        "deals_count": len(deals) if deals else 0,
                        "entry_price": entry_price,
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

        close_time_iso = (
            datetime.fromtimestamp(close_time, tz=UTC).isoformat().replace("+00:00", "Z")
            if close_time
            else ""
        )

        close_entry = {
            "schema_version": "live_trade_journal.v2",
            "recorded_at": close_time_iso,
            "message_id": f"close_{open_entry.get('message_id', 'unknown')}",
            "target": "exec_bridge",
            "ack_status": "closed",
            "detail": {
                "reason": close_reason_str,
                "close_price": close_price,
                "pnl": pnl,
            },
            "symbol": symbol,
            "action": "close",
            "side": side,
            "volume": close_volume,
            "pnl": pnl,
            "label": label,
            "position_ticket": ticket,
            "magic": _resolved_magic,
            "strategy": _resolved_strategy,
            "sl": open_entry.get("sl"),
            "tp": open_entry.get("tp"),
            "open_message_id": open_entry.get("message_id"),
            "brain_ids": open_entry.get("brain_ids"),
        }
        closed_entries.append(close_entry)

        # ── DQAF-20260614-013: Write live label on every close ──
        # Previously labels were only assigned by daily_ops offline batch
        # (_step_label_builder), leaving 16.8% of trades unlabeled between
        # runs.  Now every close event writes a label record immediately.
        try:
            _labels_path = Path(journal_path).parent / "reports" / "live_labels.jsonl"
            _labels_path.parent.mkdir(parents=True, exist_ok=True)
            _label_entry = {
                "position_ticket": ticket,
                "entry_time": open_entry.get("recorded_at", ""),
                "close_time": close_time_iso,
                "pnl": pnl,
                "label": label,
                "side": side,
                "entry_price": float(open_entry.get("entry_price", 0) or 0),
                "close_price": float(close_price) if close_price else 0.0,
                "exit_reason": close_reason_str,
                "strategy": _resolved_strategy,
                "symbol": symbol,
                "generated_by": "reconciliation.reconcile_closed_positions",
            }
            with open(_labels_path, "a", encoding="utf-8") as _lf:
                _lf.write(json.dumps(_label_entry, ensure_ascii=False) + "\n")
        except Exception:  # BLE001:FOG
            with fail_open_guard("reconciliation:reconcile_closed_positions"):
                pass  # best-effort — label write must not block reconciliation
        # ── DQAF-20260614-005c: SignalSettled from startup reconciliation ──
        # This path handles ALL real closes (the reconcile_mt5_close_events
        # path in live_cycle.py uses PositionCloseAdapter and rarely fires).
        # Without this, SignalSettled stays 0 forever — all closes go through
        # this reconciliation path at startup.
        #
        # ── DQAF-20260621-030: Idempotency Gate ──
        # FIX: Before writing any SignalSettled, scan the existing ledger for
        # already-settled (position_ticket, brain_id) pairs.  Without this gate,
        # every system restart re-settles ALL historically-closed positions,
        # causing unbounded ledger bloat (25MB → 2.7MB after cleanup).
        # Idempotency key: (position_ticket, brain_id) — a brain's signal can
        # only be settled once against a given position.
        _brain_ids = open_entry.get("brain_ids")
        if _brain_ids and pnl != 0:
            with fail_open_guard("Reconciliation:PnLEventWrite"):
                from core.contracts.events import PnLEvent
                from core.data.event_writer import EventWriter

                _ledger_path = str(
                    Path(journal_path).parent / "ledger_events.jsonl"
                )
                # ── DQAF-20260621-030: Load already-settled keys ──
                _already_settled = _load_settled_keys(_ledger_path)
                _writer = EventWriter(_ledger_path)
                _settled_ts = datetime.now(UTC)
                # entry_price may be at: top level, detail.entry_price,
                # or detail.request.price (MT5 order fill price)
                _entry_price = float(open_entry.get("entry_price", 0) or 0)
                if _entry_price <= 0:
                    _detail = open_entry.get("detail", {})
                    if isinstance(_detail, dict):
                        _ep = _detail.get("entry_price", 0) or _detail.get("request", {}).get("price", 0)
                        if _ep:
                            _entry_price = float(_ep)
                _close_price_f = float(close_price) if close_price else 0.0
                _pnl_r = (
                    (_close_price_f - _entry_price) / _entry_price
                    if _entry_price > 0 and side == "long"
                    else (_entry_price - _close_price_f) / _entry_price
                    if _entry_price > 0
                    else 0.0
                )
                _skipped = 0
                for _bid in _brain_ids:
                    # ── DQAF-20260621-030: Idempotency guard ──
                    _dedup_key = (ticket, str(_bid))
                    if _dedup_key in _already_settled:
                        _skipped += 1
                        continue
                    _event = PnLEvent(
                        timestamp=_settled_ts,
                        source="live",
                        event_type="SignalSettled",
                        brain_id=str(_bid),
                        symbol=symbol,
                        direction=side,
                        entry_price=_entry_price,
                        exit_price=_close_price_f,
                        pnl_r=round(_pnl_r, 6),
                        position_ticket=ticket,
                        generated_by="reconciliation._reconcile_closed_positions",
                    )
                    _writer.write(_event)
                    _already_settled.add(_dedup_key)
                if _skipped > 0:
                    logging.getLogger(__name__).debug(
                        "[IDEMPOTENCY] reconciliation: skipped %d/%d already-settled "
                        "signals for ticket=%s",
                        _skipped, len(_brain_ids), ticket,
                    )

        # ── Record exit for re-entry guard (native MT5 SL/TP) ──
        if state is not None:
            _exit_strategy = _resolved_strategy
            _exit_side = side
            _exit_price = float(close_price) if close_price else 0.0
            _exit_ts = float(close_time) if close_time else time.time()
            _exit_confidence = open_entry.get("entry_consensus", {}).get("consensus_score", 0.5)
            if _exit_strategy and _exit_side in ("long", "short"):
                try:
                    from core.execution.reentry_guard import ExitRecord, ensure_reentry_state

                    _rec = ExitRecord(
                        timestamp=_exit_ts,
                        strategy_name=_exit_strategy,
                        direction=_exit_side,
                        reason=close_reason_str,
                        confidence=float(_exit_confidence),
                        price=_exit_price,
                        ticket=ticket,
                    )
                    _rs = ensure_reentry_state(state._reentry_states, _exit_strategy)
                    _rs.record_exit(_rec)
                except Exception:  # BLE001:FOG_WRAPPED
                    with fail_open_guard("Reconciliation:ReentryGuardRecord"):
                        raise
                    logging.getLogger(__name__).warning(
                        "Reentry guard state recording failed ticket=%s strategy=%s — "
                        "reentry protection is volatile until next persist",
                        ticket,
                        _exit_strategy,
                    )

        del known_tickets[ticket]

    return closed_entries
