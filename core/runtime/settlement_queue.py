"""Settlement Queue Isolation — physically decouples execution state from settlement state.

FIX-20260730-011 (L3 Architecture Fix): The journal PnL field had no Single Source of
Truth because the Bridge wrote estimated PnL and the Reconciliation adapter's correction
path was starved (position_manager cleared tickets before reconciliation ran).

This module implements the Settlement Queue Isolation pattern mandated by the
Institutional Risk Control & Architecture Committee:

  known_open_tickets  →  ACTIVE positions (engine manages: trail, watchdog, etc.)
       │
       ▼  (Bridge executes close → position gone from MT5)
  pending_settlement_tickets  →  AWAITING settlement (engine MUST NOT touch)
       │
       ▼  (Reconciliation verifies deal.profit via resolve_exit_deal())
  [REMOVED from queue]  →  SETTLED (journal updated with verified PnL)

Zombie handling: 4-tier timeout escalation (5min → 1hr → 24hr → terminal).
See ``SettlementQueue.settle()`` for the full state machine.
"""

from __future__ import annotations

import logging
import time as _time_module
from dataclasses import dataclass, field
from typing import Any

_log = logging.getLogger(__name__)

# ── Timeout tiers (seconds) ──
T1_NORMAL_POLL = 300  # 5 min — normal MT5 settlement window
T2_EXTENDED_POLL = 3600  # 60 min — extended polling, WARNING
T3_DEGRADED_WRITE = 86400  # 24 hr — write best estimate, CRITICAL alert
T4_TERMINAL_TIMEOUT = 86400  # 24 hr — terminal: remove from queue, CRITICAL alert

# ── Queue health threshold ──
MAX_PENDING_CRITICAL = 10  # >10 pending → CRITICAL alert (MT5 likely down)


@dataclass
class SettlementEntry:
    """One ticket awaiting MT5 deal settlement verification."""

    ticket: int
    symbol: str
    side: str  # "long" | "short"
    entry_price: float
    volume: float
    strategy: str = ""
    magic: int = 0
    brain_ids: list[str] = field(default_factory=list)
    open_message_id: str = ""
    # ── Best available estimate (from bridge detail, for timeout fallback) ──
    estimated_pnl: float | None = None
    estimated_close_price: float | None = None
    # ── Queue metadata ──
    queued_at: float = 0.0  # time.time() when queued
    queued_cycle: int = 0  # loop_iteration when queued
    last_poll_at: float = 0.0  # time.time() of last history_deals_get attempt
    poll_count: int = 0  # total poll attempts
    # ── State ──
    tier: int = 1  # current escalation tier (1-4)
    degraded_written: bool = False  # True after Tier-3 best-effort journal write
    terminal: bool = False  # True after Tier-4 terminal timeout


class SettlementQueue:
    """Manages the pending settlement lifecycle with zombie detection.

    Usage per cycle::

        sq = SettlementQueue()
        # Engine moves closed positions here:
        sq.enqueue(ticket=..., symbol=..., side=..., entry_price=..., ...)
        # Reconciliation attempts settlement:
        results = sq.settle_all(mt5_worker, symbol, journal_path, state)
        # Persist:
        sq.to_dict()  # → serialize to execution_state.json
    """

    def __init__(self) -> None:
        self._pending: dict[int, SettlementEntry] = {}

    # ── Public API ──────────────────────────────────────────────────────

    def enqueue(
        self,
        *,
        ticket: int,
        symbol: str,
        side: str,
        entry_price: float,
        volume: float,
        strategy: str = "",
        magic: int = 0,
        brain_ids: list[str] | None = None,
        open_message_id: str = "",
        estimated_pnl: float | None = None,
        estimated_close_price: float | None = None,
        cycle: int = 0,
    ) -> SettlementEntry:
        """Move a ticket from known_open_tickets into the settlement queue.

        MUST be called atomically after verifying the position is gone from MT5.
        Once queued, the engine MUST NOT manage this ticket (no trail, no watchdog).
        """
        entry = SettlementEntry(
            ticket=ticket,
            symbol=symbol,
            side=side,
            entry_price=entry_price,
            volume=volume,
            strategy=strategy,
            magic=magic,
            brain_ids=list(brain_ids or []),
            open_message_id=open_message_id,
            estimated_pnl=estimated_pnl,
            estimated_close_price=estimated_close_price,
            queued_at=_time_module.time(),
            queued_cycle=cycle,
        )
        self._pending[ticket] = entry
        _log.info(
            "[SettlementQueue] enqueued ticket=%s symbol=%s side=%s volume=%s — %s pending total",
            ticket,
            symbol,
            side,
            volume,
            len(self._pending),
        )
        self._check_queue_health()
        return entry

    def settle_all(
        self,
        mt5_worker: Any,
        symbol: str,
        journal_path: str,
        state: Any = None,
        gate: Any = None,
    ) -> list[dict[str, Any]]:
        """Attempt settlement for all pending tickets.

        Returns list of settlement result dicts for downstream processing
        (position_manager cleanup, budget recording, etc.).
        """
        results: list[dict[str, Any]] = []
        now = _time_module.time()

        for ticket in list(self._pending.keys()):
            entry = self._pending[ticket]
            entry.last_poll_at = now
            entry.poll_count += 1

            age_s = now - entry.queued_at

            # ── Update tier ──
            if age_s > T4_TERMINAL_TIMEOUT:
                entry.tier = 4
                entry.terminal = True
            elif age_s > T3_DEGRADED_WRITE:
                entry.tier = 3
            elif age_s > T2_EXTENDED_POLL:
                entry.tier = 2
            else:
                entry.tier = 1

            # ── Attempt deal resolution ──
            from core.runtime.deal_selection import resolve_exit_deal

            _res = None
            try:
                deals = mt5_worker.history_deals_get(position=ticket)
                if deals:
                    _res = resolve_exit_deal(deals, cursor=0)
            except (RuntimeError, ValueError, KeyError, TypeError, OSError):
                pass  # MT5 IPC failure — retry next cycle

            # ── Tier 4: Terminal timeout ──
            if entry.terminal:
                result = self._handle_terminal(entry, _res, journal_path, state, gate)
                self._pending.pop(ticket, None)
                results.append(result)
                continue

            # ── Settlement resolved? ──
            if _res is not None and _res.has_exit and _res.close_pnl is not None:
                result = self._handle_settled(entry, _res, journal_path, state, gate)
                self._pending.pop(ticket, None)
                results.append(result)
                continue

            # ── Tier 3: Degraded write (best estimate, keep polling) ──
            if entry.tier >= 3 and not entry.degraded_written:
                self._handle_degraded_write(entry, _res, journal_path, state, gate)
                entry.degraded_written = True

            # ── Log tier transitions ──
            if entry.tier >= 2:
                _log.warning(
                    "[SettlementQueue] ticket=%s age=%ds tier=%d polls=%d — still pending",
                    entry.ticket,
                    int(age_s),
                    entry.tier,
                    entry.poll_count,
                )
            if entry.tier >= 3:
                _log.error(
                    "[SettlementQueue] CRITICAL: ticket=%s age=%ds tier=%d — "
                    "MT5 deal settlement may be broken",
                    entry.ticket,
                    int(age_s),
                    entry.tier,
                )

        return results

    def remove(self, ticket: int) -> SettlementEntry | None:
        """Manually remove a ticket (e.g. operator resolution)."""
        return self._pending.pop(ticket, None)

    def get(self, ticket: int) -> SettlementEntry | None:
        return self._pending.get(ticket)

    def is_pending(self, ticket: int) -> bool:
        return ticket in self._pending

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    @property
    def tickets(self) -> list[int]:
        return sorted(self._pending.keys())

    # ── Persistence ─────────────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """Serialize for execution_state.json."""
        return {
            "entries": {
                str(t): {
                    "ticket": e.ticket,
                    "symbol": e.symbol,
                    "side": e.side,
                    "entry_price": e.entry_price,
                    "volume": e.volume,
                    "strategy": e.strategy,
                    "magic": e.magic,
                    "brain_ids": e.brain_ids,
                    "open_message_id": e.open_message_id,
                    "estimated_pnl": e.estimated_pnl,
                    "estimated_close_price": e.estimated_close_price,
                    "queued_at": e.queued_at,
                    "queued_cycle": e.queued_cycle,
                    "last_poll_at": e.last_poll_at,
                    "poll_count": e.poll_count,
                    "tier": e.tier,
                    "degraded_written": e.degraded_written,
                    "terminal": e.terminal,
                }
                for t, e in self._pending.items()
            }
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> SettlementQueue:
        """Restore from execution_state.json."""
        sq = cls()
        if not data or not isinstance(data, dict):
            return sq
        entries = data.get("entries", {})
        if not isinstance(entries, dict):
            return sq
        for ticket_str, e_data in entries.items():
            try:
                entry = SettlementEntry(
                    ticket=int(e_data.get("ticket", 0)),
                    symbol=str(e_data.get("symbol", "")),
                    side=str(e_data.get("side", "")),
                    entry_price=float(e_data.get("entry_price", 0)),
                    volume=float(e_data.get("volume", 0)),
                    strategy=str(e_data.get("strategy", "")),
                    magic=int(e_data.get("magic", 0)),
                    brain_ids=list(e_data.get("brain_ids", [])),
                    open_message_id=str(e_data.get("open_message_id", "")),
                    estimated_pnl=float(e_data["estimated_pnl"])
                    if e_data.get("estimated_pnl") is not None
                    else None,
                    estimated_close_price=float(e_data["estimated_close_price"])
                    if e_data.get("estimated_close_price") is not None
                    else None,
                    queued_at=float(e_data.get("queued_at", 0)),
                    queued_cycle=int(e_data.get("queued_cycle", 0)),
                    last_poll_at=float(e_data.get("last_poll_at", 0)),
                    poll_count=int(e_data.get("poll_count", 0)),
                    tier=int(e_data.get("tier", 1)),
                    degraded_written=bool(e_data.get("degraded_written", False)),
                    terminal=bool(e_data.get("terminal", False)),
                )
                sq._pending[entry.ticket] = entry
            except (KeyError, ValueError, TypeError):
                _log.warning("[SettlementQueue] skipped corrupt entry: %s", ticket_str)
        return sq

    # ── Internal handlers ───────────────────────────────────────────────

    def _handle_settled(
        self,
        entry: SettlementEntry,
        resolution: Any,  # ExitResolution
        journal_path: str,
        state: Any,
        gate: Any,
    ) -> dict[str, Any]:
        """Write verified PnL to journal, superseding bridge's pnl=null entry.

        Builds the journal entry directly from the pre-computed ExitResolution
        rather than re-querying MT5 through the adapter.  The resolution was
        already obtained by settle_all() via resolve_exit_deal().
        """
        from datetime import UTC, datetime

        _now = datetime.now(UTC).replace(tzinfo=None).isoformat()
        _close_pnl = float(resolution.close_pnl) if resolution.close_pnl is not None else None
        _close_price = float(resolution.close_price) if resolution.close_price is not None else None

        # ── Compute PnL if not directly available from deal.profit ──
        _pnl_status = "verified_from_mt5_deal"
        if _close_pnl is None and _close_price is not None and entry.entry_price > 0:
            if entry.side == "long":
                _close_pnl = round((_close_price - entry.entry_price) * entry.volume, 2)
            elif entry.side == "short":
                _close_pnl = round((entry.entry_price - _close_price) * entry.volume, 2)
            _pnl_status = "estimated_from_close_price"

        # ── Determine label from deal reason ──
        _reason = resolution.close_reason if resolution is not None else None
        _comment = str(resolution.comment) if resolution is not None else ""
        if _reason == 4:  # DEAL_REASON_SL
            _label = "sl_hit_first"
        elif _reason == 5:  # DEAL_REASON_TP
            _label = "tp_hit_first"
        elif _comment:
            _label = f"managed:{_comment[:80]}"
        else:
            _label = (
                "broker:client_close" if _reason in (0, 1, 2, 3) else f"broker:reason_{_reason}"
            )

        # ── Close time ──
        _close_time = ""
        if resolution is not None and resolution.close_time is not None:
            import contextlib as _ctxlib_ts

            _ct = float(resolution.close_time)
            if _ct > 0:
                with _ctxlib_ts.suppress(ValueError, OSError):
                    _close_time = datetime.fromtimestamp(_ct, tz=UTC).isoformat()

        journal_entry = {
            "schema_version": "live_trade_journal.v2",
            "recorded_at": _now,
            "message_id": f"settlement_verified_{entry.ticket}",
            "target": "settlement_queue",
            "ack_status": "closed",
            "symbol": entry.symbol,
            "action": "close",
            "side": entry.side,
            "volume": entry.volume,
            "pnl": _close_pnl,
            "_pnl_status": _pnl_status,
            "_close_price_source": (
                "mt5_exit_deal"
                if resolution is not None and resolution.has_exit
                else "no_exit_deal"
            ),
            "label": _label,
            "exit_reason": "settlement_verified",
            "close_time": _close_time,
            "entry_price": entry.entry_price,
            "exit_price": _close_price,
            "comment": f"Settlement verified after {entry.poll_count} polls",
            "strategy": entry.strategy,
            "magic": entry.magic,
            "position_ticket": entry.ticket,
            "position_identifier": entry.ticket,
            "brain_ids": entry.brain_ids,
            "open_message_id": entry.open_message_id,
            "_source": "mt5_reconciliation",
            "_settlement_polls": entry.poll_count,
            "_settlement_age_s": int(_time_module.time() - entry.queued_at),
        }

        from core.ledger.services.journal_cleanup import _append_journal

        try:
            from pathlib import Path

            _path = Path(journal_path)
            _lock_dir = _path.parent / "locks"
            _append_journal(_path, journal_entry, lock_dir=_lock_dir, gate=gate)
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):
            _log.exception("[SettlementQueue] journal write failed for ticket=%s", entry.ticket)
            return {
                "ticket": entry.ticket,
                "status": "settled_journal_failed",
                "pnl": _close_pnl,
                "event": None,
            }

        _log.info(
            "[SettlementQueue] SETTLED ticket=%s pnl=%s age=%ds polls=%d",
            entry.ticket,
            _close_pnl,
            int(_time_module.time() - entry.queued_at),
            entry.poll_count,
        )

        # Build a lightweight result for downstream processing
        return {
            "ticket": entry.ticket,
            "status": "settled",
            "pnl": _close_pnl,
            "event": type(
                "SettlementEvent",
                (),
                {
                    "position_ticket": entry.ticket,
                    "pnl": _close_pnl,
                    "label": _label,
                    "strategy": entry.strategy,
                    "side": entry.side,
                    "close_price": _close_price,
                    "entry_price": entry.entry_price,
                    "closed_volume": entry.volume,
                    "brain_ids": entry.brain_ids,
                    "open_message_id": entry.open_message_id,
                    "exit_reason": "settlement_verified",
                    "close_time": _close_time,
                    "p_win": 0.5,
                    "remaining_volume": 0.0,
                },
            )(),
        }

    def _handle_degraded_write(
        self,
        entry: SettlementEntry,
        resolution: Any | None,
        journal_path: str,
        state: Any,
        gate: Any,
    ) -> None:
        """Tier 3: Write best-effort estimate with timeout provenance."""
        from datetime import UTC, datetime

        _now = datetime.now(UTC).replace(tzinfo=None).isoformat()
        _best_pnl = entry.estimated_pnl
        _best_cp = entry.estimated_close_price

        # Try to get a better estimate from the resolution
        if resolution is not None and resolution.close_pnl is not None:
            _best_pnl = float(resolution.close_pnl)
        if resolution is not None and resolution.close_price is not None:
            _best_cp = float(resolution.close_price)

        journal_entry = {
            "schema_version": "live_trade_journal.v2",
            "recorded_at": _now,
            "message_id": f"settlement_timeout_{entry.ticket}",
            "target": "settlement_queue",
            "ack_status": "closed",
            "symbol": entry.symbol,
            "action": "close",
            "side": entry.side,
            "volume": entry.volume,
            "pnl": _best_pnl,
            "_pnl_status": "settlement_timeout",
            "_close_price_source": (
                "mt5_exit_deal"
                if (resolution is not None and resolution.has_exit)
                else "no_exit_deal"
            ),
            "label": "settlement_timeout",
            "comment": (
                f"Tier-3 degraded write: {entry.poll_count} polls over "
                f"{int(_time_module.time() - entry.queued_at)}s, no verified deal.profit"
            ),
            "strategy": entry.strategy,
            "magic": entry.magic,
            "position_ticket": entry.ticket,
            "position_identifier": entry.ticket,
            "brain_ids": entry.brain_ids,
            "_source": "settlement_queue_timeout",
            "exit_price": _best_cp,
            "entry_price": entry.entry_price,
            "_settlement_age_s": int(_time_module.time() - entry.queued_at),
            "_settlement_polls": entry.poll_count,
        }

        from core.ledger.services.journal_cleanup import _append_journal

        try:
            from pathlib import Path

            _path = Path(journal_path)
            _lock_dir = _path.parent / "locks"
            _append_journal(_path, journal_entry, lock_dir=_lock_dir, gate=gate)
            _log.warning(
                "[SettlementQueue] DEGRADED WRITE ticket=%s best_pnl=%s age=%ds",
                entry.ticket,
                _best_pnl,
                int(_time_module.time() - entry.queued_at),
            )
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):
            _log.exception("[SettlementQueue] degraded write failed for ticket=%s", entry.ticket)

        # Emit structured alert for operations
        import json as _json

        print(
            _json.dumps(
                {
                    "event": "settlement_timeout_degraded",
                    "severity": "CRITICAL",
                    "ticket": entry.ticket,
                    "symbol": entry.symbol,
                    "age_s": int(_time_module.time() - entry.queued_at),
                    "polls": entry.poll_count,
                    "best_pnl": _best_pnl,
                    "best_close_price": _best_cp,
                    "time": _now,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    def _handle_terminal(
        self,
        entry: SettlementEntry,
        resolution: Any | None,
        journal_path: str,
        state: Any,
        gate: Any,
    ) -> dict[str, Any]:
        """Tier 4: Terminal timeout — final write, remove from queue, CRITICAL alert."""
        from datetime import UTC, datetime

        _now = datetime.now(UTC).replace(tzinfo=None).isoformat()
        _best_pnl = entry.estimated_pnl
        _best_cp = entry.estimated_close_price

        if resolution is not None and resolution.close_pnl is not None:
            _best_pnl = float(resolution.close_pnl)
        if resolution is not None and resolution.close_price is not None:
            _best_cp = float(resolution.close_price)

        journal_entry = {
            "schema_version": "live_trade_journal.v2",
            "recorded_at": _now,
            "message_id": f"settlement_terminal_{entry.ticket}",
            "target": "settlement_queue",
            "ack_status": "closed",
            "symbol": entry.symbol,
            "action": "close",
            "side": entry.side,
            "volume": entry.volume,
            "pnl": _best_pnl,
            "_pnl_status": "settlement_timeout_terminal",
            "_close_price_source": (
                "mt5_exit_deal"
                if (resolution is not None and resolution.has_exit)
                else "no_exit_deal"
            ),
            "label": "settlement_timeout_terminal",
            "comment": (
                f"TERMINAL: {entry.poll_count} polls over "
                f"{int(_time_module.time() - entry.queued_at)}s, "
                f"MT5 deal settlement failed permanently"
            ),
            "strategy": entry.strategy,
            "magic": entry.magic,
            "position_ticket": entry.ticket,
            "position_identifier": entry.ticket,
            "brain_ids": entry.brain_ids,
            "_source": "settlement_queue_terminal",
            "exit_price": _best_cp,
            "entry_price": entry.entry_price,
            "_settlement_age_s": int(_time_module.time() - entry.queued_at),
            "_settlement_polls": entry.poll_count,
        }

        from core.ledger.services.journal_cleanup import _append_journal

        try:
            from pathlib import Path

            _path = Path(journal_path)
            _lock_dir = _path.parent / "locks"
            _append_journal(_path, journal_entry, lock_dir=_lock_dir, gate=gate)
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):
            _log.exception("[SettlementQueue] terminal write failed for ticket=%s", entry.ticket)

        # CRITICAL alert — this should never happen in normal operations
        import json as _json

        print(
            _json.dumps(
                {
                    "event": "settlement_timeout_terminal",
                    "severity": "CRITICAL",
                    "ticket": entry.ticket,
                    "symbol": entry.symbol,
                    "age_s": int(_time_module.time() - entry.queued_at),
                    "polls": entry.poll_count,
                    "best_pnl": _best_pnl,
                    "best_close_price": _best_cp,
                    "forensic_snapshot": {
                        "side": entry.side,
                        "entry_price": entry.entry_price,
                        "volume": entry.volume,
                        "strategy": entry.strategy,
                        "queued_at_iso": datetime.fromtimestamp(
                            entry.queued_at, tz=UTC
                        ).isoformat(),
                        "resolution": (
                            {
                                "has_exit": resolution.has_exit,
                                "close_price": resolution.close_price,
                                "close_pnl": resolution.close_pnl,
                                "n_exit_deals": resolution.n_exit_deals,
                                "close_price_source": resolution.close_price_source,
                            }
                            if resolution is not None
                            else "no_resolution"
                        ),
                    },
                    "time": _now,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

        _log.error(
            "[SettlementQueue] TERMINAL ticket=%s — removed after %d polls / %ds",
            entry.ticket,
            entry.poll_count,
            int(_time_module.time() - entry.queued_at),
        )

        return {
            "ticket": entry.ticket,
            "status": "terminal_timeout",
            "pnl": _best_pnl,
            "event": None,
        }

    def _check_queue_health(self) -> None:
        """Alert if pending queue is abnormally large (MT5 connectivity likely lost)."""
        if len(self._pending) > MAX_PENDING_CRITICAL:
            import json as _json

            _log.error(
                "[SettlementQueue] QUEUE_HEALTH_CRITICAL: %d pending settlements — "
                "MT5 connectivity may be lost",
                len(self._pending),
            )
            print(
                _json.dumps(
                    {
                        "event": "settlement_queue_health_critical",
                        "severity": "CRITICAL",
                        "pending_count": len(self._pending),
                        "oldest_age_s": int(
                            _time_module.time() - min(e.queued_at for e in self._pending.values())
                        ),
                        "tickets": sorted(self._pending.keys()),
                        "time": _time_module.time(),
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
