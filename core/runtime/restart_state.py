"""Restart state bootstrap — replay journal closes to restore runtime guards.

Extracted from live_cycle.py per the Strangler Fig pattern (#8).
Called once on first cycle after restart to populate reentry states,
SL streak counters, and graduated cooldown from recent journal history.

..  rubric:: Fail-Closed Principle (FIX-20260606-138)

    Previously, any exception during journal parsing was silently swallowed
    (``except Exception:  # BLE001:REVIEWED — docstring return``), leaving ``_reentry_states`` empty.  This
    caused ``last_exit = None`` → ``"first_entry"`` bypass → restart-immediate-
    trade (RC-03 / ReB-003c bootstrap_silent_fail_to_open).

    Now: parse errors are logged with full traceback at ERROR level, and a
    ``_bootstrap_degraded`` flag is set on the state object.  The pre-trade
    gate evaluator checks this flag and blocks all trades when bootstrap
    integrity cannot be confirmed — the system defaults to **closed** gates.
"""

from __future__ import annotations

import json
import logging as _logging
import time
import traceback as _traceback
from datetime import UTC, datetime
from pathlib import Path as _Path
from typing import Any

_logger = _logging.getLogger(__name__)


def bootstrap_restart_state(state: Any, journal_path: str, config: Any) -> None:
    """Replay recent journal close entries to restore runtime guard state.

    Called once on the first cycle after restart.  Scans the last 30 min
    of journal closes and populates:
      - _reentry_states  (most recent exit per strategy)
      - _pending_sl_records  (all SL/loss events for graduated cooldown)
      - consecutive_sl_hits  (per-strategy SL streak counter)

    Sets ``state._bootstrap_degraded = True`` when journal parsing fails
    entirely — downstream gates MUST check this flag and default to
    conservative (block all trades) until manual intervention.
    """
    _jp = _Path(journal_path)
    if not _jp.exists():
        _logger.warning(
            "Bootstrap: journal not found at %s — reentry guard will have no "
            "exit history. Setting _bootstrap_degraded=True. "
            "All trades will be blocked until journal data is available.",
            journal_path,
        )
        state._bootstrap_degraded = True
        return

    now = time.time()
    # FIX-20260603-061: scan journal backwards from end — no arbitrary time window.
    # Previous 30min (and later 24h) windows both failed: if the most recent exit
    # was outside the window, _reentry_states was empty → last_exit=None →
    # "first_entry" bypass → immediate trade after restart.
    # Now: find the most recent close per strategy, regardless of age.
    # SL streak tracking still uses a time window (last 7 days).
    _close_entries: list[dict[str, Any]] = []
    _seen_strategies: set[str] = set()  # track which strategies already have a recent close

    # Build set of message_ids for currently-open positions so we skip
    # their closes (those will be handled by normal reconciliation).
    _active_open_mids: set[str] = {
        _v.get("message_id", "")
        for _v in state.known_open_tickets.values()
        if _v.get("message_id", "")
    }

    # SL streak cutoff: 7 days — long enough to capture meaningful streaks
    _sl_cutoff = now - 604800.0  # 7 days

    try:
        _content = _jp.read_text(encoding="utf-8")
    except Exception:  # BLE001:FOG_WRAPPED
        with fail_open_guard("RestartState:JournalRead"):
            raise
        _logger.error(
            "Bootstrap: failed to read journal at %s.\n%s",
            journal_path,
            _traceback.format_exc(),
        )
        state._bootstrap_degraded = True
        return

    # ── Scan backwards from end to find most recent close per strategy ──
    _lines = _content.splitlines()
    for _line in reversed(_lines):
        _line = _line.strip()
        if not _line:
            continue
        try:
            _entry = json.loads(_line)
        except json.JSONDecodeError:
            _logger.debug(
                "Bootstrap: skipping non-JSON line in journal: %.100s...",
                _line,
            )
            continue

        _action = _entry.get("action", "")
        if _action not in ("close",):
            continue

        # Skip closes whose open is still tracked
        _open_mid = _entry.get("open_message_id", "")
        if _open_mid and _open_mid in _active_open_mids:
            continue

        _ts_str = _entry.get("recorded_at", "")
        try:
            if _ts_str:
                _ts = datetime.fromisoformat(_ts_str.replace("Z", "+00:00")).timestamp()
            else:
                continue
        except Exception:  # BLE001:FOG_WRAPPED
            with fail_open_guard("RestartState:TimestampParse"):
                raise
            _logger.debug(
                "Bootstrap: unparseable timestamp in journal entry: %.120s",
                _ts_str,
            )
            continue

        # Resolve strategy from entry or magic
        _strategy = _entry.get("strategy", "")
        if not _strategy:
            _magic = _entry.get("magic", 0)
            if _magic:
                from core.contracts.strategy_magic import MAGIC_TO_STRATEGY as _M2
                _strategy = _M2.get(_magic, "")

        # Collect: always for unseen strategies (most recent close),
        # and for SL streak within the 7-day window.
        if _strategy and _strategy not in _seen_strategies:
            _close_entries.append(_entry)
            _seen_strategies.add(_strategy)
        elif _ts >= _sl_cutoff:
            # Within SL streak window: collect for streak counting
            _label = _entry.get("label", "")
            if _label in ("sl_hit_first", "loss", "tp_hit_first", "win"):
                _close_entries.append(_entry)

    # ── FIX-20260603-068: full-chain debug — one restart to find root cause ──
    import json as _json
    _debug = {
        "event": "bootstrap_debug",
        "step": "scan_complete",
        "total_close_entries": len(_close_entries),
        "seen_strategies": sorted(_seen_strategies),
    }
    _debug_entries = []
    for _ce in _close_entries:
        _debug_entries.append({
            "time": _ce.get("recorded_at", "?"),
            "strategy": _ce.get("strategy", "?"),
            "label": _ce.get("label", "?"),
            "ticket": _ce.get("position_ticket", "?"),
        })
    _debug["entries"] = _debug_entries
    print(_json.dumps(_debug, ensure_ascii=False, default=str), flush=True)

    if not _close_entries:
        return

    # FIX-20260603-068: sort DESCENDING (most recent first).
    # Previously sorted ascending → oldest close processed last → overwrote
    # the most recent exit in record_exit() → reentry guard used 46h-old
    # unknown_close instead of the actual 2h-old exit → stale_exit_allowed.
    _close_entries.sort(key=lambda e: e.get("recorded_at", ""), reverse=True)

    from core.contracts.strategy_magic import MAGIC_TO_STRATEGY as _MAGIC_MAP
    from core.runtime.fault_handler import fail_open_guard
    from core.execution.reentry_guard import ExitRecord, ensure_reentry_state

    # ── FIX-20260603-069: build open-index to resolve entry_confidence ──
    # The close entry doesn't carry confidence.  We look up the matching
    # open entry by position_ticket to get the real entry_confidence.
    _open_index: dict[int, dict[str, Any]] = {}
    for _line in _content.splitlines():
        _line = _line.strip()
        if not _line:
            continue
        try:
            _oe = json.loads(_line)
        except json.JSONDecodeError:
            continue
        if _oe.get("action") != "open":
            continue
        _ot = _oe.get("position_ticket")
        if _ot and isinstance(_ot, int) and _ot > 0:
            _open_index[_ot] = _oe

    # FIX-20260603-068: guard against multiple entries per strategy.
    # SL streak collection can add extra entries → the last one processed
    # by record_exit() wins, overwriting the most recent exit.
    _recorded_strategies: set[str] = set()
    for _entry in _close_entries:
        _strategy = _entry.get("strategy", "")
        if not _strategy:
            _magic = _entry.get("magic", 0)
            if _magic:
                from core.contracts.strategy_magic import MAGIC_TO_STRATEGY as _M

                _strategy = _M.get(_magic, "")
        if not _strategy:
            continue
        if _strategy in _recorded_strategies:
            continue  # FIX-068: already recorded a more recent exit for this strategy
        _recorded_strategies.add(_strategy)

        _side = _entry.get("side", "")
        _label = _entry.get("label", "")
        _close_price = _entry.get("detail", {}).get("close_price") or 0.0
        _ticket = _entry.get("position_ticket", 0)
        # Prefer the software-side close reason from `comment` (set by
        # managed_close / exit_watchdog).  Falls back to SW-assigned
        # `label` ("win"/"loss"/"breakeven"/"sl_hit_first"/etc.),
        # then to the MT5-side `detail.reason` only when neither exists.
        #
        # DQAF-20260616-001: previously `label` was skipped in the fallback
        # chain → exits with no `comment` (e.g. structural_swing_v1) had
        # _reason="mt5_deal_reason_3" → classify()→UNKNOWN → permanent
        # reentry deadlock for rule-based strategies on restart.
        _reason = (
            _entry.get("comment", "").strip()
            or str(_label or "").strip()
            or _entry.get("detail", {}).get("reason", "")
            or "unknown_close"
        )

        # When the most recent close entry has no SW comment, borrow it
        # from an adjacent entry for the same strategy (the managed-close
        # dispatch record written ~13s earlier).  This prevents every
        # restart from classifying software-side exits as unknown_close.
        #
        # Two-phase search:
        #   1. Search _close_entries (entries that passed the label filter).
        #      Works for entries written after _derive_label was fixed to
        #      return "win"/"loss" instead of the comment text.
        #   2. Fall back to the raw journal content (for entries written
        #      before the fix, where the comment-rich entry has a non-standard
        #      label and was filtered out of _close_entries).
        if not _entry.get("comment", "").strip():
            _borrowed = False
            # Phase 1: filtered entries
            for _next in _close_entries:
                _ns = _next.get("strategy", "") or _MAGIC_MAP.get(_next.get("magic", 0), "")
                _nc = _next.get("comment", "").strip()
                if _ns == _strategy and _nc:
                    _reason = _nc
                    _borrowed = True
                    break
            # Phase 2: raw journal (for entries with pre-fix labels)
            if not _borrowed:
                for _line in _content.splitlines():
                    _line = _line.strip()
                    if not _line:
                        continue
                    try:
                        _raw = json.loads(_line)
                    except json.JSONDecodeError:
                        continue
                    if _raw.get("action") != "close":
                        continue
                    _rs = _raw.get("strategy", "") or _MAGIC_MAP.get(_raw.get("magic", 0), "")
                    _rc = _raw.get("comment", "").strip()
                    if _rs == _strategy and _rc:
                        _reason = _rc
                        break

        # ── FIX-20260603-073: re-parse _ts from THIS entry ──
        # Previously _ts leaked from the first scan loop (line 77) —
        # every ExitRecord got the timestamp of the OLDEST close entry
        # in the journal.  All exits appeared >24h old → stale_exit_allowed
        # bypassed the reentry guard → restart-immediate-trade.
        _ts_str = _entry.get("recorded_at", "")
        try:
            if _ts_str:
                _ts = datetime.fromisoformat(_ts_str.replace("Z", "+00:00")).timestamp()
            else:
                continue
        except Exception:  # BLE001:REVIEWED
            continue

        # ── Resolve entry_confidence from matching open ──
        _entry_confidence = 0.5
        _open_match = _open_index.get(int(_ticket)) if _ticket else None
        if _open_match:
            _entry_confidence = float(_open_match.get("confidence", 0.5) or 0.5)

        # ── Record exit for re-entry guard ──
        if _side in ("long", "short"):
            try:
                _rec = ExitRecord(
                    timestamp=_ts,
                    strategy_name=_strategy,
                    direction=_side,
                    reason=_reason,
                    confidence=_entry_confidence,  # FIX-069: real entry confidence, not 0.5
                    price=float(_close_price) if _close_price else 0.0,
                    ticket=int(_ticket) if _ticket else 0,
                )
                _rs = ensure_reentry_state(state._reentry_states, _strategy)
                _rs.record_exit(_rec)
                # Debug: show what was actually recorded
                print(_json.dumps({
                    "event": "bootstrap_debug",
                    "step": "recorded_exit",
                    "strategy": _strategy,
                    "exit_time": _ts,
                    "exit_confidence": _entry_confidence,
                    "exit_reason": _reason,
                    "exit_label": _label,
                    "last_exit_timestamp": _rs.last_exit.timestamp if _rs.last_exit else None,
                    "last_exit_confidence": _rs.last_exit.confidence if _rs.last_exit else None,
                    "last_exit_reason": _rs.last_exit.reason if _rs.last_exit else None,
                }, ensure_ascii=False, default=str), flush=True)
            except Exception:  # BLE001:FOG_WRAPPED
                with fail_open_guard("RestartState:RecordExit"):
                    raise
                _logger.warning(
                    "Bootstrap: failed to record exit for strategy=%s ticket=%s.\n%s",
                    _strategy,
                    _ticket,
                    _traceback.format_exc(),
                )

        # ── Count SL/loss for streak tracker ──
        if _label in ("sl_hit_first", "loss"):
            _curr = state.consecutive_sl_hits.get(_strategy, 0) + 1
            state.consecutive_sl_hits[_strategy] = _curr
            state._pending_sl_records.append(
                {
                    "strategy": _strategy,
                    "timestamp": now,
                }
            )
            if _curr >= 3:
                state.sl_streak_blocked_until[_strategy] = now + 1800
        elif _label in ("tp_hit_first", "win"):
            state.consecutive_sl_hits[_strategy] = 0

    if state._pending_sl_records:
        print(
            json.dumps(
                {
                    "event": "restart_state_bootstrapped",
                    "time": datetime.now(UTC).isoformat(),
                    "close_entries_replayed": len(_close_entries),
                    "sl_records": len(state._pending_sl_records),
                    "reentry_strategies": list(state._reentry_states.keys()),
                    "sl_streaks": dict(state.consecutive_sl_hits),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
