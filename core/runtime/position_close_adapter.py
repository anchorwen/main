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

import contextlib
import logging
import time as _time_module
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.contracts.position_events import PositionClosed, PositionOpened
from core.runtime.close_label import (
    resolve_close_label,
    resolve_close_reason_str,
    trail_active_from_sources,
)

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
    gate: Any = None,
) -> list[PositionClosed]:
    """Strangler Fig #11: reconcile MT5 positions, build events, record to Journal.

    Replaces the old _reconcile_closed_positions() + manual journal write block.
    Called from live_cycle.py reconciliation section.

    FIX-20260628-XXX: *gate* is a JournalGate instance for orphan prevention.
    """
    adapter = PositionCloseAdapter(
        tick_size=0.01 if "XAU" in symbol else 1.0,
    )
    events = adapter.detect_and_build(
        known_tickets=known_tickets,
        mt5_worker=mt5_worker,
        symbol=symbol,
        state=state,
    )
    for evt in events:
        adapter.record(evt, journal_path, state=state, gate=gate)
    return events


def record_mia_closes(
    mia_entries: list[dict],
    mt5_worker: Any,
    symbol: str,
    journal_path: str,
    state: Any = None,
    gate: Any = None,
) -> int:
    """Strangler Fig #12: record MIA-detected closes through the adapter.

    Replaces the old FileLock + manual journal append block.
    Returns count of successfully recorded events.

    FIX-20260628-XXX: *gate* is a JournalGate instance for orphan prevention.
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
            state=state,
        )
        if evt is not None and adapter.record(evt, journal_path, state=state, gate=gate):
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
    gate: Any = None,
) -> bool:
    """Strangler Fig #13: record a new position open through the adapter.

    Called from live_cycle.py at position_registered_for_mgmt point.

    FIX-20260628-XXX: *gate* is a JournalGate instance for orphan prevention.
    """
    try:
        adapter = PositionCloseAdapter(
            tick_size=0.01 if "XAU" in symbol else 1.0,
        )
        evt = PositionOpened(
            position_ticket=ticket,
            # FIX-20260708-001: at open the immutable identity equals the ticket
            # (MT5 assigns POSITION_IDENTIFIER = opening position ticket).
            position_identifier=ticket,
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
        return adapter.record_open(evt, journal_path, state=state, gate=gate)
    except (RuntimeError, ValueError, KeyError, TypeError, OSError):
        pass
    return False  # best-effort — never block position registration


UTC = UTC
_log = logging.getLogger(__name__)

# ── MT5 DEAL reason taxonomy — single source: core.runtime.close_label ──
# TECH_DEBT-007 (FIX-20260821-002): the local DEAL_REASON_SL/TP constants and
# _DEAL_REASON_MAP copy (adapter:144-159) plus the sibling copies in
# reconciliation/mia_close are DELETED.  resolve_close_label is the only
# decision point for close labels.

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
        state: Any = None,
    ) -> list[PositionClosed]:
        """Detect volume changes and build PositionClosed events.

        Args:
            known_tickets: {ticket: {entry_price, side, strategy, ...}}
            mt5_worker: MT5Worker instance
            symbol: e.g. "XAUUSDc"
            state: runtime state — DQAF-20260806-001 (FIX-2026XXXX-XXX): threaded
                into _build_event so the SL label can read position_manager
                trail_advances (Option A, mirrors reconciliation.py:198-204).

        Returns:
            List of PositionClosed events for this cycle.
        """
        events: list[PositionClosed] = []

        current_positions = []
        with contextlib.suppress(RuntimeError, ValueError, KeyError, TypeError, OSError):
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
                state=state,
            )
            if event is not None:
                events.append(event)

        return events

    def record(
        self,
        event: PositionClosed,
        journal_path: str | Path,
        state: Any = None,
        gate: Any | None = None,
    ) -> bool:
        """Write event to Journal (atomic anchor), then notify downstream.

        Returns True if written, False if duplicate (already recorded).

        FIX-20260626-143: *gate* is a JournalGate instance for orphan
        prevention.  Close events for untracked tickets are rejected
        before write.
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
        # ── FIX-20260716-005 §4: Provenance tagging ──
        # Mark adapter-mediated entries with _source so the journal dedup
        # can correctly supersede bridge-direct writes (which have
        # _source="bridge_position_already_closed_recovered") with more
        # accurate reconciliation data.  Without this tag, the dedup
        # returned True for all same-ticket close entries, blocking the
        # adapter from writing corrected data and from notifying downstream
        # consumers (position_manager, reentry_guard, pnl_ledger).
        _entry["_source"] = "mt5_reconciliation"

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
        try:
            from core.ledger.services.journal_cleanup import _append_journal

            _append_journal(_path, _entry, lock_dir=_lock_dir, gate=gate)
            _journal_ok = True
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):
            pass  # _journal_ok stays False

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
        state: Any = None,
    ) -> PositionClosed | None:
        """Query MT5 deal history.  Build COMPLETE PositionClosed event.

        Uses DEAL cursor to only process unprocessed deals.
        If no deal found after 3 retries → CRITICAL log → return None.

        state (DQAF-20260806-001, FIX-2026XXXX-XXX): lets the SL label read
        position_manager trail_advances (Option A, mirrors reconciliation.py:
        198-204).  Backward compatible — None ⇒ sl_hit_first (unchanged).
        """
        _cursor = self._last_deal_id.get(ticket, 0)

        for _attempt in range(3):
            try:
                deals = mt5_worker.history_deals_get(position=ticket)
            except (RuntimeError, ValueError, KeyError, TypeError, OSError):
                _time_module.sleep(1.0)
                continue

            if not deals:
                _time_module.sleep(1.0)
                continue

            # ── DQAF-20260708-003: authoritative exit-deal resolution (SSOT) ──
            # Previously this picked ``_new_deals[0]`` (the earliest deal after
            # the cursor).  Because the adapter is re-instantiated every cycle
            # the cursor is always 0, so ``[0]`` was the DEAL_ENTRY_IN opening
            # deal → close_price == entry_price, profit == 0 → every full close
            # was fabricated as a break-even.  resolve_exit_deal() enforces the
            # invariant "close comes from a DEAL_ENTRY_OUT deal" and NEVER falls
            # back to the entry deal's price.
            from core.runtime.deal_selection import resolve_exit_deal

            _res = resolve_exit_deal(deals, _cursor)
            # ``has_exit`` already guarantees ``close_price is not None`` (see
            # ExitResolution.has_exit); the explicit ``is None`` disjunct makes
            # that invariant visible to the type checker and is defensive in depth.
            if _res is None or not _res.has_exit or _res.close_price is None:
                _log.error(
                    "[CRITICAL] PositionCloseAdapter: no exit deal for ticket=%s "
                    "(cursor=%s, source=%s) — cannot build event",
                    ticket,
                    _cursor,
                    _res.close_price_source if _res is not None else "no_deals",
                )
                # Advance cursor past any inspected exit deal; retry next cycle.
                if _res is not None and _res.deal_id:
                    self._last_deal_id[ticket] = _res.deal_id
                return None

            _deal_id = _res.deal_id
            _close_price = float(_res.close_price)
            _deal_time = _res.close_time or 0
            _position_identifier = _res.position_id
            # DQAF-064 §1: deal comment for watchdog label preservation
            _deal_comment = _res.comment
            _close_price_source = _res.close_price_source

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

            # ── DQAF-20260708-003: PnL from broker-authoritative deal.profit,
            #    else price-based estimate; provenance recorded in _pnl_status ──
            if _res.close_pnl is not None:
                _deal_profit = float(_res.close_pnl)
                _pnl_status = "verified_from_mt5_deal"
            else:
                _ep_est = _entry_price or (_res.entry_fill_price or 0.0)
                if _ep_est > 0 and _side == "long":
                    _deal_profit = round((_close_price - _ep_est) * closed_volume, 2)
                    _pnl_status = "estimated_from_close_price"
                elif _ep_est > 0 and _side == "short":
                    _deal_profit = round((_ep_est - _close_price) * closed_volume, 2)
                    _pnl_status = "estimated_from_close_price"
                else:
                    _deal_profit = 0.0
                    _pnl_status = "pending_mt5_confirmation"

            # ── DQAF-20260806-001: trail-aware SL label (adapter path) ──
            # Mirror of reconciliation.py:198-204 (FIX-20260612-003).  That fix
            # landed on the restart-only reconciliation path (live_cycle.py:1503,
            # loop_iteration==1); the adapter is the ACTIVE journal writer
            # (live_cycle.py:1756), so its hardcoded sl_hit_first silently
            # relabelled every trailed SL exit since Strangler Fig #11
            # (FIX-20260611-005, 2026-06-11) — 0 sl_hit_trailed ever emitted.
            #
            # Primary source: position_manager trail_advances (position still
            # present at _build_event time — the position is only cleared in
            # _notify_position_manager AFTER record() writes the journal).
            # Unified trail-active predicate (TECH_DEBT-007 / FIX-20260821-002):
            # the SSOT ORs both telemetry sources — position_manager
            # trail_advances AND the detection-time trail_contribution dict
            # (ghost path, mia_close.py:89-92).  Pre-P6 reconciliation only
            # read position_manager and settlement_queue read neither, so the
            # same trailed SL could be labeled differently per producer.
            _pm_trail = 0
            if state is not None:
                _pm = getattr(state, "position_manager", None)
                if _pm is not None:
                    _pos = _pm.get_position(ticket) if hasattr(_pm, "get_position") else None
                    if _pos is not None:
                        _pm_trail = getattr(_pos, "trail_advances", 0)
            trail_active = trail_active_from_sources(
                _pm_trail,
                open_entry.get("trail_contribution"),
            )

            # ── Label from deal reason — SSOT (TECH_DEBT-007 / FIX-20260821-002) ──
            # resolve_close_label() is the single decision point for the whole
            # taxonomy: watchdog → SL(trail-aware) → TP → managed → broker →
            # honest unknown_close.  The pre-P6 chain above (DQAF-064 §1
            # watchdog / DQAF-20260722-002 managed) duplicated this logic in
            # four producers, which is exactly how it diverged.
            _label = resolve_close_label(
                _res.close_reason,
                _deal_comment,
                trail_active,
            )

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
                entry_price=_entry_price or _res.entry_fill_price or _close_price,  # fallback
                close_price=_close_price,
                closed_volume=closed_volume,
                remaining_volume=remaining_volume,
                original_volume=_original_vol,
                pnl=_deal_profit,
                label=_label,
                exit_reason=resolve_close_reason_str(_res.close_reason),
                close_time=_close_time,
                source="mt5_deal",
                brain_ids=_brain_ids,
                trail_contribution=_trail,
                open_message_id=_open_msg,
                sl=_sl,
                tp=_tp,
                deal_id=_deal_id,
                p_win=_p_win,
                close_price_source=_close_price_source,
                pnl_status=_pnl_status,
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
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):
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
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):
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
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):
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
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):
            _log.warning("PositionCloseAdapter: budget notify failed")

    # ── Open event recording ────────────────────────────────────────────

    def record_open(
        self,
        event: PositionOpened,
        journal_path: str | Path,
        state: Any = None,
        gate: Any | None = None,
    ) -> bool:
        """Write PositionOpened event to Journal (atomic anchor).

        Dedup by message_id to prevent double-writes from bridge worker
        and live_cycle both recording the same open.

        FIX-20260626-143: *gate* is a JournalGate instance for orphan
        prevention.  After writing the open entry, the ticket is
        registered with the gate.
        """

        _now = datetime.now(UTC).replace(tzinfo=None).isoformat()
        _entry = event.to_journal_entry()
        _entry["recorded_at"] = _now

        _path = Path(journal_path)
        _lock_dir = _path.parent / "locks"
        try:
            from core.ledger.services.journal_cleanup import _append_journal

            _append_journal(_path, _entry, lock_dir=_lock_dir, gate=gate)
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):
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
