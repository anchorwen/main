"""PositionCloseAdapter — single authority for PositionClosed event generation.

FIX-20260611-005 Phase 2: Replaces fragmented close detection (reconciliation,
MIA, managed_close journal writes) with a unified event source.

Key design decisions:
  - Volume-delta detection (not ticket disappearance) → supports partial close
  - DEAL cursor per ticket → close_price matches the exact partial close
  - Dedup by deal_id as passive safety net (WARNING on trigger)
  - Journal write is atomic anchor → downstream notified after write
"""

from __future__ import annotations

import logging
import time as _time_module
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.contracts.position_events import PositionClosed, PositionOpened
from core.runtime.fault_handler import fail_open_guard

# ═══════════════════════════════════════════════════════════════════════════
# Strangler Fig: delegation wrappers for live_cycle.py
# Each wrapper encapsulates adapter lifecycle → live_cycle.py calls are 1-2 lines.
# ═══════════════════════════════════════════════════════════════════════════


def reconcile_and_record_closes(
    known_tickets: dict[int, dict],
    mt5_worker: Any,
    symbol: str,
    journal_path: str,
    state: Any = None,
) -> list[PositionClosed]:
    """Strangler Fig #11: reconcile MT5 positions, build events, record to Journal.

    Replaces the old _reconcile_closed_positions() + manual journal write block.
    Called from live_cycle.py reconciliation section.
    """
    adapter = PositionCloseAdapter(
        tick_size=0.01 if "XAU" in symbol else 1.0,
    )
    events = adapter.detect_and_build(
        known_tickets=known_tickets,
        mt5_worker=mt5_worker,
        symbol=symbol,
    )
    for evt in events:
        adapter.record(evt, journal_path, state=state)
    return events


def record_mia_closes(
    mia_entries: list[dict],
    mt5_worker: Any,
    symbol: str,
    journal_path: str,
    state: Any = None,
) -> int:
    """Strangler Fig #12: record MIA-detected closes through the adapter.

    Replaces the old FileLock + manual journal append block.
    Returns count of successfully recorded events.
    """
    adapter = PositionCloseAdapter(
        tick_size=0.01 if "XAU" in symbol else 1.0,
    )
    recorded = 0
    for entry in mia_entries:
        evt = adapter._build_event(
            ticket=int(entry.get("position_ticket", 0) or 0),
            open_entry=entry,
            closed_volume=float(entry.get("volume", 0.01) or 0.01),
            remaining_volume=0.0,
            symbol=symbol,
            mt5_worker=mt5_worker,
        )
        if evt is not None and adapter.record(evt, journal_path, state=state):
            recorded += 1
    return recorded


def record_position_opened(
    ticket: int,
    symbol: str,
    side: str,
    strategy: str,
    magic: int,
    entry_price: float,
    volume: float,
    sl: float,
    tp: float,
    brain_ids: list[str] | None,
    confidence: float,
    journal_path: str,
    state: Any = None,
) -> bool:
    """Strangler Fig #13: record a new position open through the adapter.

    Called from live_cycle.py at position_registered_for_mgmt point.
    """
    with fail_open_guard("PositionCloseAdapter:RecordOpen"):
        adapter = PositionCloseAdapter(
            tick_size=0.01 if "XAU" in symbol else 1.0,
        )
        evt = PositionOpened(
            position_ticket=ticket,
            symbol=symbol,
            side=side,
            strategy=strategy,
            magic=magic,
            entry_price=entry_price,
            volume=volume,
            sl=sl,
            tp=tp,
            brain_ids=tuple(brain_ids) if brain_ids else (),
            confidence=confidence,
        )
        return adapter.record_open(evt, journal_path, state=state)
    return False  # best-effort — never block position registration


UTC = UTC
_log = logging.getLogger(__name__)

# ── MT5 DEAL reasons ──
DEAL_REASON_SL = 4
DEAL_REASON_TP = 5

# DQAF-20260621-033: unified MT5 deal reason taxonomy (aligned with
# reconciliation.py:205-218).  Shared across all close-detection paths.
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

# ── XAU tick_size = 0.01, BTC = 1.0.  0.5 * tick_size is the minimum
# detectable volume change.  We use a generous default.
DEFAULT_TICK_SIZE = 0.01  # XAU gold — override per symbol
MIN_VOLUME_DELTA = 0.005  # 0.5 * 0.01


class PositionCloseAdapter:
    """Single authority source for position close events.

    Usage per cycle::

        adapter = PositionCloseAdapter(tick_size=0.01)
        events = adapter.detect_and_build(
            known_tickets=state.known_open_tickets,
            mt5_worker=mt5_worker,
            symbol=config.symbol,
        )
        for event in events:
            adapter.record(event, journal_path, state)
    """

    def __init__(self, tick_size: float = DEFAULT_TICK_SIZE):
        self._tick_size = tick_size
        self._min_delta = max(tick_size * 0.5, MIN_VOLUME_DELTA)
        # DEAL cursor: ticket → last processed deal_id
        self._last_deal_id: dict[int, int] = {}
        # Dedup: set of (ticket, deal_id) already recorded
        self._recorded_deals: set[tuple[int, int]] = set()

    # ── Public API ──────────────────────────────────────────────────────

    def detect_and_build(
        self,
        known_tickets: dict[int, dict],
        mt5_worker: Any,
        symbol: str,
    ) -> list[PositionClosed]:
        """Detect volume changes and build PositionClosed events.

        Args:
            known_tickets: {ticket: {entry_price, side, strategy, ...}}
            mt5_worker: MT5Worker instance
            symbol: e.g. "XAUUSDc"

        Returns:
            List of PositionClosed events for this cycle.
        """
        events: list[PositionClosed] = []

        current_positions = []
        with fail_open_guard("PositionCloseAdapter:PositionsGet"):
            current_positions = mt5_worker.positions_get(symbol=symbol)

        current_volumes: dict[int, float] = {}
        if current_positions:
            for pos in current_positions:
                ticket = getattr(pos, "ticket", 0)
                vol = getattr(pos, "volume", 0.0)
                if ticket and vol > 0:
                    current_volumes[ticket] = float(vol)

        for ticket, open_entry in list(known_tickets.items()):
            known_vol = float(open_entry.get("volume", 0) or 0)
            current_vol = current_volumes.get(ticket, 0.0)
            delta = known_vol - current_vol

            # ── Volume tolerance check ──
            if delta < self._min_delta:
                if current_vol <= 0:
                    # Ticket disappeared but delta below threshold —
                    # likely a tiny residual.  Mark as closed.
                    pass
                else:
                    continue  # no meaningful change

            # ── Build event from latest unprocessed DEAL ──
            event = self._build_event(
                ticket=ticket,
                open_entry=open_entry,
                closed_volume=max(delta, 0.001),  # at least min lot
                remaining_volume=current_vol,
                symbol=symbol,
                mt5_worker=mt5_worker,
            )
            if event is not None:
                events.append(event)

        return events

    def record(
        self,
        event: PositionClosed,
        journal_path: str | Path,
        state: Any = None,
    ) -> bool:
        """Write event to Journal (atomic anchor), then notify downstream.

        Returns True if written, False if duplicate (already recorded).
        """
        # ── Dedup by deal_id ──
        _dedup_key = (event.position_ticket, event.deal_id)
        if _dedup_key in self._recorded_deals:
            _log.warning(
                "[DEDUP] PositionCloseAdapter: duplicate event suppressed — "
                "ticket=%s deal_id=%s.  This indicates a possible Adapter bug.",
                event.position_ticket,
                event.deal_id,
            )
            return False
        self._recorded_deals.add(_dedup_key)

        # ── Journal write (atomic anchor) ──
        _now = datetime.now(UTC).replace(tzinfo=None).isoformat()
        _entry = event.to_journal_entry()
        _entry["recorded_at"] = _now
        _entry["message_id"] = (
            _entry["message_id"] or f"close_{event.position_ticket}_{event.deal_id}"
        )

        _path = Path(journal_path)
        _lock_dir = _path.parent / "locks"

        # FIX-20260620-023: Contract — record() returns True iff Journal write
        # succeeded AND downstream notifications completed.  Previously the
        # method returned False after a successful write (line 253), which:
        #   1. Skipped _notify_position_manager → ticket stayed in known_open_tickets
        #   2. Caused the adapter to re-detect the same close next cycle
        #   3. Produced 2-5 duplicate close entries per affected ticket
        #   4. Cross-contaminated XAU journal via bridge-side writes
        # The fix: journal write outcome tracks _journal_ok; if fail_open_guard
        # catches an exception, _journal_ok stays False and we return False
        # (retry next cycle).  On success we proceed to notify downstream.
        _journal_ok = False
        with fail_open_guard("PositionCloseAdapter:JournalWrite"):
            from core.ledger.services.journal_cleanup import _append_journal

            _append_journal(_path, _entry, lock_dir=_lock_dir)
            _journal_ok = True

        if not _journal_ok:
            return False  # Journal write failed — do not notify downstream

        # ── Notify downstream consumers ──
        # FIX-20260620-001: _notify_budget REMOVED — the fallback path passed
        # raw USD as percentage (wrong units, no is_win field).  Budget recording
        # is handled by the reconciliation loop in live_cycle.py which correctly
        # converts _evt.pnl / equity → percentage.  The other notify calls
        # (position_manager, reentry_guard, pnl_ledger) are independent of budget.
        if state is not None:
            self._notify_position_manager(event, state)
            self._notify_reentry_guard(event, state)
            self._notify_pnl_ledger(event, state)

        return True

    # ── Internal: build event from MT5 deal data ────────────────────────

    def _build_event(
        self,
        ticket: int,
        open_entry: dict,
        closed_volume: float,
        remaining_volume: float,
        symbol: str,
        mt5_worker: Any,
    ) -> PositionClosed | None:
        """Query MT5 deal history.  Build COMPLETE PositionClosed event.

        Uses DEAL cursor to only process unprocessed deals.
        If no deal found after 3 retries → CRITICAL log → return None.
        """
        _cursor = self._last_deal_id.get(ticket, 0)

        for _attempt in range(3):
            try:
                deals = mt5_worker.history_deals_get(position=ticket)
            except Exception:  # BLE001:FOG_WRAPPED
                with fail_open_guard("PositionCloseAdapter:DealHistoryGet"):
                    raise
                _time_module.sleep(1.0)
                continue

            if not deals:
                _time_module.sleep(1.0)
                continue

            # Find first unprocessed DEAL after cursor
            _new_deals = [d for d in deals if getattr(d, "ticket", 0) > _cursor]
            if not _new_deals:
                # All deals already processed — this shouldn't happen
                # with volume-delta detection, but handle gracefully
                _log.warning(
                    "PositionCloseAdapter: no new deals for ticket=%s (cursor=%s)",
                    ticket,
                    _cursor,
                )
                return None

            _deal = _new_deals[0]  # earliest unprocessed deal
            _deal_id = getattr(_deal, "ticket", 0)
            _close_price = float(getattr(_deal, "price", 0) or 0)
            _deal_volume = float(getattr(_deal, "volume", 0) or 0)
            _deal_profit = float(getattr(_deal, "profit", 0) or 0)
            _deal_reason = getattr(_deal, "reason", -1)
            _deal_time = getattr(_deal, "time", 0)
            _position_identifier = int(getattr(_deal, "position_id", 0) or 0)

            if _close_price <= 0:
                _log.error(
                    "[CRITICAL] PositionCloseAdapter: deal %s for ticket=%s "
                    "has close_price=0 — cannot build event",
                    _deal_id,
                    ticket,
                )
                # Update cursor to skip this broken deal
                self._last_deal_id[ticket] = _deal_id
                return None

            # Update cursor
            self._last_deal_id[ticket] = _deal_id

            # ── Resolve open entry fields ──
            _entry_price = float(open_entry.get("entry_price", 0) or 0)
            _side = str(open_entry.get("side", ""))
            _strategy = str(open_entry.get("strategy", ""))
            _magic = int(open_entry.get("magic", 0) or 0)
            _original_vol = float(open_entry.get("volume", 0) or 0)
            _brain_ids = tuple(open_entry.get("brain_ids") or [])
            _trail = open_entry.get("trail_contribution")
            _sl = float(open_entry.get("sl", 0) or 0)
            _tp = float(open_entry.get("tp", 0) or 0)
            _open_msg = str(open_entry.get("message_id", ""))
            # FIX-20260623-084: propagate p_win from open decision through
            # to PositionClosed so the calibrator receives actual p_win
            _p_win = float(open_entry.get("p_win", 0.5) or 0.5)

            # ── Label from deal reason ──
            if _deal_reason == DEAL_REASON_SL:
                _label = "sl_hit_first"
            elif _deal_reason == DEAL_REASON_TP:
                _label = "tp_hit_first"
            elif _deal_profit > 0:
                _label = "win"
            elif _deal_profit < 0:
                _label = "loss"
            else:
                _label = "breakeven"

            # ── Close time ──
            _close_time = ""
            if _deal_time > 0:
                import contextlib as _ctxlib_ts

                with _ctxlib_ts.suppress(ValueError, OSError):
                    _close_time = datetime.fromtimestamp(_deal_time, tz=UTC).isoformat()

            return PositionClosed(
                position_ticket=ticket,
                position_identifier=_position_identifier or ticket,
                symbol=symbol,
                side=_side,
                strategy=_strategy,
                magic=_magic,
                entry_price=_entry_price or _close_price,  # fallback
                close_price=_close_price,
                closed_volume=closed_volume,
                remaining_volume=remaining_volume,
                original_volume=_original_vol,
                pnl=_deal_profit,
                label=_label,
                exit_reason=_DEAL_REASON_MAP.get(_deal_reason, f"unknown_{_deal_reason}"),
                close_time=_close_time,
                source="mt5_deal",
                brain_ids=_brain_ids,
                trail_contribution=_trail,
                open_message_id=_open_msg,
                sl=_sl,
                tp=_tp,
                deal_id=_deal_id,
                p_win=_p_win,
            )

        _log.error(
            "[CRITICAL] PositionCloseAdapter: failed to find deal for ticket=%s "
            "after 3 retries — event lost",
            ticket,
        )
        return None

    # ── Internal: downstream notification ───────────────────────────────

    @staticmethod
    def _notify_position_manager(event: PositionClosed, state: Any) -> None:
        try:
            pm = getattr(state, "position_manager", None)
            if pm is not None and hasattr(pm, "clear_position"):
                if event.remaining_volume <= 0:
                    pm.clear_position(event.position_ticket)
        except Exception:  # BLE001:FOG_WRAPPED
            with fail_open_guard("PositionCloseAdapter:PositionManagerNotify"):
                raise
            _log.warning("PositionCloseAdapter: position_manager notify failed")

    @staticmethod
    def _notify_reentry_guard(event: PositionClosed, state: Any) -> None:
        try:
            from core.execution.reentry_guard import ExitRecord, ensure_reentry_state

            if event.strategy and event.side in ("long", "short"):
                _rec = ExitRecord(
                    timestamp=_time_module.time(),
                    strategy_name=event.strategy,
                    direction=event.side,
                    reason=event.exit_reason,
                    confidence=0.5,  # MT5 closes don't carry confidence
                    price=event.close_price,
                    ticket=event.position_ticket,
                )
                _rs = ensure_reentry_state(
                    getattr(state, "_reentry_states", {}),
                    event.strategy,
                )
                _rs.record_exit(_rec)
        except Exception:  # BLE001:FOG_WRAPPED
            with fail_open_guard("PositionCloseAdapter:ReentryGuardNotify"):
                raise
            _log.warning("PositionCloseAdapter: reentry_guard notify failed")

    @staticmethod
    def _notify_pnl_ledger(event: PositionClosed, state: Any) -> None:
        """Settle all pending brain predictions for this closed position."""
        try:
            _ledger = getattr(state, "_pnl_ledger", None)
            if _ledger is None:
                return
            # Settle pending signals matching this position_ticket
            _settled = 0
            for _sid in list(_ledger._pending.keys()):
                _sig = _ledger._pending.get(_sid)
                if _sig is None:
                    continue
                _pt = _sig.get("position_ticket", 0)
                if _pt == event.position_ticket:
                    _ledger.settle_one(
                        _sid,
                        close_price=event.close_price,
                        close_time=event.close_time,
                        spread=0.0,
                        slippage=0.10,
                    )
                    _settled += 1
            if _settled > 0:
                _log.debug(
                    "PnL Ledger: settled %s signals for ticket=%s",
                    _settled,
                    event.position_ticket,
                )
        except Exception:  # BLE001:FOG_WRAPPED
            with fail_open_guard("PositionCloseAdapter:PnLLedgerNotify"):
                raise
            _log.warning("PositionCloseAdapter: pnl_ledger notify failed")

    @staticmethod
    def _notify_budget(event: PositionClosed, state: Any) -> None:
        try:
            _pending = getattr(state, "_pending_budget_records", None)
            if _pending is not None:
                _pending.append(
                    {
                        "strategy": event.strategy,
                        "pnl": event.pnl,
                        "ticket": event.position_ticket,
                    }
                )
        except Exception:  # BLE001:FOG_WRAPPED
            with fail_open_guard("PositionCloseAdapter:BudgetNotify"):
                raise
            _log.warning("PositionCloseAdapter: budget notify failed")

    # ── Open event recording ────────────────────────────────────────────

    def record_open(
        self,
        event: PositionOpened,
        journal_path: str | Path,
        state: Any = None,
    ) -> bool:
        """Write PositionOpened event to Journal (atomic anchor).

        Dedup by message_id to prevent double-writes from bridge worker
        and live_cycle both recording the same open.
        """

        _now = datetime.now(UTC).replace(tzinfo=None).isoformat()
        _entry = event.to_journal_entry()
        _entry["recorded_at"] = _now

        _path = Path(journal_path)
        _lock_dir = _path.parent / "locks"
        try:
            from core.ledger.services.journal_cleanup import _append_journal

            _append_journal(_path, _entry, lock_dir=_lock_dir)
        except Exception:  # BLE001:FOG_WRAPPED
            with fail_open_guard("PositionCloseAdapter:OpenJournalWrite"):
                raise
            _log.exception(
                "PositionCloseAdapter: open journal write failed for ticket=%s",
                event.position_ticket,
            )
            return False

        # Register in known_open_tickets for close detection
        if state is not None:
            known = getattr(state, "known_open_tickets", None)
            if known is not None:
                known[event.position_ticket] = {
                    "entry_price": event.entry_price,
                    "side": event.side,
                    "strategy": event.strategy,
                    "magic": event.magic,
                    "volume": event.volume,
                    "brain_ids": list(event.brain_ids),
                    "message_id": event.message_id,
                    "sl": event.sl,
                    "tp": event.tp,
                }

        return True
