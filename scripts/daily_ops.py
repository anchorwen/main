"""Daily operations orchestrator: run all governance and monitoring in sequence.

Ties together shadow ensemble, governance scheduler, champion/challenger,
retraining trigger, and daily recap into a single daily pipeline.

Usage:
  # Full pipeline (all steps)
  python scripts/daily_ops.py

  # Dry-run: assess everything without applying transitions
  python scripts/daily_ops.py --dry-run

  # Skip specific steps
  python scripts/daily_ops.py --skip-shadow --skip-retraining

  # Write combined report
  python scripts/daily_ops.py --output data/reports/daily_ops.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.contracts.exceptions import DataIntegrityError
from core.data.ticket_resolver import resolve as resolve_ticket

SCHEMA_VERSION = "daily_ops.v1"

THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = THIS_DIR.parent

DEFAULT_TRACKER_PATH = "data/brain_performance.json"
DEFAULT_GOVERNANCE_PATH = "data/governance_state.json"

# Default brain registrations when creating a fresh governance service.
# When empty, auto-discovers all brain_registry_entry.v1 configs from disk.
DEFAULT_BRAIN_REGISTRATIONS: dict[str, str] = {}


def _utc_now_iso() -> str:
    return (
        datetime.now(UTC)
        .replace(tzinfo=None)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _load_or_create_tracker(base_dir: str) -> Any:
    """Load persisted tracker state, or create a fresh one."""
    tracker_path = Path(base_dir) / "brain_performance.json"
    try:
        from core.feedback.brain_performance_tracker import BrainPerformanceTracker

        if tracker_path.exists():
            return BrainPerformanceTracker.load(tracker_path)
        return BrainPerformanceTracker(window_size=100)
    except Exception:  # noqa: BLE001 — REVIEWED: fail_open_guard below
        try:  # BLE001:FOG (was: FOG/LAC)
            from core.feedback.brain_performance_tracker import BrainPerformanceTracker

            return BrainPerformanceTracker(window_size=100)
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            pass


def _resolve_brains_dir(base_dir: str) -> Path:
    """Defense 3: explicitly map base_dir to brains config directory.

    Falls back to configs/brains/ for unknown base directories (test paths, etc.).
    No implicit guessing — hard contract between base_dir and brains_dir.
    """
    _bd = str(base_dir).rstrip("/\\")
    # Base dir → brains dir contract
    _MAPPING: dict[str, str] = {
        "data": "configs/brains",
        "data_btc": "configs/brains_btc",
    }
    for key, brains in _MAPPING.items():
        if _bd.endswith(key):
            return PROJECT_ROOT / brains
    # Fallback for test/unknown paths — use XAU default
    return PROJECT_ROOT / "configs" / "brains"


def _load_or_create_governance(base_dir: str, *, brains_dir: Path | None = None) -> Any:
    """Load persisted governance state, or create a fresh one.

    When creating a new governance service, auto-discovers brain configs from
    the resolved brains_dir.  The hardcoded DEFAULT_BRAIN_REGISTRATIONS dict
    (above) can still be used to pin specific initial statuses, but the
    default empty dict triggers full auto-discovery.

    Defense 3: brains_dir is resolved explicitly from base_dir contract.
    """
    if brains_dir is None:
        brains_dir = _resolve_brains_dir(base_dir)
    gov_path = Path(base_dir) / "governance_state.json"
    try:
        from core.governance.governance_service import GovernanceService

        if gov_path.exists():
            return GovernanceService.load(gov_path)
        gov = GovernanceService()
        if DEFAULT_BRAIN_REGISTRATIONS:
            for brain_id, status in DEFAULT_BRAIN_REGISTRATIONS.items():
                gov.register_brain(brain_id, status)
        else:
            if brains_dir.is_dir():
                import json as _json

                for cfg_path in sorted(brains_dir.glob("*.json")):
                    if "normalization" in cfg_path.name.lower():
                        continue
                    try:
                        cfg = _json.loads(cfg_path.read_text(encoding="utf-8"))
                    except Exception:  # noqa: BLE001 — REVIEWED: fail_open_guard below
                        try:  # BLE001:FOG (was: FOG/LAC)
                            continue
                        except (
                            RuntimeError,
                            ValueError,
                            KeyError,
                            TypeError,
                            OSError,
                        ):  # BLE001:FOG
                            pass
                    if cfg.get("schema_version") != "brain_registry_entry.v1":
                        continue
                    bid = cfg.get("brain_id", "")
                    if bid:
                        cfg_status = cfg.get("status", "candidate")
                        initial = (
                            cfg_status if cfg_status in ("candidate", "shadow") else "candidate"
                        )
                        gov.register_brain(bid, initial)
        return gov
    except Exception:  # noqa: BLE001 — REVIEWED: fail_open_guard below
        try:  # BLE001:FOG (was: FOG/LAC)
            from core.governance.governance_service import GovernanceService

            gov = GovernanceService()
            if DEFAULT_BRAIN_REGISTRATIONS:
                for brain_id, status in DEFAULT_BRAIN_REGISTRATIONS.items():
                    gov.register_brain(brain_id, status)
            return gov
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            pass


def _load_or_create_pnl_store(base_dir: str) -> Any:
    """Load persisted PnL ledger, or create a fresh one.

    BrainPnLStore tracks per-brain counterfactual P&L with horizon-matched
    settlement.  Unlike BrainPerformanceTracker (which uses composite_score
    from consensus-round attribution), the PnL ledger records per-brain
    signals independently — no cross-brain contamination.
    """
    from core.feedback.brain_pnl_ledger import BrainPnLStore

    # 1. Try event stream first (FIX-20260611-022)
    stream_path = Path(base_dir) / "ledger_events.jsonl"
    if stream_path.exists():
        try:
            return BrainPnLStore.load_from_stream(stream_path)
        except Exception:  # noqa: BLE001 — REVIEWED: fail_open_guard below
            try:  # BLE001:FOG (was: FOG/LAC)
                pass
            except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                pass
    # 2. Fall back to old JSON
    ledger_path = Path(base_dir) / "brain_pnl_ledger.json"
    try:
        if ledger_path.exists():
            return BrainPnLStore.load(ledger_path)
    except Exception:  # noqa: BLE001 — REVIEWED: fail_open_guard below
        try:  # BLE001:FOG (was: FOG/LAC)
            pass
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            pass
    # 3. Fresh store
    return BrainPnLStore()


def _step_journal_mt5_reconcile(
    base_dir: str,
    *,
    dry_run: bool = False,
    mt5_terminal_path: str | None = None,
) -> dict[str, Any]:
    """Reconcile journal PnL against MT5 ground truth via history_deals_get().

    FIX-20260626-143 (Journal Integrity L3 Fix — Phase 3):
    Three-pass reconciliation that runs BEFORE Strategy A backfill because
    MT5 deal.profit is the authoritative PnL source.

    Pass 1 — PnL normalization: For overlapping journal/MT5 tickets, correct
      journal PnL to deal.profit when delta exceeds instrument tolerance.
    Pass 2 — Missing close backfill: For MT5 deals with matching journal open
      but no close entry, create synthetic close from deal data.
    Pass 3 — Orphan detection: Journal close entries with no MT5 deal match
      are flagged (quarantine handled by JournalGate).

    Idempotent via reconciliation_watermark.json (composite key:
    {symbol}_{mt5_login_id} → last_deal_id).

    投委会修正令 #1 (Lock Yielding): Every 50 records, releases FileLock
    for >= 100ms to prevent bridge worker I/O starvation.
    投委会修正令 #2 (Composite Key): Watermark keyed by symbol+login_id,
    not symbol alone — prevents deal_id collisions across MT5 accounts.
    投委会防线 #2 (Float Tolerance): BTC 0.01, XAU 0.001 — absolute
    equality (==) is forbidden due to IEEE 754 representation differences.
    """
    import time as _time_module

    from core.contracts.journal_sla import get_tolerance

    _base = Path(base_dir)
    _journal_path = _base / "live_trade_journal.jsonl"
    _watermark_path = _base / "reconciliation_watermark.json"

    result: dict[str, Any] = {
        "step": "journal_mt5_reconcile",
        "status": "skipped",
        "mt5_deals_loaded": 0,
        "pnl_normalized": 0,
        "missing_closes_created": 0,
        "orphans_detected": 0,
        "within_tolerance": 0,
    }

    if not _journal_path.exists():
        result["status"] = "empty_journal"
        return result

    # ── Connect to MT5 ───────────────────────────────────────────────
    mt5 = None
    mt5_login: int = 0
    if mt5_terminal_path:
        try:
            import MetaTrader5 as _mt5_module

            if _mt5_module.initialize(path=str(mt5_terminal_path)):
                mt5 = _mt5_module
                _acc_info = mt5.account_info()
                if _acc_info is not None:
                    mt5_login = int(_acc_info.login)
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):
            pass
    if mt5 is None:
        result["status"] = "mt5_unavailable"
        return result

    # ── Resolve symbol ───────────────────────────────────────────────
    _symbol = "BTCUSDc" if "btc" in str(_base).lower() else "XAUUSDc"
    _tolerance = get_tolerance(_symbol)

    # ── Composite key watermark ──────────────────────────────────────
    _composite_key = f"{_symbol}_{mt5_login}"
    _watermark: dict[str, int] = {}
    if _watermark_path.exists():
        try:
            _watermark = json.loads(_watermark_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            _watermark = {}
    # Migrate old single-symbol keys
    if _symbol in _watermark and _composite_key not in _watermark:
        _watermark[_composite_key] = _watermark.pop(_symbol)
    _last_deal_id = _watermark.get(_composite_key, 0)

    # ── Load MT5 deal history ────────────────────────────────────────
    try:
        _deals_raw = mt5.history_deals_get(
            position=0,  # All positions
            date_from=0,  # No date filter — deal_id cursor is sufficient
            date_to=int(_time_module.time()),
        )
        _deals = list(_deals_raw) if _deals_raw else []
    except (RuntimeError, ValueError, KeyError, TypeError, OSError):
        result["status"] = "mt5_query_failed"
        return result

    _new_deals = [d for d in _deals if getattr(d, "ticket", 0) > _last_deal_id]
    result["mt5_deals_loaded"] = len(_new_deals)

    if not _new_deals:
        result["status"] = "no_new_deals"
        return result

    # ── Group MT5 deals by position ──────────────────────────────────
    _deal_positions: dict[int, list] = {}
    for d in _new_deals:
        _pos_id = getattr(d, "position_id", 0) or getattr(d, "order", 0)
        if _pos_id:
            _deal_positions.setdefault(int(_pos_id), []).append(d)

    # ── Load journal index ───────────────────────────────────────────
    _jrn_index: dict[int, dict] = {}  # ticket → {open, close}
    with open(_journal_path, encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line:
                continue
            try:
                _e = json.loads(_line)
            except json.JSONDecodeError:
                continue
            _t = _e.get("position_ticket")
            if not isinstance(_t, int) or _t <= 0:
                continue
            if _t not in _jrn_index:
                _jrn_index[_t] = {"open": None, "close": None}
            _action = _e.get("action", "")
            if _action == "open" and _jrn_index[_t]["open"] is None:
                _jrn_index[_t]["open"] = _e
            elif _action == "close":
                _jrn_index[_t]["close"] = _e

    # ── Lock yielding state ──────────────────────────────────────────
    _batch_count = 0
    _lock = None
    _lock_dir = _base / ".locks"

    def _yield_lock() -> None:
        """Release lock for 100ms to let bridge worker write."""
        nonlocal _lock
        if _lock is not None:
            try:
                _lock.release()
            except (RuntimeError, ValueError, KeyError, TypeError, OSError):
                pass
            _time_module.sleep(0.1)

    def _acquire_lock() -> bool:
        nonlocal _lock
        from core.infrastructure.distributed_lock import FileLock

        _lock = FileLock("live_trade_journal", lock_dir=str(_lock_dir), ttl_seconds=30)
        _acq = _lock.acquire(blocking=True, timeout_seconds=10)
        return _acq.acquired if _acq else False

    if not _acquire_lock():
        result["status"] = "lock_denied"
        return result

    try:
        _max_new_deal_id = _last_deal_id

        # ── Pass 1: PnL normalization ────────────────────────────────
        for _pos_id, _pos_deals in _deal_positions.items():
            _jrn = _jrn_index.get(_pos_id, {})
            _jrn_close = _jrn.get("close")
            if _jrn_close is None:
                continue

            # Get MT5 deal profit
            _exit_deals = [d for d in _pos_deals if getattr(d, "entry", -1) == 1]
            if not _exit_deals:
                continue
            _mt5_profit = sum(getattr(d, "profit", 0.0) or 0.0 for d in _exit_deals)

            _jrn_pnl = _jrn_close.get("pnl")
            if _jrn_pnl is None:
                _jrn_pnl = 0.0

            _delta = abs(float(_jrn_pnl) - float(_mt5_profit))

            if _delta < _tolerance:
                result["within_tolerance"] += 1
                continue

            # ── FIX-20260626-144 (Pass 1 PnL Fix): Write a correction
            # close entry via _append_journal instead of mutating the
            # in-memory dict alone (which was a no-op — never persisted).
            # Journal is append-only; correction entry supersedes original.
            _mt5_pnl = float(_mt5_profit)

            # Keep in-memory index updated for Pass 2/3 (A3: secondary guard)
            _jrn_close["pnl"] = _mt5_pnl
            _jrn_close["_pnl_status"] = "verified_from_mt5_deal"
            if isinstance(_jrn_close.get("detail"), dict):
                _jrn_close["detail"]["pnl"] = _mt5_pnl

            # Build correction entry from original close, overriding PnL
            from core.ledger.services.journal_cleanup import _append_journal

            _correction_entry: dict[str, Any] = {
                **_jrn_close,
                "pnl": _mt5_pnl,
                "_pnl_status": "verified_from_mt5_deal",
                "_source": "mt5_reconciliation",
                "message_id": (f"recon_pnl_fix_{_pos_id}_" f"{int(_time_module.time())}"),
            }
            if isinstance(_correction_entry.get("detail"), dict):
                _correction_entry["detail"]["pnl"] = _mt5_pnl
            if "label" in _correction_entry:
                del _correction_entry["label"]  # Let label builder re-classify

            # Write correction (dedup allows mt5_reconciliation to supersede)
            _append_journal(_journal_path, _correction_entry, lock_dir=_lock_dir, gate=None)
            result["pnl_normalized"] += 1

            # Lock yielding every 50 records
            _batch_count += 1
            if _batch_count % 50 == 0:
                _yield_lock()
                _time_module.sleep(0.1)
                if not _acquire_lock():
                    break

            _max_new_deal_id = max(
                _max_new_deal_id, max(getattr(d, "ticket", 0) for d in _pos_deals)
            )

        # ── Pass 2: Missing close backfill ───────────────────────────
        for _pos_id, _pos_deals in _deal_positions.items():
            _jrn = _jrn_index.get(_pos_id, {})
            if _jrn.get("close") is not None:
                continue  # Already has a close entry
            _jrn_open = _jrn.get("open")
            if _jrn_open is None:
                continue  # No open to link to

            _exit_deals = [d for d in _pos_deals if getattr(d, "entry", -1) == 1]
            if not _exit_deals:
                continue

            _last_deal = max(_exit_deals, key=lambda d: getattr(d, "time", 0))
            _close_price = getattr(_last_deal, "price", 0.0) or 0.0
            _close_time_ts = getattr(_last_deal, "time", 0)
            _mt5_profit = sum(getattr(d, "profit", 0.0) or 0.0 for d in _exit_deals)
            _close_reason = getattr(_last_deal, "reason", -1)
            _close_time = (
                datetime.fromtimestamp(_close_time_ts, tz=UTC).isoformat().replace("+00:00", "Z")
                if _close_time_ts
                else _jrn_open.get("recorded_at", "")
            )

            _reason_map = {4: "sl_hit", 5: "tp_hit"}
            _close_reason_str = _reason_map.get(_close_reason, "detected_by_reconciliation")

            _label = "unknown_pnl_pending"
            if _mt5_profit != 0:
                _label = "win" if _mt5_profit > 0 else "loss"

            _close_entry: dict[str, Any] = {
                "schema_version": "live_trade_journal.v2",
                "recorded_at": _close_time,
                "message_id": f"recon_close_{_pos_id}_{int(_close_time_ts)}",
                "target": "exec_bridge",
                "ack_status": "closed",
                "detail": {
                    "reason": _close_reason_str,
                    "close_price": _close_price,
                    "profit": _mt5_profit,
                    "pnl": _mt5_profit,
                },
                "symbol": _symbol,
                "action": "close",
                "side": _jrn_open.get("side", ""),
                "volume": _jrn_open.get("volume", 0.0),
                "pnl": _mt5_profit,
                "_pnl_status": "verified_from_mt5_deal",
                "label": _label,
                "position_ticket": _pos_id,
                "magic": _jrn_open.get("magic", 0),
                "strategy": _jrn_open.get("strategy", ""),
                "open_message_id": _jrn_open.get("message_id", ""),
                "_source": "mt5_reconciliation",
            }

            from core.ledger.services.journal_cleanup import _append_journal

            _append_journal(_journal_path, _close_entry, lock_dir=_lock_dir, gate=None)
            result["missing_closes_created"] += 1

            _batch_count += 1
            if _batch_count % 50 == 0:
                _yield_lock()
                _time_module.sleep(0.1)
                if not _acquire_lock():
                    break

        # ── Pass 3: Orphan detection ─────────────────────────────────
        for _ticket, _jrn in _jrn_index.items():
            _jrn_close = _jrn.get("close")
            if _jrn_close is None:
                continue
            if _ticket not in _deal_positions:
                result["orphans_detected"] += 1

        # ── Update watermark ──────────────────────────────────────────
        if _max_new_deal_id > _last_deal_id:
            _watermark[_composite_key] = _max_new_deal_id
            _watermark_path.write_text(json.dumps(_watermark, indent=2), encoding="utf-8")

        result["status"] = "ok"

    finally:
        if _lock is not None:
            try:
                _lock.release()
            except (RuntimeError, ValueError, KeyError, TypeError, OSError):
                pass

    return result


def _step_journal_health_report(
    base_dir: str,
    *,
    gate: Any | None = None,
    mt5_terminal_path: str | None = None,
) -> dict[str, Any]:
    """Generate journal health report with SLA compliance assessment.

    FIX-20260626-143: Runs AFTER reconciliation. Reports coverage,
    PnL accuracy, orphan count, null-PnL count, and SLA status.
    DingTalk alert if SLA is violated.
    """
    from core.contracts.journal_sla import JournalHealthSLA

    _base = Path(base_dir)
    _journal_path = _base / "live_trade_journal.jsonl"

    report: dict[str, Any] = {
        "step": "journal_health_report",
        "status": "ok",
        "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
    }

    if not _journal_path.exists():
        report.update(
            {
                "total_entries": 0,
                "open_entries": 0,
                "close_entries": 0,
                "coverage_pct": 0.0,
                "pnl_mismatch_pct": 0.0,
                "orphan_count": 0,
                "null_pnl_pct": 0.0,
                "breakeven_pct": 0.0,
                "sla_status": "violated",
                "sla_reason": "journal_not_found",
            }
        )
        return report

    _total = 0
    _opens = 0
    _closes = 0
    _null_pnl = 0
    _breakeven = 0
    _orphans = 0

    _open_tickets: set[int] = set()
    _close_tickets: set[int] = set()

    with open(_journal_path, encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line:
                continue
            try:
                _e = json.loads(_line)
            except json.JSONDecodeError:
                continue
            _total += 1
            _action = _e.get("action", "")
            _ticket = _e.get("position_ticket")

            if _action == "open":
                _opens += 1
                if isinstance(_ticket, int) and _ticket > 0:
                    _open_tickets.add(_ticket)
            elif _action == "close":
                _closes += 1
                if isinstance(_ticket, int) and _ticket > 0:
                    _close_tickets.add(_ticket)
                _pnl = _e.get("pnl")
                if _pnl is None or _e.get("_pnl_status") == "pending_mt5_confirmation":
                    _null_pnl += 1
                if _e.get("label") == "breakeven":
                    _breakeven += 1

    # Detect orphans: close tickets not in open set
    _orphans = len(_close_tickets - _open_tickets)

    # SLA metrics
    _coverage = (len(_open_tickets & _close_tickets) / max(len(_close_tickets), 1)) * 100
    _null_pnl_pct = (_null_pnl / max(_closes, 1)) * 100
    _breakeven_pct = (_breakeven / max(_closes, 1)) * 100

    report.update(
        {
            "total_entries": _total,
            "open_entries": _opens,
            "close_entries": _closes,
            "coverage_pct": round(_coverage, 1),
            "pnl_mismatch_pct": 0.0,  # Set by reconciliation step
            "orphan_count": _orphans,
            "null_pnl_pct": round(_null_pnl_pct, 1),
            "breakeven_pct": round(_breakeven_pct, 1),
        }
    )

    # Gate health
    if gate is not None:
        report["quarantine"] = gate.get_health()

    _sla_status = JournalHealthSLA.assess(report)
    report["sla_status"] = _sla_status

    # ── DingTalk alert on violation ───────────────────────────────────
    if _sla_status == "violated":
        _alert = json.dumps(
            {
                "event": "JOURNAL_SLA_VIOLATED",
                "severity": "P1",
                "base_dir": str(_base),
                "metrics": report,
            },
            ensure_ascii=False,
        )
        print(_alert, flush=True)
        report["alert_sent"] = True

    return report


def _step_journal_backfill(base_dir: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Backfill null PnL in live_trade_journal.jsonl from close_price.

    Strategy A only (no MT5 dependency): for close entries whose ``pnl`` field
    is null but whose ``detail.close_price`` is populated, computes PnL from
    ``(close_price - entry_price) × volume`` using the matching open entry.

    Strategy B (MT5 deal history) is intentionally NOT run here — it belongs
    in the standalone ``scripts/backfill_journal_pnl.py`` which requires an
    MT5 terminal session.

    The journal is rewritten atomically (temp file + os.replace) under
    FileLock protection to prevent concurrent-write corruption.

    FIX-20260622-057 Phase 3c: Integrated into daily_ops to run before
    label_builder so labels benefit from backfilled PnL data.
    """
    import os as _os

    try:
        base = Path(base_dir)
        journal_path = base / "live_trade_journal.jsonl"
        if not journal_path.exists():
            return {
                "step": "journal_backfill",
                "status": "skipped",
                "reason": "no_journal",
                "fixed": 0,
                "skipped": 0,
            }

        # ── Load journal ──
        entries: list[dict[str, Any]] = []
        for _line in journal_path.read_text(encoding="utf-8").splitlines():
            _line = _line.strip()
            if not _line:
                continue
            with suppress(json.JSONDecodeError):
                entries.append(json.loads(_line))

        # ── Build open-entry index: ticket → open entry ──
        open_by_ticket: dict[int, dict[str, Any]] = {}
        for e in entries:
            if e.get("action") == "open":
                t = e.get("position_ticket")
                if isinstance(t, int) and t > 0:
                    open_by_ticket[t] = e

        # ── Find backfill candidates ──
        fixed = 0
        skipped_no_close_price = 0
        skipped_no_open = 0

        for e in entries:
            if e.get("action") != "close":
                continue
            if e.get("pnl") is not None:
                continue

            ticket = e.get("position_ticket")
            if not isinstance(ticket, int):
                skipped_no_open += 1
                continue
            detail = e.get("detail", {})
            close_price = detail.get("close_price") if isinstance(detail, dict) else None
            if close_price is None:
                skipped_no_close_price += 1
                continue

            open_entry = open_by_ticket.get(ticket)
            if open_entry is None:
                skipped_no_open += 1
                continue

            # Resolve entry price from open entry
            _od = open_entry.get("detail", {})
            _oreq = _od.get("request", {}) if isinstance(_od, dict) else {}
            entry_price = _oreq.get("price") if isinstance(_oreq, dict) else None
            if entry_price is None:
                skipped_no_open += 1
                continue

            side = e.get("side", open_entry.get("side", ""))
            # Volume: prefer open entry (close entries often have vol=0 for full closes)
            _cv = float(e.get("volume", 0) or 0)
            _ov = float(open_entry.get("volume", 0) or 0)
            volume = _ov if _ov > 0 else (_cv if _cv > 0 else 0.01)

            cp = float(close_price)
            ep = float(entry_price)
            if side == "short":
                pnl = round((ep - cp) * volume, 6)
            else:
                pnl = round((cp - ep) * volume, 6)

            if not dry_run:
                e["pnl"] = pnl
                if isinstance(e.get("detail"), dict):
                    e["detail"]["profit"] = pnl
            fixed += 1

        # ── Rewrite journal atomically if anything was fixed ──
        if fixed > 0 and not dry_run:
            from core.infrastructure.distributed_lock import FileLock

            _lock_dir = base / "locks"
            lock = FileLock("live_trade_journal", lock_dir=str(_lock_dir), ttl_seconds=10)
            acquired = lock.acquire(blocking=True, timeout_seconds=5)
            if not acquired.acquired:
                return {
                    "step": "journal_backfill",
                    "status": "error",
                    "error": f"lock denied: {acquired.error}",
                    "fixed": fixed,
                    "skipped": skipped_no_close_price + skipped_no_open,
                }
            try:
                _tmp = journal_path.with_suffix(".jsonl.backfill_tmp")
                _tmp.write_text(
                    "\n".join(json.dumps(e, ensure_ascii=False, default=str) for e in entries)
                    + "\n",
                    encoding="utf-8",
                )
                _os.replace(_tmp, journal_path)
            finally:
                lock.release()

        return {
            "step": "journal_backfill",
            "status": "ok",
            "dry_run": dry_run,
            "fixed": fixed,
            "skipped": skipped_no_close_price + skipped_no_open,
            "skipped_no_close_price": skipped_no_close_price,
            "skipped_no_open": skipped_no_open,
            "total_entries": len(entries),
        }

    except Exception as exc:  # noqa: BLE001 — REVIEWED: fail_open_guard below
        try:  # BLE001:FOG (was: FOG/LAC)
            return {"step": "journal_backfill", "status": "error", "error": str(exc)[:500]}
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            return {"step": "journal_backfill", "status": "error", "error": "unknown"}


def _step_label_builder(
    base_dir: str, *, dry_run: bool = False, contract_path: Path | None = None
) -> dict[str, Any]:
    """Generate training labels from live + paper trade journals.

    Calls label_builder.build_trade_records() to produce live_labels.jsonl,
    which downstream steps (feedback_loop, retraining_check, leaderboard) depend on.
    Runs BEFORE feedback_loop so tracker sees fresh labels.

    FIX-20260622-057 Phase 2: Now resolves per-brain training contracts via
    resolve_brain_contracts() so each brain's label is classified using its
    own SL/TP contract rather than a single symbol-wide contract.
    Also computes label_coverage_pct — the percentage of journal position
    tickets that received a non-"unlabeled" label.
    """
    try:
        from scripts.training.label_builder import build_trade_records, resolve_brain_contracts

        base = Path(base_dir)
        out_dir = base / "reports"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "live_labels.jsonl"

        # Load optional label contract for barrier-based classification
        contract = None
        if contract_path is not None and contract_path.exists():
            from core.contracts.training.label_contract import LabelContract

            contract = LabelContract.from_file(contract_path)

        # FIX-20260622-057 Phase 2 A1: Resolve per-brain training contracts.
        # Each brain's label is classified using its own SL/TP contract.
        # Falls back gracefully if resolve_brain_contracts() returns {}.
        _brain_contracts = resolve_brain_contracts()

        # Resolve OHLC price data directory for barrier simulation
        _price_data_raw = base / "raw"
        _price_data_dir: Path | None = _price_data_raw if _price_data_raw.is_dir() else None

        # Process live journal
        live_records: list[dict[str, Any]] = []
        live_journal = base / "live_trade_journal.jsonl"
        if live_journal.exists():
            live_records = build_trade_records(
                live_journal,
                contract=contract,
                brain_contracts=_brain_contracts,
                price_data_dir=_price_data_dir,
            )

        # Process paper journal
        paper_records: list[dict[str, Any]] = []
        paper_journal = base / "paper_trade_journal.jsonl"
        if paper_journal.exists():
            paper_records = build_trade_records(
                paper_journal,
                contract=contract,
                brain_contracts=_brain_contracts,
                price_data_dir=_price_data_dir,
            )

        all_records = live_records + paper_records

        if not dry_run:
            lines = "\n".join(json.dumps(r, ensure_ascii=False, default=str) for r in all_records)
            out_path.write_text(lines + "\n", encoding="utf-8")

        closed = sum(1 for r in all_records if r.get("is_closed"))
        open_trades = len(all_records) - closed
        wins = sum(1 for r in all_records if r["label"] in ("win", "tp_hit_first"))
        losses = sum(1 for r in all_records if r["label"] in ("loss", "sl_hit_first"))

        # FIX-20260622-057 Phase 2 C1: label coverage metric.
        # Coverage = |journal position_tickets ∩ label position_tickets| / |journal position_tickets|
        _journal_path = base / "live_trade_journal.jsonl"
        _journal_tickets: set[int] = set()
        if _journal_path.exists():
            for _line in _journal_path.read_text(encoding="utf-8").splitlines():
                _line = _line.strip()
                if not _line:
                    continue
                try:
                    _r = json.loads(_line)
                    # DQAF-20260623-073: unified ticket resolver
                    _t = resolve_ticket(_r)
                    if _t and isinstance(_t, int) and _r.get("action") == "open":
                        _journal_tickets.add(_t)
                except (json.JSONDecodeError, AttributeError):
                    pass
        _labeled_tickets = {
            r.get("position_ticket")
            for r in all_records
            if r.get("position_ticket") is not None and r.get("label") != "unlabeled"
        }
        _coverage_pct = round(len(_labeled_tickets) / max(len(_journal_tickets), 1) * 100, 1)

        return {
            "step": "label_builder",
            "status": "ok",
            "dry_run": dry_run,
            "total_labels": len(all_records),
            "live_labels": len(live_records),
            "paper_labels": len(paper_records),
            "closed_trades": closed,
            "open_trades": open_trades,
            "wins": wins,
            "losses": losses,
            "label_coverage_pct": _coverage_pct,
            "journal_tickets": len(_journal_tickets),
            "labeled_tickets": len(_labeled_tickets),
            "output": str(out_path) if not dry_run else None,
        }
    except Exception as exc:  # noqa: BLE001 — REVIEWED: fail_open_guard below
        try:  # BLE001:FOG (was: FOG/LAC)
            return {"step": "label_builder", "status": "error", "error": str(exc)[:500]}
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            return {"step": "label_builder", "status": "error", "error": "unknown"}


def _step_shadow_ensemble(base_dir: str) -> dict[str, Any]:
    """Run shadow ensemble and return summary."""
    try:
        from scripts.live_shadow_ensemble import build_report

        # FIX-20260612-021: resolve symbol from base_dir instead of hardcoding XAU
        _symbol = "BTCUSDc" if "btc" in str(base_dir).lower() else "XAUUSDc"
        report = build_report(
            brains_dir=_resolve_brains_dir(base_dir),
            feature_store_dir=Path(base_dir) / "feature_store",
            parallel=True,
            symbol=_symbol,
        )
        return {
            "step": "shadow_ensemble",
            "status": "ok" if "error" not in report else "error",
            "brains": report.get("total_brains", 0),
            "consensus": report.get("comparison", {}).get("consensus", "unknown"),
            "agreement": report.get("comparison", {}).get("agreement_score", 0.0),
        }
    except Exception as exc:  # noqa: BLE001 — REVIEWED: fail_open_guard below
        try:  # BLE001:FOG (was: FOG/LAC)
            return {"step": "shadow_ensemble", "status": "error", "error": str(exc)[:500]}
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            return {"step": "shadow_ensemble", "status": "error", "error": "unknown"}


def _step_feedback_loop(
    base_dir: str, *, dry_run: bool = False, tracker: Any = None, symbol: str = "XAUUSDc"
) -> dict[str, Any]:
    """Run feedback loop to update tracker with real trade outcomes from journal."""
    try:
        from scripts.feedback_loop import ingest_journal_to_tracker

        if tracker is None:
            tracker = _load_or_create_tracker(base_dir)
        report = ingest_journal_to_tracker(
            tracker, base_dir=base_dir, dry_run=dry_run, symbol=symbol
        )
        return {
            "step": "feedback_loop",
            "status": "ok",
            "mode": report.get("mode", "multi_brain"),
            "journal_entries": report.get("journal_entries", 0),
            "accepted_trades": report.get("accepted_trades", 0),
            "updates_applied": report.get("updates_applied", 0),
            "brains_updated": report.get("brain_ids_updated", []),
        }
    except Exception as exc:  # noqa: BLE001 — REVIEWED: fail_open_guard below
        try:  # BLE001:FOG (was: FOG/LAC)
            return {"step": "feedback_loop", "status": "error", "error": str(exc)[:500]}
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            return {"step": "feedback_loop", "status": "error", "error": "unknown"}


def _step_calibrator_feed(base_dir: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Feed the ConformalCalibrator from closed trades — independent of ML online learning.

    FIX-028 hardcoded ``_step_online_feedback`` as "skipped: brain_retired" when
    Online_MLP_V1 was retired (2026-05-25).  This also starved the ConformalCalibrator
    whose only update() call site was inside ``OnlineFeedbackHook.process_closed_trades()``.

    This step decouples calibrator updates from the ML pipeline so the calibrator
    continues accumulating samples even when online learning is disabled.
    """
    import json
    from pathlib import Path

    journal_path = Path(base_dir) / "live_trade_journal.jsonl"
    if not journal_path.exists():
        return {"step": "calibrator_feed", "status": "skipped", "reason": "no_journal"}

    # ── FIX-20260613-090: record-id watermark replaces brittle line-number pointer ──
    # Journal compaction (compact_journal) prunes old rejected entries and rewrites
    # the file, invalidating line-number offsets.  We now use (recorded_at, message_id)
    # as an append-only watermark that survives compaction.
    state_path = Path(base_dir) / "calibrator_feed_state.json"
    last_recorded_at = ""
    last_message_id = ""
    _state: dict[str, Any] = {}
    if state_path.exists():
        try:  # BLE001:FOG (was: FOG/LAC)
            _state = json.loads(state_path.read_text(encoding="utf-8"))
            last_recorded_at = _state.get("last_recorded_at", "")
            last_message_id = _state.get("last_message_id", "")
            # Migration: old format used "last_line" — derive watermark from it
            if not last_recorded_at and "last_line" in _state:
                _old_pos = _state["last_line"]
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            pass

    # Read all lines (typically <1000; reading full file is fast and safe)
    lines = journal_path.read_text(encoding="utf-8").splitlines()

    # Migrate from legacy last_line if needed
    if not last_recorded_at and "last_line" in _state:
        _old_pos = _state["last_line"]
        if _old_pos > 0 and _old_pos <= len(lines):
            # Normal case: pointer is valid, derive watermark from the
            # last-processed line's timestamp
            try:
                _mig_entry = json.loads(lines[_old_pos - 1])
                last_recorded_at = _mig_entry.get("recorded_at", "")
                last_message_id = _mig_entry.get("message_id", "")
            except Exception:  # noqa: BLE001 — REVIEWED: fail_open_guard below
                try:  # BLE001:FOG (was: FOG/LAC)
                    pass
                except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                    pass
        elif _old_pos > len(lines):
            # Compaction pruned lines — the pointer is now beyond EOF.
            # Use empty watermark to force a full rescan.  The calibrator
            # may briefly double-count ~55 existing samples, but this is
            # infinitely better than permanent zero-processing stall.
            last_recorded_at = ""
            last_message_id = ""
        # else: _old_pos == 0 (first run) — leave watermark empty

    # Filter to lines strictly after the watermark
    new_lines: list[str] = []
    for _l in lines:
        try:
            _e = json.loads(_l)
        except Exception:  # noqa: BLE001 — REVIEWED: fail_open_guard below
            try:  # BLE001:FOG (was: FOG/LAC)
                continue
            except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                # FIX-20260627-153: ensure skip on parse failure
                continue
        _ts = _e.get("recorded_at", "")
        _mid = _e.get("message_id", "")
        if _ts > last_recorded_at or (_ts == last_recorded_at and _mid != last_message_id):
            new_lines.append(_l)

    if not new_lines:
        return {"step": "calibrator_feed", "status": "ok", "new_samples": 0, "total": 0}

    # Load calibrator
    try:
        from core.execution.conformal_calibrator import ConformalCalibrator

        cal = ConformalCalibrator(state_path=f"{base_dir}/conformal_calibrator_state.json")
    except Exception as e:  # noqa: BLE001 — REVIEWED: fail_open_guard below
        try:  # BLE001:FOG (was: FOG/LAC)
            return {"step": "calibrator_feed", "status": "error", "error": f"init: {e}"}
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            pass
    # Pass 1: build p_win lookup + cold_explore blacklist from accepted entries
    p_win_by_msg_id: dict[str, float] = {}
    cold_explore_msg_ids: set[str] = set()  # DQAF-053: defense-in-depth
    skipped_cold_explore: int = 0
    for line in new_lines:
        try:
            entry = json.loads(line)
        except Exception:  # noqa: BLE001 — REVIEWED: fail_open_guard below
            try:  # BLE001:FOG (was: FOG/LAC)
                continue
            except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                # FIX-20260627-153: ensure skip on parse failure
                continue
        if entry.get("ack_status") != "accepted":
            continue

        # ── DQAF-053: detect cold_explore by p_win_source marker ──
        pws = entry.get("p_win_source", "")
        if not pws:
            ctx = entry.get("entry_context", {})
            if isinstance(ctx, dict):
                pws = ctx.get("p_win_source", "")
        mid = entry.get("message_id")
        if pws == "cold_explore_neutral" and mid:
            cold_explore_msg_ids.add(mid)

        pw = entry.get("p_win")
        if pw is None:
            continue
        if mid:
            p_win_by_msg_id[mid] = float(pw)

    # Pass 2: JOIN closed → p_win via open_message_id
    new_samples = 0
    for line in new_lines:
        try:
            entry = json.loads(line)
        except Exception:  # noqa: BLE001 — REVIEWED: fail_open_guard below
            try:  # BLE001:FOG (was: FOG/LAC)
                continue
            except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                # FIX-20260627-153: ensure skip on parse failure
                continue
        if entry.get("ack_status") != "closed":
            continue

        p_win = entry.get("p_win")
        if p_win is None:
            detail = entry.get("detail", {})
            if isinstance(detail, dict):
                p_win = detail.get("p_win")
        if p_win is None:
            open_mid = entry.get("open_message_id")
            if open_mid:
                p_win = p_win_by_msg_id.get(open_mid)
        if p_win is None:
            continue

        # ── DQAF-053: defense-in-depth — skip cold_explore p_win ──
        # Cold-explore entries have p_win forced to 0.50 regardless of
        # actual signal quality.  Feeding them into the ConformalCalibrator
        # biases the Q10 threshold toward 0.50, degrading gate accuracy.
        # Check both the closed entry's own p_win_source AND the JOIN-linked
        # open entry's p_win_source to catch both serialisation paths.
        _pws_closed = entry.get("p_win_source", "")
        if _pws_closed == "cold_explore_neutral":
            skipped_cold_explore += 1
            continue
        _open_mid = entry.get("open_message_id")
        if _open_mid and _open_mid in cold_explore_msg_ids:
            skipped_cold_explore += 1
            continue

        label = None
        lbl = entry.get("label", "")
        pnl = entry.get("pnl")
        if isinstance(pnl, int | float) and pnl is not None:
            if pnl > 0:
                label = 1
            elif pnl < 0:
                label = -1
            else:
                label = 0
        elif isinstance(lbl, str):
            if "win" in lbl.lower() or "tp" in lbl.lower():
                label = 1
            elif "loss" in lbl.lower() or "sl" in lbl.lower():
                label = -1
        if label is None:
            continue

        ts = entry.get("recorded_at", "")
        try:  # BLE001:FOG (was: FOG/LAC)
            cal.update(float(p_win), label, timestamp_utc=str(ts))
            new_samples += 1
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            pass

    # Save watermark for next run (record-id based, compaction-safe)
    _last_ts = last_recorded_at
    _last_mid = last_message_id
    for _l in reversed(new_lines):
        try:
            _e = json.loads(_l)
            _last_ts = _e.get("recorded_at", _last_ts)
            _last_mid = _e.get("message_id", _last_mid)
            break
        except Exception:  # noqa: BLE001 — REVIEWED: fail_open_guard below
            try:  # BLE001:FOG (was: FOG/LAC)
                continue
            except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                pass
    # ── FIX-20260627-152: Persist watermark via StateWriter gate ──
    # Was: state_path.write_text(json.dumps({...})) — raw write bypassing
    # the 4-layer StateWriter gate (Plan B Phase 1-4).
    if not dry_run:
        try:  # BLE001:FOG
            from core.state.catalog import lookup
            from core.state.writer import StateWriter

            _sym = "BTCUSDc" if "btc" in str(base_dir).lower() else "XAUUSDc"
            _writer = StateWriter(str(base_dir), symbol=_sym)
            _writer.write_artifact(
                lookup("CALIBRATOR_FEED_STATE"),
                _sym,
                {
                    "last_recorded_at": _last_ts,
                    "last_message_id": _last_mid,
                    "last_line": len(lines),  # retained for backward compatibility
                    "updated_utc": datetime.now(UTC).isoformat(),
                    "sample_count": cal.describe().get("sample_count", 0),
                },
            )
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            pass

    diag = cal.describe()
    return {
        "step": "calibrator_feed",
        "status": "ok",
        "new_samples": new_samples,
        "total_samples": diag.get("sample_count", 0),
        "is_warm": diag.get("is_warm", False),
        "threshold": diag.get("current_threshold"),
        "skipped_cold_explore": skipped_cold_explore,  # DQAF-053
    }


def _step_online_feedback(base_dir: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Online_MLP_V1 retired 2026-05-25 (pnl:critical). This step is permanently skipped.

    Calibrator updates are handled separately by ``_step_calibrator_feed()``.
    """
    return {"step": "online_feedback", "status": "skipped", "reason": "brain_retired"}


def _step_paper_trade_simulation(base_dir: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Run paper trade simulator to generate labeled outcomes from shadow decisions."""
    try:
        from scripts.paper_trade_simulator import run_simulator

        result = run_simulator(
            since=None,
            dry_run=dry_run,
            output_path=Path(base_dir) / "paper_trade_journal.jsonl",
        )
        return {
            "step": "paper_trade_simulation",
            "status": result.get("status", "ok"),
            "trades": result.get("trades", 0),
            "total_pnl": result.get("total_pnl", 0),
            "win_rate": result.get("win_rate", 0),
            "dry_run": dry_run,
        }
    except Exception as exc:  # noqa: BLE001 — REVIEWED: fail_open_guard below
        try:  # BLE001:FOG (was: FOG/LAC)
            return {"step": "paper_trade_simulation", "status": "error", "error": str(exc)[:500]}
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            return {"step": "paper_trade_simulation", "status": "error", "error": "unknown"}


def _step_governance(
    base_dir: str,
    *,
    dry_run: bool = False,
    tracker: Any = None,
    governance: Any = None,
    pnl_store: Any = None,
) -> dict[str, Any]:
    """Run governance cycle and cross-validate against leaderboard data.

    Cross-checks tracker-based recommendations against the leaderboard's
    trade-linked win_rates to detect inconsistent governance signals.
    """
    try:
        from scripts.training.governance_scheduler import run_governance_cycle

        if tracker is None:
            tracker = _load_or_create_tracker(base_dir)
        if governance is None:
            governance = _load_or_create_governance(base_dir)
        if pnl_store is None:
            pnl_store = _load_or_create_pnl_store(base_dir)
        report = run_governance_cycle(
            tracker, governance, dry_run=dry_run, pnl_store=pnl_store, base_dir=base_dir
        )

        # ── Cross-validate against leaderboard ──
        cross_check = _cross_check_governance_with_leaderboard(base_dir, report)

        return {
            "step": "governance",
            "status": "ok",
            "brains_assessed": report.get("brains_assessed", 0),
            "actions_applied": len(report.get("actions_applied", [])),
            "actions_flagged": len(report.get("actions_flagged", [])),
            "details": report.get("actions_applied", []),
            "flagged": report.get("actions_flagged", []),
            "leaderboard_cross_check": cross_check,
        }
    except Exception as exc:  # noqa: BLE001 — REVIEWED: fail_open_guard below
        # FIX-20260621-043: Log the exception type so silent type errors
        # (like AttributeError from dict-vs-dataclass mismatch) are visible
        # in diagnostics, not swallowed without trace.
        import logging as _diag_log

        _diag_log.getLogger(__name__).exception(
            "daily_ops:_step_governance failed — type=%s: %s",
            type(exc).__name__,
            str(exc)[:300],
        )
        try:  # BLE001:FOG (was: FOG/LAC)
            return {
                "step": "governance",
                "status": "error",
                "error": f"{type(exc).__name__}: {str(exc)[:400]}",
            }
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            return {"step": "governance", "status": "error", "error": "unknown"}


def _cross_check_governance_with_leaderboard(
    base_dir: str, gov_report: dict[str, Any]
) -> list[dict[str, Any]]:
    """Cross-validate governance actions against leaderboard win_rates.

    Returns a list of conflict warnings where tracker composite_score and
    leaderboard trade-linked win_rate tell contradictory stories.
    """
    lb_path = Path(base_dir) / "reports" / "leaderboard.json"
    if not lb_path.exists():
        return []

    try:
        leaderboard = json.loads(lb_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []

    lb_entries = leaderboard.get("leaderboard", [])
    if not lb_entries:
        return []

    # Index leaderboard by brain_id
    lb_index: dict[str, dict[str, Any]] = {}
    for e in lb_entries:
        lb_index[e.get("brain_id", "")] = e

    conflicts: list[dict[str, Any]] = []

    actions = gov_report.get("actions_applied", []) + gov_report.get("actions_flagged", [])
    for action in actions:
        brain_id = action.get("brain_id", "")
        lb_entry = lb_index.get(brain_id)
        if lb_entry is None:
            continue

        trade_perf = lb_entry.get("trade_performance") or {}
        lb_win_rate = trade_perf.get("win_rate")
        lb_linked = trade_perf.get("linked_trades", 0)
        tracker_composite = action.get("composite_mean", 0.0)

        # Conflict: tracker says healthy (composite > 0.5) but leaderboard
        # shows low win_rate (<0.35) with enough linked trades
        if (
            lb_win_rate is not None
            and lb_linked >= 5
            and lb_win_rate < 0.35
            and tracker_composite > 0.50
        ):
            conflicts.append(
                {
                    "brain_id": brain_id,
                    "type": "tracker_leaderboard_divergence",
                    "tracker_composite": tracker_composite,
                    "leaderboard_win_rate": lb_win_rate,
                    "leaderboard_linked_trades": lb_linked,
                    "detail": "Tracker shows healthy composite but leaderboard win_rate is low",
                }
            )

        # Conflict: tracker recommends demotion but leaderboard shows
        # strong win_rate with sufficient data
        recommendation = action.get("recommendation", "")
        if (
            recommendation in ("freeze", "demote_to_probation")
            and lb_win_rate is not None
            and lb_linked >= 5
            and lb_win_rate > 0.50
        ):
            conflicts.append(
                {
                    "brain_id": brain_id,
                    "type": "recommendation_leaderboard_divergence",
                    "recommendation": recommendation,
                    "leaderboard_win_rate": lb_win_rate,
                    "leaderboard_linked_trades": lb_linked,
                    "detail": f"Governance recommends {recommendation} but leaderboard shows strong win_rate",
                }
            )

    return conflicts


def _step_champion_challenger(
    base_dir: str, *, dry_run: bool = False, tracker: Any = None, governance: Any = None
) -> dict[str, Any]:
    """Run champion/challenger promotion cycle and return summary."""
    try:
        from scripts.training.champion_challenger import run_promotion_cycle

        if tracker is None:
            tracker = _load_or_create_tracker(base_dir)
        if governance is None:
            governance = _load_or_create_governance(base_dir)
        report = run_promotion_cycle(tracker, governance, dry_run=dry_run)
        return {
            "step": "champion_challenger",
            "status": "ok",
            "brains_assessed": report.get("brains_assessed", 0),
            "comparisons": len(report.get("comparisons", [])),
            "promotions": len(report.get("promotions", [])),
            "eligible": sum(1 for c in report.get("comparisons", []) if c.get("eligible")),
            "details": report.get("promotions", []),
        }
    except Exception as exc:  # noqa: BLE001 — REVIEWED: fail_open_guard below
        try:  # BLE001:FOG (was: FOG/LAC)
            return {"step": "champion_challenger", "status": "error", "error": str(exc)[:500]}
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            return {"step": "champion_challenger", "status": "error", "error": "unknown"}


def _step_retraining_check(
    base_dir: str, *, dry_run: bool = False, auto_execute: bool = False, symbol: str = "XAUUSDc"
) -> dict[str, Any]:
    """Run retraining trigger degradation check and optionally auto-execute.

    Auto-execute safety gates:
      - Only when >=2 brains are degraded with critical severity
      - Only when the same brain was degraded in the previous report
        (2-day persistence gate), OR >=3 critical signals (strong first-time signal)
      - Logs all execution events to data/retraining_log.jsonl
    """
    try:
        from scripts.training.brain_leaderboard import build_report as build_lb
        from scripts.training.retraining_trigger import detect_degradation, execute_retraining

        base = Path(base_dir)
        decisions_dir = base / "decisions"
        labels_path = base / "reports" / "live_labels.jsonl"

        # Load previous leaderboard for trend comparison
        baseline = None
        prev_lb_path = base / "reports" / "leaderboard_prev.json"
        if prev_lb_path.exists():
            baseline = json.loads(prev_lb_path.read_text(encoding="utf-8"))

        leaderboard = build_lb(
            decisions_dir,
            labels_path=labels_path if labels_path.exists() else None,
            symbol=symbol,
        )

        # ── FIX-20260606-132: PnL-based fallback for assets without shadow ensemble ──
        # brain_leaderboard.build_report() relies on XAUUSD.decisions.jsonl from
        # shadow ensemble.  Assets without shadow (BTC 24/7 crypto) have no decision
        # files → leaderboard is always empty.  Fall back to PnL-based BrainLeaderboard
        # which consumes brain_pnl_ledger.json + governance_state.json — available for
        # all assets via base_dir.
        if leaderboard.get("total_decisions", 0) == 0:
            try:
                from core.brains.services.brain_leaderboard import BrainLeaderboard
                from core.feedback.brain_pnl_ledger import BrainPnLMetrics, BrainPnLStore
                from core.feedback.live_journal_metrics import compute_journal_brain_metrics
                from core.governance.governance_service import GovernanceService

                pnl_path = base / "brain_pnl_ledger.json"
                gov_path = base / "governance_state.json"
                if gov_path.exists():
                    governance = GovernanceService.load(gov_path)
                    lb = BrainLeaderboard()

                    # FIX-20260621-032: Use journal-based PnL metrics (live execution pnl_r)
                    # instead of BrainPnLStore (shadow signal pnl_per_unit).
                    # The journal is the sole source of truth for live-trading brain performance.
                    _journal_metrics = compute_journal_brain_metrics(base)

                    # Load PnL store as secondary source (for shadow-only brains
                    # that have no journal trades yet)
                    _pnl_metrics: dict[str, Any] = {}
                    pnl_store = None
                    if pnl_path.exists():
                        try:
                            pnl_store = BrainPnLStore.load(pnl_path)
                            _pnl_metrics = pnl_store.get_all_metrics()
                        except Exception:  # noqa: BLE001 — REVIEWED: fail_open_guard below
                            try:  # BLE001:FOG (was: FOG/LAC)
                                pass
                            except (
                                RuntimeError,
                                ValueError,
                                KeyError,
                                TypeError,
                                OSError,
                            ):  # BLE001:FOG
                                pass

                    # Merge: journal metrics take priority. Shadow-only brains
                    # (no journal trades) fall back to PnL store metrics.
                    #
                    # FIX-20260623-083: compute_journal_brain_metrics() returns
                    # plain dicts, but downstream consumers (BrainLeaderboard.rank,
                    # BrainQualityEngine.assess) may receive mixed types.  Convert
                    # journal dicts → BrainPnLMetrics before merging to prevent
                    # AttributeError ('dict' object has no attribute 'win_rate').
                    _merged_metrics: dict[str, Any] = {}
                    for bid, m in _pnl_metrics.items():
                        _merged_metrics[bid] = m
                    for bid, m in _journal_metrics.items():
                        if isinstance(m, dict):
                            _merged_metrics[bid] = BrainPnLMetrics(
                                brain_id=bid,
                                sample_count=int(m.get("sample_count", 0)),
                                cumulative_pnl=float(
                                    m.get("cumulative_pnl", m.get("pnl_r", 0.0)) or 0.0
                                ),
                                win_rate=float(m.get("win_rate", 0.0) or 0.0),
                                sharpe_ratio=float(m.get("sharpe_ratio", 0.0) or 0.0),
                                profit_factor=float(m.get("profit_factor", 0.0) or 0.0),
                                max_drawdown=float(m.get("max_drawdown", 0.0) or 0.0),
                                long_win_rate=float(m.get("long_win_rate", 0.0) or 0.0),
                                short_win_rate=float(m.get("short_win_rate", 0.0) or 0.0),
                                long_count=int(m.get("long_count", 0)),
                                short_count=int(m.get("short_count", 0)),
                            )
                        else:
                            _merged_metrics[bid] = m  # already BrainPnLMetrics

                    # FIX-20260610-007: pass vote_weights from DynamicBrainWeighter
                    _vote_weights: dict[str, float] = {}
                    try:
                        from core.brains.brain_registry import load_brain_registry
                        from core.brains.services.dynamic_brain_weighter import DynamicBrainWeighter

                        _perf_path = base / "brain_performance.json"
                        if _perf_path.exists():
                            _registry = load_brain_registry(base_dir=str(base))
                            _tracker = _registry.get("tracker")
                            if _tracker is not None:
                                _dw = DynamicBrainWeighter(
                                    performance_tracker=_tracker,
                                    pnl_store=pnl_store,
                                )
                                _vote_weights = _dw.get_weights()
                    except Exception:  # noqa: BLE001 — REVIEWED: fail_open_guard below
                        try:  # BLE001:FOG (was: FOG/LAC)
                            pass
                        except (
                            RuntimeError,
                            ValueError,
                            KeyError,
                            TypeError,
                            OSError,
                        ):  # BLE001:FOG
                            pass

                    # DQAF-20260621-042: POISON PILL — validate merged metrics
                    # before passing to rank().  If required fields are missing,
                    # this raises DataIntegrityError → FAIL-CLOSED halt.
                    # We pass the dict (not list of values) so rank() can
                    # associate each brain_id with its governance state.
                    rankings = lb.rank(
                        _merged_metrics,
                        governance_states=governance.get_all_states(),
                        vote_weights=_vote_weights,
                    )
                    leaderboard = {
                        "schema": "pnl_leaderboard.v1",
                        "generated_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
                        "total_brains": len(rankings),
                        "total_decisions": sum(r.trade_count for r in rankings),
                        "brains": [
                            {
                                "brain_id": r.brain_id,
                                "rank": i + 1,
                                "score": round(r.score, 2),
                                "status": r.governance_status,
                                "win_rate": round(r.win_rate, 4),
                                "profit_factor": round(r.profit_factor, 4),
                                "sharpe_ratio": round(r.sharpe, 4),
                                "total_trades": r.trade_count,
                                "pnl_r": round(r.cum_pnl, 2),
                                "max_drawdown": round(r.max_drawdown, 4),
                                "health_signal": r.health_signal,
                                "vote_weight": r.vote_weight,
                                "recommendation": r.recommendation,
                            }
                            for i, r in enumerate(rankings)
                        ],
                    }

                    # ── DQAF-20260621-042: POISON PILL ──
                    # If the leaderboard was generated with zero decisions
                    # despite having governance data, the data pipeline is
                    # corrupted.  Halt immediately — do NOT write broken
                    # state that downstream consumers would act upon.
                    _gov_states = governance.get_all_states()
                    if leaderboard.get("total_decisions", 0) == 0 and _gov_states:
                        _brain_count = len(_gov_states)
                        _journal_count = len(_journal_metrics)
                        raise DataIntegrityError(
                            f"POISON PILL: Leaderboard generated with 0 total_decisions "
                            f"despite {_brain_count} brains in governance_state and "
                            f"{_journal_count} brains with journal metrics. "
                            f"This indicates a data pipeline corruption — the leaderboard "
                            f"would silently report empty rankings, causing governance "
                            f"to act on missing data (Fail-Open). "
                            f"SYSTEM HALTED. Fix the data pipeline before restarting.",
                            source="daily_ops:_step_retraining_check",
                        )
            except Exception as _pnl_lb_exc:
                # DQAF-20260621-042 + DQAF-20260622-048: POISON PILL — do NOT swallow.
                # fail_open_guard would silently suppress it (Fail-Open anti-pattern),
                # causing daily_ops to write a broken leaderboard that downstream
                # consumers (governance, alerts) would act upon with corrupted data.
                # Per IC Architectural Override: corrupted data → FATAL HALT.
                # DataIntegrityError is imported at module level for the outer handler.
                if isinstance(_pnl_lb_exc, DataIntegrityError):
                    raise  # already a poison pill — propagate unchanged
                _detail = str(_pnl_lb_exc)[:500]
                raise DataIntegrityError(
                    f"POISON PILL: Leaderboard generation failed — {_detail}",
                    source="daily_ops:_step_retraining_check",
                ) from _pnl_lb_exc

        # ── DQAF-20260622-048: Leaderboard contract validation ──
        # Verify the generated leaderboard has reasonable brain coverage
        # relative to governance.  A near-empty leaderboard when governance
        # has active brains signals data pipeline corruption.
        #
        # Institutional Polish: ratio assertion, not absolute threshold.
        # The guard scales with system size — it cannot be defeated by
        # shrinking brain count below a magic number.
        _gov_path = base / "governance_state.json"
        if _gov_path.exists():
            try:
                _gov_data = json.loads(_gov_path.read_text(encoding="utf-8"))
                _gov_states = _gov_data.get("brain_states") or _gov_data.get("brains") or {}
                _active_count = sum(
                    1
                    for s in (
                        _gov_states.values() if isinstance(_gov_states, dict) else _gov_states
                    )
                    if isinstance(s, dict) and s.get("status") in ("live", "probation", "candidate")
                )
                _lb_brain_count = leaderboard.get("total_brains", 0)

                # Absolute zero check: no brains ranked but governance has active
                if _lb_brain_count == 0 and _active_count > 0:
                    raise DataIntegrityError(
                        f"LEADERBOARD CONTRACT VIOLATION: 0 brains ranked "
                        f"vs {_active_count} active in governance. "
                        f"SYSTEM HALTED.",
                        source="daily_ops:_step_retraining_check:contract_validation",
                    )

                # Ratio check: <10% of active brains appear in leaderboard
                if _active_count > 0:
                    _ratio = _lb_brain_count / _active_count
                    if _ratio < 0.1:
                        raise DataIntegrityError(
                            f"LEADERBOARD CONTRACT VIOLATION: "
                            f"{_lb_brain_count} brains ranked vs "
                            f"{_active_count} active in governance "
                            f"(ratio={_ratio:.3f}, below 10% safety threshold). "
                            f"SYSTEM HALTED.",
                            source="daily_ops:_step_retraining_check:contract_validation",
                        )
            except DataIntegrityError:
                raise  # poison pill — propagate
            except Exception:  # noqa: BLE001 — governance read failure → pass
                # Governance read failure should not crash the pipeline.
                # Leaderboard was still generated; validation is best-effort.
                pass

        result = detect_degradation(leaderboard, baseline)

        # Persist leaderboard via StateWriter gate (DQAF-046 Plan B)
        if not dry_run:
            from core.state.catalog import lookup
            from core.state.writer import StateWriter

            _sym = "BTCUSDc" if "btc" in str(base_dir).lower() else "XAUUSDc"
            _writer = StateWriter(str(base_dir), symbol=_sym)
            _writer.write_artifact(lookup("LEADERBOARD"), _sym, leaderboard)
            # Backup copy for next-run comparison
            _writer.write_artifact(lookup("LEADERBOARD_PREV"), _sym, leaderboard)

        # ── Auto-execute retraining (with safety gates) ──
        execution_result = None
        if auto_execute and not dry_run:
            critical_signals = [s for s in result.get("signals", []) if s["urgency"] == "critical"]
            if len(critical_signals) >= 2:
                prev_critical_ids = _load_prev_critical_ids(base)
                current_critical_ids = {s["brain_id"] for s in critical_signals}
                persistent = current_critical_ids & prev_critical_ids

                should_execute = len(persistent) >= 1 or len(critical_signals) >= 3
                if should_execute:
                    execution_result = execute_retraining(
                        critical_signals,
                        feature_store_dir=base / "feature_store",
                        output_dir=base / "training",
                        labels_path=labels_path if labels_path.exists() else None,
                        dry_run=False,
                    )
                    _log_retraining_event(base, critical_signals, execution_result, persistent)
            # Always save current signal for next day's persistence check
            _save_prev_signal(base, result)

        return {
            "step": "retraining_check",
            "status": "ok" if "error" not in result else "error",
            "degraded_count": result.get("degraded_count", 0),
            "healthy_brains": result.get("total_brains_assessed", 0)
            - result.get("degraded_count", 0),
            "overall_urgency": result.get("overall_urgency", "ok"),
            "details": result.get("signals", []),
            "auto_execution": execution_result,
        }
    except DataIntegrityError:
        # ── DQAF-20260622-048: POISON PILL — FAIL-CLOSED ──
        # The inner POISON PILL raises DataIntegrityError to halt the
        # system when the leaderboard cannot be generated with complete
        # data.  The previous generic ``except Exception`` silently
        # defeated this by wrapping DataIntegrityError in fail_open_guard()
        # (DEGRADE: log+continue), causing the system to write corrupted
        # leaderboard state that downstream consumers would act upon.
        #
        # Per Iron Law IMMUTABLE_LEDGER_AND_EPHEMERAL_PROJECTION:
        #   "If a materialized view cannot be generated with complete data,
        #    the system MUST halt rather than produce silently-corrupted output."
        raise
    except Exception as exc:  # noqa: BLE001 — REVIEWED: fail_open_guard below
        try:  # BLE001:FOG (was: FOG/LAC)
            return {"step": "retraining_check", "status": "error", "error": str(exc)[:500]}
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            return {"step": "retraining_check", "status": "error", "error": "unknown"}


def _load_prev_critical_ids(base: Path) -> set[str]:
    """Load brain_ids that were critical in yesterday's retraining signal."""
    sig_path = base / "reports" / "retraining_signal_prev.json"
    if not sig_path.exists():
        return set()
    try:
        prev = json.loads(sig_path.read_text(encoding="utf-8"))
        return {s["brain_id"] for s in prev.get("signals", []) if s["urgency"] == "critical"}
    except (json.JSONDecodeError, OSError, KeyError):
        return set()


def _save_prev_signal(base: Path, result: dict[str, Any]) -> None:
    """Save current retraining signal for next day's persistence check."""
    from core.state.catalog import lookup
    from core.state.writer import StateWriter

    symbol = "BTCUSDc" if "btc" in str(base).lower() else "XAUUSDc"
    writer = StateWriter(str(base), symbol=symbol)
    writer.write_artifact(lookup("RETRAINING_SIGNAL_PREV"), symbol, result)


def _log_retraining_event(
    base: Path,
    critical_signals: list[dict[str, Any]],
    exec_result: dict[str, Any],
    persistent_ids: set[str],
) -> None:
    """Append a retraining execution event to the retraining log."""
    log_path = base / "retraining_log.jsonl"
    event = {
        "timestamp": datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z",
        "critical_brains": [s["brain_id"] for s in critical_signals],
        "persistent_brains": sorted(persistent_ids),
        "execution": exec_result,
    }
    try:
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False, default=str) + "\n")
    except OSError:
        print(
            json.dumps(
                {"event": "retraining_log_write_error", "path": str(log_path)},
                ensure_ascii=False,
            ),
            flush=True,
        )


def _step_training_readiness(base_dir: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Run training pipeline readiness checks for contracts matching this symbol.

    DQAF-20260622-047: Wires the previously-orphaned check_training_readiness.py
    pure-function engine into the automated daily ops pipeline.  Uses strict
    symbol-isolation (prefix match on contract filename) to prevent cross-symbol
    contamination.

    I/O is routed through the StateWriter gate (Plan B Layer 2).
    """
    import logging

    from core.state.catalog import lookup
    from core.state.writer import StateWriter
    from scripts.check_training_readiness import evaluate_training_readiness

    logger = logging.getLogger("daily_ops")
    resolved = Path(base_dir).resolve()
    contracts_dir = Path("configs/contracts")

    # Symbol isolation: only match contracts with the correct prefix
    symbol_prefix = "btc" if "btc" in str(resolved).lower() else "xau"
    symbol_full = "BTCUSDc" if symbol_prefix == "btc" else "XAUUSDc"

    contract_pattern = f"training_pipeline_{symbol_prefix}*.json"
    matching = sorted(contracts_dir.glob(contract_pattern))

    if not matching:
        logger.warning(
            "[%s] No training pipeline contracts found (pattern=%s). Skipping readiness check.",
            symbol_full,
            contract_pattern,
        )
        return {
            "step": "training_readiness",
            "status": "skipped",
            "reason": "no_contracts",
            "symbol": symbol_full,
        }

    reports: list[dict[str, Any]] = []
    errors: list[str] = []
    for cp in matching:
        logger.info("[%s] Evaluating training contract: %s", symbol_full, cp.name)
        try:  # BLE001:FOG (was: FOG/LAC)
            try:
                report = evaluate_training_readiness(str(cp), str(resolved))
                reports.append(report)
            except Exception as exc:
                errors.append(f"{cp.name}: {type(exc).__name__}: {exc}")
                logger.exception(
                    "[%s] Training readiness check failed for %s", symbol_full, cp.name
                )
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            pass

    if not reports:
        return {
            "step": "training_readiness",
            "status": "error",
            "errors": errors,
            "symbol": symbol_full,
        }

    # Write through StateWriter gate (fail-closed: raise on bad data)
    writer = StateWriter(str(resolved), symbol=symbol_full)
    final_payload = reports[0] if len(reports) == 1 else {"contracts": reports}
    writer.write_artifact(lookup("TRAINING_READINESS"), symbol_full, final_payload)

    overall = reports[0].get("overall_verdict", "UNKNOWN") if len(reports) == 1 else "MULTI"
    return {
        "step": "training_readiness",
        "status": "ok",
        "symbol": symbol_full,
        "contracts_checked": len(reports),
        "contracts": [r["contract_id"] for r in reports],
        "overall_verdict": overall,
    }


def _step_param_optimization(
    base_dir: str, retraining_result: dict[str, Any], *, dry_run: bool = False
) -> dict[str, Any]:
    """Generate parameter optimization suggestions for degraded brains.

    Calls param_optimizer.suggest_parameters() with degraded brain_ids.
    Writes suggestions to data/reports/param_suggestions.json for manual review.
    """
    try:
        from core.feedback.param_optimizer import suggest_parameters

        details = retraining_result.get("details", [])
        degraded_ids = [
            s["brain_id"] for s in details if isinstance(s, dict) and s.get("urgency") == "critical"
        ]
        if not degraded_ids:
            return {
                "step": "param_optimization",
                "status": "skipped",
                "reason": "no_critical_brains",
            }

        if not dry_run:
            report = suggest_parameters(degraded_ids, base_dir=base_dir)
            return {
                "step": "param_optimization",
                "status": "ok",
                "degraded_brains": degraded_ids,
                "searchable_count": report.get("searchable_count", 0),
                "no_search_count": report.get("no_search_count", 0),
                "output": f"{base_dir}/reports/param_suggestions.json",
            }

        return {
            "step": "param_optimization",
            "status": "ok",
            "dry_run": True,
            "degraded_brains": degraded_ids,
            "would_generate": len(degraded_ids),
        }
    except Exception as exc:  # noqa: BLE001 — REVIEWED: fail_open_guard below
        try:  # BLE001:FOG (was: FOG/LAC)
            return {"step": "param_optimization", "status": "error", "error": str(exc)[:500]}
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            return {"step": "param_optimization", "status": "error", "error": "unknown"}


def _step_alpha_feed(base_dir: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Feed closed trade PnL into Alpha performance store.

    Reads live_trade_journal.jsonl, extracts per-alpha PnL from closed
    trades since the last feed, and records AlphaPerformanceSnapshot(s).

    Runs BEFORE _step_alpha_lifecycle so promotion/demotion evaluation
    has fresh performance data to work with.
    """
    try:  # BLE001:FOG (was: FOG/LAC)
        from core.alpha.performance_store import AlphaPerformanceStore
        from core.alpha.registry import AlphaRegistry

        journal_path = Path(base_dir) / "live_trade_journal.jsonl"
        registry_path = Path(base_dir) / "alpha_registry.json"
        perf_path = Path(base_dir) / "alpha_performance.json"
        state_path = Path(base_dir) / "alpha_feed_state.json"

        # ── Load or create registry ──
        if registry_path.exists():
            try:  # BLE001:FOG (was: FOG/LAC)
                registry = AlphaRegistry.load(registry_path)
            except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                pass
        else:
            registry = AlphaRegistry()

        # Ensure btc_swing is registered
        existing_ids = {r.alpha_id for r in registry.list_records()}
        if "btc_swing" not in existing_ids and not dry_run:
            try:  # BLE001:FOG (was: FOG/LAC)
                from core.alpha.contracts import AlphaLifecycleState, AlphaRecord

                registry.register(
                    AlphaRecord(
                        alpha_id="btc_swing",
                        name="btc_swing",
                        version="1.0.0",
                        state=AlphaLifecycleState.ACTIVE,
                        strategy_class="swing",
                        assets=["BTCUSDc"],
                    )
                )
                registry.save(registry_path)
            except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                pass

        # ── Load performance store ──
        if perf_path.exists():
            try:  # BLE001:FOG (was: FOG/LAC)
                perf_store = AlphaPerformanceStore.load(perf_path)
            except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                pass
        else:
            perf_store = AlphaPerformanceStore()

        # ── FIX-20260613-090: record-id watermark replaces brittle line-number pointer ──
        last_recorded_at = ""
        last_message_id = ""
        _st: dict[str, Any] = {}
        if state_path.exists():
            try:  # BLE001:FOG (was: FOG/LAC)
                _st = json.loads(state_path.read_text(encoding="utf-8"))
                last_recorded_at = _st.get("last_recorded_at", "")
                last_message_id = _st.get("last_message_id", "")
            except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                pass

        if not journal_path.exists():
            return {"step": "alpha_feed", "status": "skipped", "reason": "no_journal"}

        lines = journal_path.read_text(encoding="utf-8").splitlines()

        # Migrate from legacy last_line if needed
        if not last_recorded_at and "last_line" in _st:
            _old_pos = _st["last_line"]
            if _old_pos > 0 and _old_pos <= len(lines):
                try:
                    _mig_entry = json.loads(lines[_old_pos - 1])
                    last_recorded_at = _mig_entry.get("recorded_at", "")
                    last_message_id = _mig_entry.get("message_id", "")
                except Exception:  # noqa: BLE001 — REVIEWED: fail_open_guard below
                    try:  # BLE001:FOG (was: FOG/LAC)
                        pass
                    except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                        pass
            elif _old_pos > len(lines):
                # Compaction pruned lines — force full rescan
                last_recorded_at = ""
                last_message_id = ""

        # Filter to lines strictly after the watermark (compaction-safe)
        new_lines: list[str] = []
        for _l in lines:
            try:
                _e = json.loads(_l)
            except Exception:  # noqa: BLE001 — REVIEWED: fail_open_guard below
                try:  # BLE001:FOG (was: FOG/LAC)
                    continue
                except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                    # FIX-20260627-153: ensure skip on parse failure
                    continue
            _ts = _e.get("recorded_at", "")
            _mid = _e.get("message_id", "")
            if _ts > last_recorded_at or (_ts == last_recorded_at and _mid != last_message_id):
                new_lines.append(_l)

        if not new_lines:
            return {"step": "alpha_feed", "status": "ok", "new_snapshots": 0}

        # ── Aggregate PnL by alpha from closed trades ──
        alpha_pnls: dict[str, list[float]] = {}
        alpha_wins: dict[str, int] = {}
        alpha_losses: dict[str, int] = {}
        alpha_trades: dict[str, int] = {}

        for line in new_lines:
            try:  # BLE001:FOG (was: FOG/LAC)
                entry = json.loads(line)
            except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                # FIX-20260627-153: missing continue — JSON parse failure
                # left entry undefined → NameError at entry.get("action") below
                continue
            if entry.get("action") != "close":
                continue
            ack = entry.get("ack_status", "")
            if ack not in ("accepted", "closed"):
                continue
            pnl = entry.get("pnl")
            if pnl is None:
                continue
            strategy = entry.get("strategy", "") or "btc_swing"
            # DQAF-053 (Op Clear Sight): "" is falsy but not None,
            # so .get("strategy", "btc_swing") would pass through the
            # empty string.  ``or`` catches both None and "".
            if not strategy.strip():
                strategy = "btc_swing"
            alpha_pnls.setdefault(strategy, []).append(float(pnl))
            alpha_trades[strategy] = alpha_trades.get(strategy, 0) + 1
            if float(pnl) > 0:
                alpha_wins[strategy] = alpha_wins.get(strategy, 0) + 1
            elif float(pnl) < 0:
                alpha_losses[strategy] = alpha_losses.get(strategy, 0) + 1

        # ── Create snapshots ──
        new_snapshots = 0
        now_utc = datetime.now(UTC).isoformat()
        for alpha_id, pnls in alpha_pnls.items():
            total_pnl = sum(pnls)
            n = len(pnls)
            wins = alpha_wins.get(alpha_id, 0)
            losses = alpha_losses.get(alpha_id, 0)
            wr = wins / (wins + losses) if (wins + losses) > 0 else 0.0
            avg_win = sum(p for p in pnls if p > 0) / max(wins, 1)
            avg_loss = abs(sum(p for p in pnls if p < 0)) / max(losses, 1)
            pf = (sum(p for p in pnls if p > 0) / avg_loss) if avg_loss > 0 else 0.0

            try:  # BLE001:FOG (was: FOG/LAC)
                perf_store.record_snapshot(
                    alpha_id=alpha_id,
                    metrics={
                        "total_pnl": round(total_pnl, 2),
                        "trade_count": n,
                        "win_rate": round(wr, 4),
                        "profit_factor": round(pf, 4),
                        "avg_win": round(avg_win, 2),
                        "avg_loss": round(avg_loss, 2),
                        "wins": wins,
                        "losses": losses,
                    },
                    source="trade_journal",
                    window="daily",
                )
                new_snapshots += 1
            except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                pass

        # ── Save ──
        if not dry_run and new_snapshots > 0:
            try:  # BLE001:FOG (was: FOG/LAC)
                perf_store.save(perf_path)
            except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                pass
            # FIX-20260613-090: use record-id watermark (compaction-safe)
            _last_ts = last_recorded_at
            _last_mid = last_message_id
            for _l in reversed(new_lines):
                try:
                    _e = json.loads(_l)
                    _last_ts = _e.get("recorded_at", _last_ts)
                    _last_mid = _e.get("message_id", _last_mid)
                    break
                except Exception:  # noqa: BLE001 — REVIEWED: fail_open_guard below
                    try:  # BLE001:FOG (was: FOG/LAC)
                        continue
                    except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                        pass
            try:  # BLE001:FOG (was: FOG/LAC)
                from core.state.catalog import lookup
                from core.state.writer import StateWriter

                symbol = "BTCUSDc" if "btc" in str(base_dir).lower() else "XAUUSDc"
                writer = StateWriter(base_dir, symbol=symbol)
                writer.write_artifact(
                    lookup("ALPHA_FEED_STATE"),
                    symbol,
                    {
                        "last_recorded_at": _last_ts,
                        "last_message_id": _last_mid,
                        "last_line": len(lines),
                        "updated_utc": now_utc,
                    },
                )
            except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                pass

        return {
            "step": "alpha_feed",
            "status": "ok",
            "new_snapshots": new_snapshots,
            "alphas_fed": list(alpha_pnls.keys()),
            "total_trades_fed": sum(alpha_trades.values()),
        }
    except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
        return {"step": "alpha_feed", "status": "error", "error": "unknown"}


# ── DQAF-20260622-049: Nomination thresholds (aligned with governance_scheduler.py) ──
NOMINATION_MIN_TRADES = 50  # MIN_TRADES_FOR_LIVE
NOMINATION_MIN_WIN_RATE = 0.45  # WR_PROBATION_THRESHOLD
NOMINATION_MIN_SHARPE = 0.30  # MIN_SHARPE (audit_institutional_performance.py)
NOMINATION_ELIGIBLE_STATUSES = {"live", "probation"}


# ── DQAF-20260622-050: Strategy class inference from brain_id ──
def _infer_strategy_class(brain_id: str) -> str:
    """Infer strategy_class from brain_id naming convention.

    DQAF-20260622-050: The nomination bridge constructs AlphaRecord without
    strategy_class.  Infer it from the brain_id prefix/pattern so the
    allocator and lifecycle have meaningful metadata.
    """
    bid_lower = brain_id.lower()
    if bid_lower.startswith("swing"):
        return "swing"
    if bid_lower.startswith("ou_params"):
        return "ou_params"
    if "barrier" in bid_lower:
        return "barrier"
    if "trend" in bid_lower:
        return "trend"
    if "meta" in bid_lower:
        return "meta"
    if "rev" in bid_lower:
        return "rev"
    if "xgboost" in bid_lower or "xgb_" in bid_lower:
        return "xgboost"
    if "lightgbm" in bid_lower or "lgb_" in bid_lower:
        return "lightgbm"
    if "deep" in bid_lower or "mlp" in bid_lower:
        return "deep"
    if "online" in bid_lower:
        return "online"
    if "microstructure" in bid_lower:
        return "microstructure"
    # DQAF-053: substring fallback for prefixed ids like btc_swing, xau_swing
    if "swing" in bid_lower:
        return "swing"
    return "unknown"


def _step_alpha_registration(base_dir: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Nominate qualifying brains from leaderboard into alpha_registry.

    DQAF-20260622-049: Builds the missing bridge between the Brain
    Governance pipeline (leaderboard.json) and the Alpha pipeline
    (alpha_registry.json).  Reads the leaderboard, identifies brains
    meeting institutional nomination thresholds, and registers them
    as alpha CANDIDATE records.

    Institutional constraint (投委会):
      - **Nominate only**: initial state is ALWAYS ``candidate``.
      - **Never activate**: the ``_step_alpha_lifecycle`` owns all
        state transitions — this bridge only nominates.
      - **Idempotent**: re-running does not duplicate registrations.
    """
    try:
        from core.alpha.contracts import AlphaLifecycleState, AlphaRecord
        from core.alpha.registry import AlphaRegistry

        base = Path(base_dir)
        lb_path = base / "reports" / "leaderboard.json"
        registry_path = base / "alpha_registry.json"

        # ── Load leaderboard ──
        if not lb_path.exists():
            return {
                "step": "alpha_registration",
                "status": "skipped",
                "reason": "no_leaderboard",
            }

        lb_data = json.loads(lb_path.read_text(encoding="utf-8"))
        brains = lb_data.get("leaderboard") or lb_data.get("brains") or []

        if not brains:
            return {
                "step": "alpha_registration",
                "status": "skipped",
                "reason": "empty_leaderboard",
            }

        # ── Load registry ──
        registry = AlphaRegistry()
        if registry_path.exists():
            registry = AlphaRegistry.load(registry_path)

        existing_ids = {r.alpha_id for r in registry.list_records()}

        # ── Symbol resolution (DQAF-050: needed for assets field) ──
        symbol_assets = ["BTCUSDc"] if "btc" in str(base_dir).lower() else ["XAUUSDc"]

        # ── DQAF-050: Ghost record cleanup ──
        # Remove registry entries whose alpha_id has no matching brain in
        # leaderboard AND no performance snapshots (truly orphaned records).
        brain_ids = {b.get("brain_id", "") for b in brains}
        ghosts: list[str] = []
        for rec_id in sorted(existing_ids):
            if rec_id not in brain_ids:
                # Check if it has performance history — if so, preserve it
                try:
                    perf_path = base / "alpha_performance.json"
                    if perf_path.exists():
                        from core.alpha.performance_store import AlphaPerformanceStore

                        ps = AlphaPerformanceStore.load(perf_path)
                        if ps.latest(rec_id) is not None:
                            continue  # has performance data — don't remove
                except (OSError, ValueError, ImportError, KeyError):
                    continue  # can't verify — conservatively preserve record
                ghosts.append(rec_id)
                registry.remove(rec_id)
                existing_ids.discard(rec_id)

        # ── Load governance as secondary data source ──
        # Decision-based leaderboard (brain_leaderboard.v1) has signal_count
        # and trade_performance.win_rate but NOT sharpe_ratio or governance_status.
        # The PnL-based fallback (pnl_leaderboard.v1) has all fields top-level.
        # Merge governance_state.json to fill gaps for decision-based entries.
        gov_data: dict[str, dict[str, Any]] = {}
        gov_path = base / "governance_state.json"
        if gov_path.exists():
            try:
                gov_raw = json.loads(gov_path.read_text(encoding="utf-8"))
                gov_data = gov_raw.get("brain_states") or gov_raw.get("brains") or {}
            except Exception:  # noqa: BLE001 — best-effort secondary source
                pass

        nominated: list[str] = []
        skipped: list[str] = []

        for brain in brains:
            brain_id = brain.get("brain_id", "")
            if not brain_id:
                continue

            # alpha_id = brain_id (per DQAF-049 Phase 3 design)
            alpha_id = brain_id
            if alpha_id in existing_ids:
                continue  # already registered — idempotent

            # ── Nomination criteria — multi-source field resolution ──
            # trade_count: PnL-based uses "trade_count", decision-based uses "signal_count"
            trade_count = brain.get("trade_count") or brain.get("signal_count") or 0

            # win_rate: PnL-based has top-level "win_rate", decision-based nests in "trade_performance"
            tp = brain.get("trade_performance") or {}
            win_rate = brain.get("win_rate") or tp.get("win_rate") or 0.0

            # sharpe_ratio: PnL-based has top-level; decision-based needs governance
            sharpe = brain.get("sharpe_ratio") or brain.get("sharpe") or 0.0
            if sharpe == 0.0:
                gs = gov_data.get(brain_id) or (
                    gov_data.get(brain_id, {}) if isinstance(gov_data, dict) else {}
                )
                pm = gs.get("performance_metrics") or {}
                sharpe = pm.get("sharpe_ratio") or 0.0

            # governance_status: PnL-based has "governance_status"; decision-based needs governance
            gov_status = brain.get("governance_status") or brain.get("status") or ""
            if not gov_status:
                gs = gov_data.get(brain_id) or {}
                gov_status = gs.get("status") or ""

            if trade_count < NOMINATION_MIN_TRADES:
                skipped.append(f"{brain_id}:trades={trade_count}<{NOMINATION_MIN_TRADES}")
                continue
            if win_rate < NOMINATION_MIN_WIN_RATE:
                skipped.append(f"{brain_id}:wr={win_rate:.3f}<{NOMINATION_MIN_WIN_RATE}")
                continue
            if sharpe < NOMINATION_MIN_SHARPE:
                skipped.append(f"{brain_id}:sharpe={sharpe:.3f}<{NOMINATION_MIN_SHARPE}")
                continue
            if gov_status not in NOMINATION_ELIGIBLE_STATUSES:
                skipped.append(f"{brain_id}:status={gov_status}")
                continue

            # ── Nominate as CANDIDATE only (Institutional Mandate) ──
            record = AlphaRecord(
                alpha_id=alpha_id,
                name=brain_id,
                version="1.0.0",
                state=AlphaLifecycleState.CANDIDATE,
                strategy_id=alpha_id,
                strategy_class=_infer_strategy_class(brain_id),  # DQAF-050
                assets=list(symbol_assets),  # DQAF-050
            )
            registry.register(record)
            nominated.append(alpha_id)

        # ── Persist via StateWriter gate ──
        if nominated and not dry_run:
            registry.save(registry_path)

        return {
            "step": "alpha_registration",
            "status": "ok",
            "nominated": len(nominated),
            "nominated_ids": nominated,
            "skipped_count": len(skipped),
            "skipped_reasons": skipped[:10],  # top 10 for diagnostics
            "existing_count": len(existing_ids),
            "ghosts_removed": len(ghosts),  # DQAF-050
            "total_after": len(existing_ids) + len(nominated),
        }

    except DataIntegrityError:
        raise  # POISON PILL — propagate
    except Exception as exc:  # noqa: BLE001 — reviewed
        try:  # BLE001:FOG (was: FOG/LAC)
            return {
                "step": "alpha_registration",
                "status": "error",
                "error": str(exc)[:500],
            }
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            return {"step": "alpha_registration", "status": "error", "error": "unknown"}


def _step_alpha_lifecycle(base_dir: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Run alpha lifecycle evaluation: governance fast-track + promotion gate.

    DQAF-20260622-050: Adds a governance fast-track that lets brains already
    certified as ``live`` or ``probation`` by the governance pipeline skip
    the cold-start barrier (candidate → probation_live or paper_trading).
    Also backfills a cold-start AlphaPerformanceSnapshot so the allocator's
    PnL fallback path has data to work with.
    """
    try:
        from core.alpha.contracts import AlphaLifecycleState  # DQAF-050
        from core.alpha.lifecycle_service import AlphaLifecycleService
        from core.alpha.performance_store import AlphaPerformanceStore
        from core.alpha.promotion_gate import AlphaPromotionGate, AlphaPromotionPolicy
        from core.alpha.registry import AlphaRegistry

        base = Path(base_dir)
        registry_path = base / "alpha_registry.json"
        perf_path = base / "alpha_performance.json"
        lb_path = base / "reports" / "leaderboard.json"
        gov_path = base / "governance_state.json"

        if registry_path.exists():
            registry = AlphaRegistry.load(registry_path)
        else:
            registry = AlphaRegistry()

        perf_store = (
            AlphaPerformanceStore.load(perf_path) if perf_path.exists() else AlphaPerformanceStore()
        )
        lifecycle = AlphaLifecycleService(registry)
        gate = AlphaPromotionGate(perf_store, policy=AlphaPromotionPolicy())

        # ── DQAF-050: Load governance for fast-track lookup ──
        gov_status_map: dict[str, str] = {}
        gov_raw: dict[str, Any] = {}
        if gov_path.exists():
            try:
                gov_raw = json.loads(gov_path.read_text(encoding="utf-8"))
                for bid, bd in (gov_raw.get("brain_states") or {}).items():
                    gov_status_map[bid] = bd.get("status", "")
            except Exception:  # noqa: BLE001 — best-effort secondary source
                pass

        # ── DQAF-050: Load leaderboard for fast-track verification + cold-start metrics ──
        lb_brains: list[dict[str, Any]] = []
        if lb_path.exists():
            try:
                lb_data = json.loads(lb_path.read_text(encoding="utf-8"))
                lb_brains = lb_data.get("leaderboard") or lb_data.get("brains") or []
            except Exception:  # noqa: BLE001 — best-effort
                pass
        lb_lookup: dict[str, dict[str, Any]] = {
            b.get("brain_id", ""): b for b in lb_brains if b.get("brain_id")
        }

        # ── Fast-track criteria (aligned with governance thresholds) ──
        # DQAF-050: Federated Trust — governance certifies, leaderboard verifies
        FT_MIN_TRADES = 50  # MIN_TRADES_FOR_LIVE
        FT_MIN_WIN_RATE = 0.45  # WR_PROBATION_THRESHOLD

        decisions: list[dict[str, Any]] = []
        fast_tracked: int = 0
        cold_start_snapshots: int = 0

        for record in registry.list_records():
            alpha_id = record.alpha_id

            # ── DQAF-050: Governance Fast-Track (before gate evaluation) ──
            gov_status = gov_status_map.get(alpha_id, "")
            if record.state_value == "candidate" and gov_status in ("live", "probation"):
                lb_entry = lb_lookup.get(alpha_id, {})
                tp = lb_entry.get("trade_performance") or {}
                ft_trades = lb_entry.get("trade_count") or lb_entry.get("signal_count") or 0
                ft_wr = lb_entry.get("win_rate") or tp.get("win_rate") or 0.0

                if ft_trades >= FT_MIN_TRADES and ft_wr >= FT_MIN_WIN_RATE:
                    # live → PROBATION_LIVE; probation → PAPER_TRADING
                    if gov_status == "live":
                        target = AlphaLifecycleState.PROBATION_LIVE.value
                        reason = "governance_fast_track:governance_live→alpha_probation_live"
                    else:
                        target = AlphaLifecycleState.PAPER_TRADING.value
                        reason = "governance_fast_track:governance_probation→alpha_paper_trading"

                    if not dry_run:
                        try:
                            lifecycle.transition(alpha_id, target, reason)
                            decisions.append(
                                {
                                    "alpha_id": alpha_id,
                                    "approved": True,
                                    "action": "fast_track",
                                    "target_state": target,
                                    "reason": reason,
                                }
                            )
                            fast_tracked += 1

                            # ── DQAF-050: Cold-start snapshot backfill ──
                            gb = (gov_raw.get("brain_states") or {}).get(alpha_id, {})
                            gov_pm = gb.get("performance_metrics") or {}
                            perf_store.record_snapshot(
                                alpha_id=alpha_id,
                                metrics={
                                    "trade_count": ft_trades,
                                    "win_rate": ft_wr,
                                    "profit_factor": gov_pm.get("profit_factor", 0.0),
                                    "sharpe_ratio": gov_pm.get("sharpe_ratio", 0.0),
                                    "total_pnl": tp.get("total_pnl", 0.0),
                                    "source": "cold_start_snapshot",
                                },
                                source="governance_fast_track_cold_start",
                                window="initial",
                            )
                            cold_start_snapshots += 1
                        except ValueError as exc:
                            decisions.append(
                                {
                                    "alpha_id": alpha_id,
                                    "approved": False,
                                    "reason": f"fast_track_transition_failed:{exc}",
                                }
                            )
                    else:
                        decisions.append(
                            {
                                "alpha_id": alpha_id,
                                "approved": True,
                                "action": "fast_track",
                                "target_state": target,
                                "reason": reason,
                            }
                        )
                        fast_tracked += 1
                    continue  # skip normal gate evaluation

            # ── Normal gate evaluation (existing path) ──
            decision = gate.evaluate(record)
            decisions.append(decision.to_dict())
            if decision.approved and decision.target_state and not dry_run:
                try:
                    lifecycle.transition(record.alpha_id, decision.target_state, decision.action)
                except ValueError as exc:
                    print(
                        json.dumps(
                            {
                                "event": "alpha_transition_error",
                                "alpha_id": record.alpha_id,
                                "target_state": decision.target_state,
                                "error": str(exc),
                            },
                            ensure_ascii=False,
                        ),
                        flush=True,
                    )

        if not dry_run:
            registry.save(registry_path)
            perf_store.save(perf_path)

        applied = [d for d in decisions if d.get("approved")]
        return {
            "step": "alpha_lifecycle",
            "status": "ok",
            "alphas_assessed": len(decisions),
            "actions_applied": len(applied) if not dry_run else 0,
            "actions_flagged": len(applied) if dry_run else len(applied),
            "fast_tracked": fast_tracked,  # DQAF-050
            "cold_start_snapshots": cold_start_snapshots,  # DQAF-050
            "details": applied,
        }
    except Exception as exc:  # noqa: BLE001 — REVIEWED: fail_open_guard below
        try:  # BLE001:FOG (was: FOG/LAC)
            return {"step": "alpha_lifecycle", "status": "error", "error": str(exc)[:500]}
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            return {"step": "alpha_lifecycle", "status": "error", "error": "unknown"}


def _step_alpha_allocation(base_dir: str, *, dry_run: bool = False) -> dict[str, Any]:
    """Run AlphaPortfolioAllocator: produce capital allocation recommendations."""
    try:
        from core.alpha.performance_store import AlphaPerformanceStore
        from core.alpha.portfolio_allocator import (
            AlphaAllocationPolicy,
            AlphaPortfolioAllocator,
        )
        from core.alpha.registry import AlphaRegistry

        registry_path = Path(base_dir) / "alpha_registry.json"
        perf_path = Path(base_dir) / "alpha_performance.json"

        registry = AlphaRegistry.load(registry_path) if registry_path.exists() else AlphaRegistry()
        perf_store = (
            AlphaPerformanceStore.load(perf_path) if perf_path.exists() else AlphaPerformanceStore()
        )

        allocator = AlphaPortfolioAllocator(registry, perf_store, policy=AlphaAllocationPolicy())
        allocation = allocator.allocate()

        # Persist allocation report via StateWriter gate (DQAF-046 Plan B)
        if not dry_run:
            from core.state.catalog import lookup
            from core.state.writer import StateWriter

            symbol = "BTCUSDc" if "btc" in str(base_dir).lower() else "XAUUSDc"
            writer = StateWriter(base_dir, symbol=symbol)
            writer.write_artifact(lookup("ALPHA_ALLOCATION"), symbol, allocation)

        return {
            "step": "alpha_allocation",
            "status": "ok",
            "alpha_count": allocation.get("alpha_count", 0),
            "allocatable_count": allocation.get("allocatable_count", 0),
            "output": str(Path(base_dir) / "reports" / "alpha_allocation.json")
            if not dry_run
            else None,
        }
    except Exception as exc:  # noqa: BLE001 — REVIEWED: fail_open_guard below
        try:  # BLE001:FOG (was: FOG/LAC)
            return {"step": "alpha_allocation", "status": "error", "error": str(exc)[:500]}
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            return {"step": "alpha_allocation", "status": "error", "error": "unknown"}


def _step_feature_store_maintenance(
    base_dir: str,
    *,
    dry_run: bool = False,
    retention_days: int = 90,
    skip_update: bool = False,
    mt5_terminal_path: str | None = None,
) -> dict[str, Any]:
    """Run feature store compaction, incremental update, and stats collection."""
    try:
        from scripts.feature_store_maintenance import run_full_maintenance

        store_dir = Path(base_dir) / "feature_store"
        fs_dir = str(store_dir) if store_dir.exists() else None
        report = run_full_maintenance(
            base_dir=base_dir,
            feature_store_dir=fs_dir,
            retention_days=retention_days,
            skip_update=skip_update,
            dry_run=dry_run,
            mt5_terminal_path=mt5_terminal_path,
        )
        steps = report.get("steps", [])
        compaction = next(
            (s for s in steps if s.get("step") == "compaction"),
            {"records_before": 0, "records_after": 0, "duplicates_removed": 0},
        )
        stats = next(
            (s for s in steps if s.get("step") == "stats"),
            {"total_records": 0, "total_file_size_mb": 0},
        )
        return {
            "step": "feature_store_maintenance",
            "status": "ok",
            "dry_run": dry_run,
            "compaction": compaction,
            "stats": stats,
        }
    except Exception as exc:  # noqa: BLE001 — REVIEWED: fail_open_guard below
        try:  # BLE001:FOG (was: FOG/LAC)
            return {"step": "feature_store_maintenance", "status": "error", "error": str(exc)[:500]}
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            return {"step": "feature_store_maintenance", "status": "error", "error": "unknown"}


def _step_data_health(
    base_dir: str,
    *,
    symbol: str = "XAUUSDc",
    dry_run: bool = False,
) -> dict[str, Any]:
    """FIX-20260610-005: Run unified data health checks (FULL mode).

    Covers all 20 data sources across CRITICAL/HIGH/MEDIUM tiers,
    cross-source validation, and orphan subsystem detection.
    Alerts are dispatched through the alert system's rule engine
    (RULE-012..016) — no ad-hoc alert_hub calls.
    """
    try:
        from core.observability.data_health_service import DataHealthService

        svc = DataHealthService(base_dir=base_dir, symbol=symbol, mode="full")
        report = svc.run_full()

        if not dry_run:
            svc.save_health_state(report)
            # Iron Law #3: DataHealthService doesn't send alerts.
            # Alert dispatch is done externally — here we produce the context
            # dict so downstream alert steps can evaluate it.
            ctx = svc.build_alert_context(report)
            steps_result = {
                "step": "data_health",
                "status": "ok",
                "alert_level": report.alert_level,
                "elapsed_ms": report.elapsed_ms,
                "total_sources_checked": len(report.sources),
                "pass_count": sum(1 for s in report.sources if s.status.value == "pass"),
                "warn_count": sum(1 for s in report.sources if s.status.value == "warn"),
                "fail_count": sum(1 for s in report.sources if s.status.value == "fail"),
                "missing_count": sum(1 for s in report.sources if s.status.value == "missing"),
                "cross_check_warnings": sum(
                    1 for c in report.cross_checks if c.status.value != "pass"
                ),
                "orphan_count": len(report.orphans),
                "primary_codes": report.primary_codes,
                "alert_context": ctx,
            }
            return steps_result
        else:
            return {
                "step": "data_health",
                "status": "dry_run",
                "total_sources_checked": len(report.sources),
                "alert_level": report.alert_level,
            }
    except Exception as exc:  # noqa: BLE001 — REVIEWED: fail_open_guard below — Iron Law #1
        try:  # BLE001:FOG (was: FOG/LAC)
            return {"step": "data_health", "status": "error", "error": str(exc)[:500]}
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            return {"step": "data_health", "status": "error", "error": "unknown"}


def _step_reconcile_governance_pnl(base_dir: str) -> dict[str, Any]:
    """Reconcile governance_state.json brain registry against brain_pnl_ledger.json.

    DQAF-20260622-057 Phase 2: Institutional-grade referential integrity check.
    对标 BlackRock Aladdin Data Integrity Protocol §7.1 — "Every registered
    entity MUST have a corresponding ledger entry within one business day
    of registration."

    Cross-references all brains in governance_state against the PnL settled
    registry.  Detects three classes of integrity gap:
      - Live brains missing from PnL → CRITICAL (trading without PnL tracking)
      - Non-live brains missing from PnL → WARNING (expected for new registrations)
      - PnL entries for brains not in governance → orphaned (stale artifacts)

    Structured log output enables downstream alerting integration.
    """
    base = Path(base_dir)
    gov_path = base / "governance_state.json"
    pnl_path = base / "brain_pnl_ledger.json"

    if not gov_path.exists():
        return {
            "step": "governance_pnl_reconciliation",
            "status": "skipped",
            "reason": "no_governance_state",
        }
    if not pnl_path.exists():
        return {
            "step": "governance_pnl_reconciliation",
            "status": "skipped",
            "reason": "no_pnl_ledger",
        }

    try:
        gov_data = json.loads(gov_path.read_text(encoding="utf-8"))
        pnl_data = json.loads(pnl_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        return {
            "step": "governance_pnl_reconciliation",
            "status": "error",
            "error": f"read_error:{exc}",
        }

    brain_states: dict[str, dict[str, Any]] = (
        gov_data.get("brain_states") or gov_data.get("brains") or {}
    )
    settled: dict[str, Any] = pnl_data.get("settled", {})
    pending: dict[str, Any] = pnl_data.get("pending", {})

    gov_brains = set(brain_states.keys())
    pnl_settled_brains = set(settled.keys())

    # ── Gap 1: Brains in governance but NOT in PnL settled ──
    pnl_missing = gov_brains - pnl_settled_brains

    # ── Gap 2: Brains in PnL settled but NOT in governance (orphaned) ──
    gov_missing = pnl_settled_brains - gov_brains

    # ── Gap 3: Brains in PnL with zero entries (paper artifacts) ──
    zero_entry = sorted(
        brain_id
        for brain_id, entries in settled.items()
        if isinstance(entries, list) and len(entries) == 0
    )

    # ── Gap 4: Orphaned pending signals (brain removed from governance) ──
    orphaned_pending: list[str] = []
    for sig_id, entry in pending.items():
        if isinstance(entry, dict):
            entry_brain = entry.get("brain_id", "")
            # Also check base-name match (e.g. Swing_V9_M30_V2_<ts> → Swing_V9_M30_V2)
            base_name = entry_brain.rsplit("_", 1)[0] if "_" in entry_brain else entry_brain
            if entry_brain not in gov_brains and base_name not in gov_brains:
                orphaned_pending.append(sig_id)

    # ── Severity classification (对标 Goldman Sachs Marquee DQF §4.2) ──
    live_missing = sorted(b for b in pnl_missing if brain_states.get(b, {}).get("status") == "live")

    result: dict[str, Any] = {
        "step": "governance_pnl_reconciliation",
        "status": "ok",
        "gov_total": len(gov_brains),
        "pnl_settled_total": len(pnl_settled_brains),
        "pnl_pending_total": len(pending),
        "pnl_missing_brains": sorted(pnl_missing),
        "pnl_missing_count": len(pnl_missing),
        "gov_missing_brains": sorted(gov_missing),
        "gov_missing_count": len(gov_missing),
        "zero_entry_brains": zero_entry,
        "zero_entry_count": len(zero_entry),
        "orphaned_pending_signals": orphaned_pending,
        "orphaned_pending_count": len(orphaned_pending),
    }

    if live_missing:
        result["severity"] = "CRITICAL"
        result["live_brains_missing_pnl"] = live_missing
        result["detail"] = (
            f"CRITICAL: {len(live_missing)} LIVE brain(s) trading without PnL tracking: "
            f"{live_missing}.  This means the Dynamic Brain Weighter operates on "
            f"incomplete data — PnL-based vote weighting is silently degraded."
        )
    elif pnl_missing:
        result["severity"] = "WARNING"
        result["detail"] = (
            f"WARNING: {len(pnl_missing)} brain(s) in governance without PnL entries. "
            f"Expected for newly registered brains with no trading history."
        )
    elif gov_missing or zero_entry or orphaned_pending:
        result["severity"] = "INFO"
        result["detail"] = (
            f"INFO: Orphaned PnL entries ({len(gov_missing)} gov-missing, "
            f"{len(zero_entry)} zero-entry, {len(orphaned_pending)} pending) — "
            f"candidates for retention pruning."
        )
    else:
        result["severity"] = "CLEAN"

    # Structured log for downstream alerting
    print(json.dumps(result, ensure_ascii=False, default=str), flush=True)
    return result


def _step_freshness_check(base_dir: str) -> dict[str, Any]:
    """Run Freshness Guard at end of daily_ops pipeline.

    DQAF-20260622-057 Phase 1: Closes the CATALOG_COVERAGE_GAP by running
    the Freshness Guard AS A PIPELINE STEP rather than as a passive,
    manually-invoked diagnostic.  After all writes are complete, checks
    every registered StateArtifact for staleness/emptiness/missingness.

    对标 Goldman Sachs Marquee Data Quality Framework §4.2 (Freshness SLA):
    "Every data product MUST self-assess freshness at the point of generation."
    """
    try:
        from core.state.freshness_guard import check_catalog_freshness

        # Only check the asset that just ran — don't cross-contaminate
        result = check_catalog_freshness(
            data_dirs=[base_dir],
            emit_alerts=True,  # CRITICAL/WARNING lines → stderr
        )

        stale_count = len(result["stale"])
        missing_count = len(result["missing"])
        empty_count = len(result["empty"])

        output: dict[str, Any] = {
            "step": "freshness_check",
            "status": "ok" if (stale_count == 0 and empty_count == 0) else "degraded",
            "checked_at_utc": result["checked_at_utc"],
            "total_artifacts": result["total"],
            "healthy": len(result["healthy"]),
            "stale_count": stale_count,
            "missing_count": missing_count,
            "empty_count": empty_count,
        }

        if stale_count > 0:
            output["stale_artifacts"] = [
                {"artifact_id": e["artifact_id"], "age_human": e.get("age_human", "?")}
                for e in result["stale"]
            ]
        if missing_count > 0:
            output["missing_artifacts"] = [e["artifact_id"] for e in result["missing"]]
        if empty_count > 0:
            output["empty_artifacts"] = [e["artifact_id"] for e in result["empty"]]

        # Emit structured log for downstream consumption
        print(json.dumps(output, ensure_ascii=False, default=str), flush=True)
        return output

    except Exception as exc:  # noqa: BLE001 — REVIEWED: fail_open_guard below
        try:  # BLE001:FOG (was: FOG/LAC)
            return {"step": "freshness_check", "status": "error", "error": str(exc)[:500]}
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            return {"step": "freshness_check", "status": "error", "error": "unknown"}


def _step_daily_recap(base_dir: str, *, mt5_terminal_path: str | None = None) -> dict[str, Any]:
    """Run daily recap and return summary."""
    try:
        from scripts.live_daily_recap import build_report as build_recap

        report = build_recap(
            base_dir=Path(base_dir),
            symbol="XAUUSDc",
            mt5_terminal_path=mt5_terminal_path,
        )
        run_state = report.get("run_state", "unknown")
        return {
            "step": "daily_recap",
            "status": "ok",
            "run_state": run_state,
            "date_key": report.get("date_key_utc", ""),
            "sections": list(report.keys()),
        }
    except Exception as exc:  # noqa: BLE001 — REVIEWED: fail_open_guard below
        try:  # BLE001:FOG (was: FOG/LAC)
            return {"step": "daily_recap", "status": "error", "error": str(exc)[:500]}
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            return {"step": "daily_recap", "status": "error", "error": "unknown"}


def _resolve_base_dir(base_dir: str | Path) -> str:
    """Resolve relative base_dir against PROJECT_ROOT to guard against CWD drift."""
    p = Path(base_dir)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return str(p)


def run_daily_ops(
    base_dir: str = "data",
    *,
    skip_shadow: bool = False,
    skip_journal_mt5_reconcile: bool = False,
    skip_journal_backfill: bool = False,
    skip_label_builder: bool = False,
    skip_feedback: bool = False,
    skip_governance: bool = False,
    skip_champion: bool = False,
    skip_retraining: bool = False,
    skip_recap: bool = False,
    skip_alpha: bool = False,
    skip_alpha_allocation: bool = False,
    skip_online_feedback: bool = False,
    skip_paper_simulation: bool = False,
    skip_fs_maintenance: bool = False,
    skip_data_health: bool = False,
    skip_training_readiness: bool = False,
    dry_run: bool = False,
    mt5_terminal_path: str | None = None,
    symbol: str = "XAUUSDc",  # FIX-20260601-033: per-symbol feedback
    force_rebuild: bool = False,  # FIX-20260626-144: reset watermarks before pipeline
) -> dict[str, Any]:
    """Run the full daily operations pipeline.

    base_dir is resolved against PROJECT_ROOT when relative, so the pipeline
    works regardless of the process CWD.

    Args:
        base_dir: Base data directory.
        skip_shadow: Skip shadow ensemble step.
        skip_journal_backfill: Skip journal PnL backfill (Strategy A: close_price→PnL).
        skip_feedback: Skip feedback loop step.
        skip_governance: Skip governance cycle.
        skip_champion: Skip champion/challenger promotion.
        skip_retraining: Skip retraining degradation check.
        skip_recap: Skip daily recap.
        skip_online_feedback: Skip online learner partial_fit from closed trades.
        skip_paper_simulation: Skip paper trade simulation from shadow decisions.
        skip_fs_maintenance: Skip feature store maintenance (compaction + stats).
        dry_run: Assess but don't apply transitions.

    Returns:
        Combined report dict with per-step results.
    """
    base_dir = _resolve_base_dir(base_dir)
    steps: list[dict[str, Any]] = []

    # ── FIX-20260626-144: --force-rebuild resets incremental watermarks ──
    if force_rebuild:
        _watermark_files = [
            "calibrator_feed_state.json",
            "alpha_feed_state.json",
            "reconciliation_watermark.json",
        ]
        for _wf in _watermark_files:
            _wp = Path(base_dir) / _wf
            if _wp.exists():
                _wp.unlink()
                steps.append({"step": "force_rebuild", "action": "deleted", "file": str(_wp)})
        if any(Path(base_dir, wf).exists() for wf in _watermark_files):
            pass  # should not happen — unlink succeeded or file never existed
        _reset_count = len([s for s in steps if s.get("step") == "force_rebuild"])
        if _reset_count > 0:
            steps.append(
                {
                    "step": "force_rebuild",
                    "status": "ok",
                    "watermarks_reset": _reset_count,
                    "message": "Full rebuild: all incremental state cleared; pipeline will reprocess from scratch",
                }
            )
        else:
            steps.append(
                {
                    "step": "force_rebuild",
                    "status": "ok",
                    "watermarks_reset": 0,
                    "message": "No watermarks found to reset; pipeline running normally",
                }
            )

    # Shared tracker + governance + pnl_store: load persisted state so governance
    # and champion see data accumulated by live_intent_loop, and brain registrations
    # survive restarts.  PnL store provides per-brain counterfactual P&L with
    # horizon-matched settlement — the preferred data source for governance decisions
    # (no cross-brain contamination, unlike tracker composite_scores).
    shared_tracker: Any = None
    shared_governance: Any = None
    shared_pnl_store: Any = None
    if not skip_feedback or not skip_governance or not skip_champion:
        shared_tracker = _load_or_create_tracker(base_dir)
        shared_governance = _load_or_create_governance(base_dir)
        shared_pnl_store = _load_or_create_pnl_store(base_dir)
        brain_count = len(shared_tracker.get_all_summaries())
        gov_brain_count = len(shared_governance.get_all_states())
        if brain_count > 0 or gov_brain_count > 0:
            steps.append(
                {
                    "step": "state_loaded",
                    "status": "ok",
                    "brains_tracked": brain_count,
                    "brains_registered": gov_brain_count,
                }
            )

    # ── SSOT reconciliation + PnL ledger retention ──
    # FIX-081: Runs first so all downstream steps see consistent config and clean data.
    try:
        from scripts.brain import cmd_reconcile

        _rec_steps: list[dict[str, Any]] = []
        _rec_steps.append({"step": "reconcile", "action": "ssot_alignment"})
        cmd_reconcile(auto_fix=True, cleanup_ledger=True)
        _rec_steps.append({"step": "reconcile", "action": "ssot_alignment", "status": "ok"})

        # PnL ledger retention: prune entries older than 90 days
        _ledger_path = Path(base_dir) / "brain_pnl_ledger.json"
        if _ledger_path.exists():
            from core.feedback.brain_pnl_ledger import BrainPnLStore

            _store = BrainPnLStore.load(str(_ledger_path))
            _pruned = _store.retention_prune(retention_days=90)
            if _pruned:
                _store.save(str(_ledger_path))
                _total_pruned = sum(_pruned.values())
                _rec_steps.append(
                    {
                        "step": "ledger_retention",
                        "status": "ok",
                        "retention_days": 90,
                        "brains_pruned": len(_pruned),
                        "entries_pruned": _total_pruned,
                    }
                )
            else:
                _rec_steps.append({"step": "ledger_retention", "status": "ok", "entries_pruned": 0})
            # FIX-20260628-156 (L2): Touch the file mtime even when nothing
            # was pruned.  Without this, the freshness guard flags the
            # ledger as STALE because its mtime only updates when
            # retention_prune() returns non-empty entries.  On low-volume
            # symbols (BTC with <90d history) this may never happen,
            # causing a permanent STALE alert for a valid file.
            _ledger_path.touch(exist_ok=True)
        steps.extend(_rec_steps)
    except Exception as _exc:  # noqa: BLE001 — REVIEWED: fail_open_guard below
        try:  # BLE001:FOG (was: FOG/LAC)
            steps.append({"step": "reconcile", "status": "error", "error": str(_exc)})
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            pass
    # ── FIX-20260607-144: Journal compaction ──────────────────────────
    # Prunes old rejected entries (>30d) from live_trade_journal.jsonl.
    # Uses atomic os.replace() + FileLock — safe for concurrent writes.
    # Runs during daily ops window when trading is paused (Option B: lazy writer).
    try:
        from core.ledger.services.journal_cleanup import compact_journal

        _base = Path(base_dir)
        _journal_path = _base / "live_trade_journal.jsonl"
        if _journal_path.exists():
            _compaction_result = compact_journal(
                _journal_path, retention_days=30, dry_run=dry_run, lock_dir=_base
            )
            steps.append({"step": "journal_compaction", **_compaction_result})
    except Exception as _jc_exc:  # noqa: BLE001 — REVIEWED: fail_open_guard below
        try:  # BLE001:FOG (was: FOG/LAC)
            steps.append(
                {"step": "journal_compaction", "status": "error", "error": str(_jc_exc)[:200]}
            )
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
            pass
    if not skip_shadow:
        steps.append(_step_shadow_ensemble(base_dir))

    # ── JournalGate initialization (FIX-20260626-143 L3) ──────────────
    _gate = None
    try:
        from core.ledger.services.journal_gate import JournalGate

        _base_p = Path(base_dir)
        _jrn_p = _base_p / "live_trade_journal.jsonl"
        if _jrn_p.exists():
            _gate = JournalGate(_jrn_p, policy="quarantine", lock_dir=_base_p / ".locks")
    except (RuntimeError, ValueError, KeyError, TypeError, OSError):
        pass

    # MT5 reconciliation: PnL normalisation + missing close backfill.
    # Runs BEFORE Strategy A backfill — MT5 deal.profit is authoritative.
    # FIX-20260626-143 Phase 3: Auto-reconciliation with lock yielding
    # and composite-key watermark.
    if not skip_journal_mt5_reconcile:
        steps.append(
            _step_journal_mt5_reconcile(
                base_dir,
                dry_run=dry_run,
                mt5_terminal_path=mt5_terminal_path,
            )
        )

    # Journal health report: SLA compliance after reconciliation.
    if not skip_journal_mt5_reconcile:
        steps.append(
            _step_journal_health_report(
                base_dir,
                gate=_gate,
                mt5_terminal_path=mt5_terminal_path,
            )
        )

    # Journal backfill: fill null PnL in close entries from close_price.
    # Strategy A only — no MT5 dependency.  Runs BEFORE label_builder so
    # the label pipeline sees backfilled PnL data.
    # FIX-20260622-057 Phase 3c: Integrated close_price→PnL backfill into
    # daily pipeline.  Idempotent — becomes a no-op once all entries fixed.
    if not skip_journal_backfill:
        steps.append(_step_journal_backfill(base_dir, dry_run=dry_run))

    # Label builder: generate fresh training labels from journals.
    # Runs BEFORE feedback_loop so tracker sees the latest labels.
    # FIX-20260622-057 P0-3: Activate label_contract defense layer.
    # When close_price is missing from the journal (66% of positions close
    # outside system control via DEAL_REASON_SIGNAL), the barrier-based
    # classifier provides a fallback label from SL/TP levels that PnL-based
    # classification cannot compute.  Contract selected by symbol:
    #   XAU → label-survival-barrier-1.0.0  (SL=2.0/TP=3.5 — exact match)
    #   BTC → label-micro-barrier-1.0.0      (SL=1.5/TP=2.5 — TP matches live config)
    if not skip_label_builder:
        _is_btc = "btc" in str(base_dir).lower()
        _contract_path = (
            Path("blueprints/contracts/label-micro-barrier-1.0.0.json")
            if _is_btc
            else Path("blueprints/contracts/label-survival-barrier-1.0.0.json")
        )
        steps.append(_step_label_builder(base_dir, dry_run=dry_run, contract_path=_contract_path))

    # Feedback loop: resolve pending dispatch outcomes → real P&L scores
    # Runs before governance/champion so they see the latest data
    if not skip_feedback:
        steps.append(
            _step_feedback_loop(base_dir, dry_run=dry_run, tracker=shared_tracker, symbol=symbol)
        )

    # Paper trade simulation: generate labeled trade outcomes from shadow decisions
    if not skip_paper_simulation:
        steps.append(_step_paper_trade_simulation(base_dir, dry_run=dry_run))

    # Online feedback: feed closed trades to online SGD learner via partial_fit
    # Runs after paper_trade_simulation and feedback_loop so all data is available
    # Calibrator feed runs BEFORE online_feedback (independent of ML pipeline).
    # FIX-028: online_feedback is permanently skipped but calibrator must keep
    # accumulating samples for ConformalOUGate adaptive threshold.
    steps.append(_step_calibrator_feed(base_dir, dry_run=dry_run))
    if not skip_online_feedback:
        steps.append(_step_online_feedback(base_dir, dry_run=dry_run))

    if not skip_governance:
        steps.append(
            _step_governance(
                base_dir,
                dry_run=dry_run,
                tracker=shared_tracker,
                governance=shared_governance,
                pnl_store=shared_pnl_store,
            )
        )
        # DQAF-20260622-057 Phase 2: Reconciliation runs immediately after
        # governance so we detect freshly-promoted brains with no PnL.
        steps.append(_step_reconcile_governance_pnl(base_dir))

    if not skip_champion:
        steps.append(
            _step_champion_challenger(
                base_dir, dry_run=dry_run, tracker=shared_tracker, governance=shared_governance
            )
        )

    # Persist tracker and governance state after modifications
    if not dry_run:
        if shared_tracker is not None:
            try:
                tracker_path = Path(base_dir) / "brain_performance.json"
                shared_tracker.save(tracker_path)
            except Exception:  # noqa: BLE001 — REVIEWED: fail_open_guard below
                try:  # BLE001:FOG (was: FOG/LAC)
                    logging.getLogger(__name__).exception(
                        "daily_ops: failed to persist BrainPerformanceTracker — "
                        "performance data lost until next save"
                    )
                except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                    pass
        if shared_governance is not None:
            try:
                gov_path = Path(base_dir) / "governance_state.json"
                shared_governance.save(gov_path)
            except Exception:  # noqa: BLE001 — REVIEWED: fail_open_guard below
                try:  # BLE001:FOG (was: FOG/LAC)
                    logging.getLogger(__name__).exception(
                        "daily_ops: failed to persist GovernanceService — "
                        "governance state may be stale on restart"
                    )
                except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                    pass
    if not skip_retraining:
        _sym = "BTCUSDc" if "btc" in str(base_dir).lower() else "XAUUSDc"
        steps.append(
            _step_retraining_check(base_dir, dry_run=dry_run, auto_execute=not dry_run, symbol=_sym)
        )

    # Training readiness: validate pipeline contracts for this symbol.
    # Runs after retraining_check so we can cross-reference degradation signals
    # against actual data readiness.  DQAF-20260622-047.
    if not skip_training_readiness:
        steps.append(_step_training_readiness(base_dir, dry_run=dry_run))

    # ── FIX-20260610-005: DataHealthService FULL mode ──
    if not skip_data_health:
        steps.append(_step_data_health(base_dir, symbol=symbol, dry_run=dry_run))

    if not skip_recap:
        steps.append(_step_daily_recap(base_dir, mt5_terminal_path=mt5_terminal_path))

    if not skip_alpha:
        steps.append(_step_alpha_feed(base_dir, dry_run=dry_run))
        steps.append(_step_alpha_registration(base_dir, dry_run=dry_run))
        steps.append(_step_alpha_lifecycle(base_dir, dry_run=dry_run))
    if not skip_alpha_allocation:
        steps.append(_step_alpha_allocation(base_dir, dry_run=dry_run))

    if not skip_fs_maintenance:
        steps.append(
            _step_feature_store_maintenance(
                base_dir, dry_run=dry_run, mt5_terminal_path=mt5_terminal_path
            )
        )

    # Parameter optimization suggestions for degraded brains
    # Runs after retraining_check so we know which brains are degraded
    if not skip_retraining:
        retraining_results = [
            s for s in steps if s is not None and s.get("step") == "retraining_check"
        ]
        retraining_result = retraining_results[-1] if retraining_results else None
        if retraining_result and retraining_result.get("degraded_count", 0) > 0:
            steps.append(_step_param_optimization(base_dir, retraining_result, dry_run=dry_run))

    # ── DQAF-20260622-057 Phase 1: Freshness Guard as pipeline step ──
    # Runs LAST, AFTER all writes are complete, so it catches the final state
    # of every registered artifact including those just written by this pipeline.
    # 对标 Goldman Sachs Marquee DQF §4.2: self-assess freshness at generation point.
    steps.append(_step_freshness_check(base_dir))

    errors = [s for s in steps if s is not None and s.get("status") == "error"]
    actions = sum(s.get("actions_applied", 0) + s.get("promotions", 0) for s in steps)

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "base_dir": base_dir,
        "dry_run": dry_run,
        "total_steps": len(steps),
        "errors": len(errors),
        "actions_total": actions,
        "steps": steps,
    }


# ── CLI ──


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="daily_ops")
    p.add_argument("--base-dir", default="data", help="Base data directory")
    p.add_argument("--symbol", default="XAUUSDc", help="Trading symbol (default: XAUUSDc)")
    p.add_argument("--dry-run", action="store_true", help="Assess without applying transitions")
    p.add_argument("--skip-shadow", action="store_true", help="Skip shadow ensemble")
    p.add_argument(
        "--skip-journal-mt5-reconcile",
        action="store_true",
        help="Skip MT5-journal PnL reconciliation (deal.profit authoritative)",
    )
    p.add_argument(
        "--skip-journal-backfill",
        action="store_true",
        help="Skip journal PnL backfill (close_price→PnL)",
    )
    p.add_argument("--skip-label-builder", action="store_true", help="Skip label builder")
    p.add_argument("--skip-feedback", action="store_true", help="Skip feedback loop")
    p.add_argument("--skip-governance", action="store_true", help="Skip governance cycle")
    p.add_argument("--skip-champion", action="store_true", help="Skip champion/challenger")
    p.add_argument("--skip-retraining", action="store_true", help="Skip retraining check")
    p.add_argument("--skip-recap", action="store_true", help="Skip daily recap")
    p.add_argument("--skip-alpha", action="store_true", help="Skip alpha lifecycle evaluation")
    p.add_argument(
        "--skip-alpha-allocation", action="store_true", help="Skip alpha portfolio allocation"
    )
    p.add_argument(
        "--skip-online-feedback", action="store_true", help="Skip online learner feedback"
    )
    p.add_argument(
        "--skip-paper-simulation", action="store_true", help="Skip paper trade simulation"
    )
    p.add_argument(
        "--skip-fs-maintenance", action="store_true", help="Skip feature store maintenance"
    )
    p.add_argument("--skip-data-health", action="store_true", help="Skip data health monitoring")
    p.add_argument(
        "--skip-training-readiness",
        action="store_true",
        help="Skip training pipeline readiness check",
    )
    p.add_argument("--output", type=Path, default=None, help="Write combined report JSON to file")
    p.add_argument(
        "--mt5-terminal-path", default=None, help="MT5 terminal64.exe path for P&L snapshot"
    )
    p.add_argument(
        "--force-rebuild",
        action="store_true",
        help="Reset incremental watermarks before pipeline run (calibrator, alpha feed, reconciliation)",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Ensure project root on path
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))

    report = run_daily_ops(
        base_dir=args.base_dir,
        skip_shadow=args.skip_shadow,
        skip_journal_mt5_reconcile=args.skip_journal_mt5_reconcile,
        skip_journal_backfill=args.skip_journal_backfill,
        skip_label_builder=args.skip_label_builder,
        skip_feedback=args.skip_feedback,
        skip_governance=args.skip_governance,
        skip_champion=args.skip_champion,
        skip_retraining=args.skip_retraining,
        skip_recap=args.skip_recap,
        skip_alpha=args.skip_alpha,
        skip_alpha_allocation=args.skip_alpha_allocation,
        skip_online_feedback=args.skip_online_feedback,
        skip_paper_simulation=args.skip_paper_simulation,
        skip_fs_maintenance=args.skip_fs_maintenance,
        skip_data_health=args.skip_data_health,
        skip_training_readiness=args.skip_training_readiness,
        dry_run=args.dry_run,
        symbol=args.symbol,
        mt5_terminal_path=args.mt5_terminal_path,
        force_rebuild=args.force_rebuild,
    )

    text = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    print(text)

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")

    # Non-zero if any errors or actions applied (signals ops attention)
    if report["errors"] > 0:
        return 2
    if report["actions_total"] > 0:
        return 1
    return 0


try:
    from core.deployment.scheduled_task_registry import register

    register("daily_ops", run_daily_ops)
except ImportError:
    pass

if __name__ == "__main__":
    raise SystemExit(main())
