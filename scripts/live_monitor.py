"""Unified live monitoring sidecar for the Quant OS runtime stack.

Aggregates health signals from circuit breaker, trade journal, bridge,
positions, brain activity, and shadow alignment into a single JSON-line
snapshot every N seconds.

Usage:
  python scripts/live_monitor.py --base-dir data --symbol XAUUSDc
  python scripts/live_monitor.py --base-dir data --symbol XAUUSDc --once
  python scripts/live_monitor.py --base-dir data --symbol XAUUSDc --interval 10 --mt5-terminal-path "D:\\MT5\\terminal64.exe"
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.runtime.fault_handler import fail_open_guard

SCHEMA_VERSION = "live_monitor.v1"


def _utc_now_iso() -> str:
    return (
        datetime.now(UTC)
        .replace(tzinfo=None)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


# ── Component collectors ──


def _check_circuit_breaker(flag_path: Path) -> dict[str, Any]:
    """Read live_dispatch_block.flag and extract status."""
    result: dict[str, Any] = {
        "blocked": False,
        "flag_exists": flag_path.exists(),
        "reasons": [],
        "sources": {},
        "flag_age_seconds": None,
    }
    if not flag_path.exists():
        return result

    try:
        raw = json.loads(flag_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        result["blocked"] = True  # flag present but unreadable = blocked
        return result

    result["blocked"] = bool(raw.get("blocked", True))
    result["reasons"] = raw.get("reasons", [])
    result["sources"] = raw.get("sources", {})

    try:
        age = time.time() - flag_path.stat().st_mtime
        result["flag_age_seconds"] = round(age, 1)
    except OSError:
        pass

    return result


def _check_trade_quality(journal_path: Path, *, lookback_hours: float = 4.0) -> dict[str, Any]:
    """Parse recent journal entries for trade quality stats."""
    result: dict[str, Any] = {
        "recent_total": 0,
        "accepted": 0,
        "rejected": 0,
        "acknowledged": 0,
        "other": 0,
        "rejection_rate": 0.0,
        "tail_consecutive_rejected": 0,
        "last_event_at": None,
    }
    if not journal_path.exists():
        return result

    cutoff = time.time() - lookback_hours * 3600
    entries: list[dict[str, Any]] = []

    try:
        for line in journal_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            recorded = rec.get("recorded_at", "")
            try:
                dt = datetime.fromisoformat(recorded.replace("Z", "+00:00"))
                if dt.timestamp() >= cutoff:
                    entries.append(rec)
            except (ValueError, OSError):
                continue
    except OSError:
        return result

    if not entries:
        return result

    result["recent_total"] = len(entries)
    result["last_event_at"] = entries[-1].get("recorded_at", "")

    ack_counts = {"accepted": 0, "rejected": 0, "acknowledged": 0, "other": 0}
    for e in entries:
        s = e.get("ack_status", "other")
        ack_counts[s] = ack_counts.get(s, 0) + 1
    result.update(ack_counts)
    result["other"] = ack_counts.get("other", 0)

    if result["recent_total"] > 0:
        result["rejection_rate"] = round(result["rejected"] / result["recent_total"], 4)

    # Count tail consecutive rejected
    tail = 0
    for e in reversed(entries):
        if e.get("ack_status") == "rejected":
            tail += 1
        else:
            break
    result["tail_consecutive_rejected"] = tail

    return result


def _check_bridge(outbox_dir: Path, receipt_dir: Path, bridge_log_path: Path) -> dict[str, Any]:
    """Check bridge health: outbox pending, receipts, bridge log freshness."""
    result: dict[str, Any] = {
        "outbox_pending": 0,
        "outbox_stale_count": 0,
        "receipt_total": 0,
        "receipt_accepted": 0,
        "receipt_rejected": 0,
        "bridge_alive": False,
        "bridge_log_age_seconds": None,
    }

    # Outbox
    if outbox_dir.exists():
        mt5_files = list(outbox_dir.rglob("*.mt5.json"))
        result["outbox_pending"] = len(mt5_files)

        # Staleness (>10 min)
        cutoff = time.time() - 600
        stale = 0
        for p in mt5_files:
            try:
                if p.stat().st_mtime < cutoff:
                    stale += 1
            except OSError:
                stale += 1
        result["outbox_stale_count"] = stale

    # Receipts
    if receipt_dir.exists():
        ack_files = list(receipt_dir.rglob("*.ack.json"))
        result["receipt_total"] = len(ack_files)
        acc = 0
        rej = 0
        for p in ack_files[-50:]:  # sample last 50
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                status = data.get("status", "")
                if status == "accepted":
                    acc += 1
                elif status == "rejected":
                    rej += 1
            except (json.JSONDecodeError, OSError):
                pass
        result["receipt_accepted"] = acc
        result["receipt_rejected"] = rej

    # Bridge log freshness
    if bridge_log_path.exists():
        try:
            age = time.time() - bridge_log_path.stat().st_mtime
            result["bridge_log_age_seconds"] = round(age, 1)
            result["bridge_alive"] = age < 300  # active within last 5 min
        except OSError:
            pass

    return result


def _check_positions(mt5_terminal_path: str | None, symbol: str) -> dict[str, Any]:
    """Get MT5 position snapshot if terminal is available."""
    result: dict[str, Any] = {
        "available": False,
        "count": 0,
        "total_pnl": 0.0,
        "symbol_positions": 0,
    }
    if not mt5_terminal_path or not Path(mt5_terminal_path).exists():
        return result

    try:
        import MetaTrader5 as mt5

        if not mt5.initialize(path=mt5_terminal_path):
            return result

        positions = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
        if positions is None:
            positions = []
        result["count"] = len(positions)
        result["symbol_positions"] = len(positions)
        total_pnl = sum(float(p.profit) for p in positions)
        result["total_pnl"] = round(total_pnl, 2)
        result["available"] = True
        mt5.shutdown()
    except Exception:  # BLE001:FOG
        with fail_open_guard("live_monitor:_check_positions"):
            pass
    return result


def _check_brains(decisions_dir: Path) -> dict[str, Any]:
    """Check recent brain activity from decisions ledger."""
    result: dict[str, Any] = {
        "active_brains": [],
        "recent_decision_count": 0,
        "last_decision_at": None,
    }
    if not decisions_dir.exists():
        return result

    today = datetime.now(UTC).replace(tzinfo=None).date().isoformat()
    today_dir = decisions_dir / today
    if not today_dir.exists():
        # Check available dates, use most recent
        date_dirs = sorted([d for d in decisions_dir.iterdir() if d.is_dir()], key=lambda x: x.name)
        if not date_dirs:
            return result
        today_dir = date_dirs[-1]

    decision_file = today_dir / "XAUUSD.decisions.jsonl"
    if not decision_file.exists():
        return result

    brain_ids: set[str] = set()
    count = 0
    try:
        for line in decision_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            count += 1
            brains = rec.get("attribution", {}).get("supporting_brains", [])
            for b in brains:
                brain_ids.add(b)
            opp = rec.get("attribution", {}).get("opposing_brains", [])
            for b in opp:
                brain_ids.add(b)
    except OSError:
        pass

    result["active_brains"] = sorted(brain_ids)
    result["recent_decision_count"] = count
    return result


def _check_shadow_alignment(shadow_outbox_dir: Path, live_outbox_dir: Path) -> dict[str, Any]:
    """Compare shadow vs live outbox activity."""
    result: dict[str, Any] = {
        "alignment": "no_data",
        "shadow_intent_count": 0,
        "live_intent_count": 0,
    }

    if shadow_outbox_dir.exists():
        shadow_files = list(shadow_outbox_dir.rglob("*.mt5.json"))
        result["shadow_intent_count"] = len(shadow_files)

    if live_outbox_dir.exists():
        live_files = list(live_outbox_dir.rglob("*.mt5.json"))
        result["live_intent_count"] = len(live_files)

    if result["shadow_intent_count"] == 0 and result["live_intent_count"] == 0:
        result["alignment"] = "both_silent"
    elif result["shadow_intent_count"] > 0 and result["live_intent_count"] > 0:
        result["alignment"] = "both_active"
    elif result["shadow_intent_count"] > 0:
        result["alignment"] = "shadow_active_live_silent"
    elif result["live_intent_count"] > 0:
        result["alignment"] = "live_active_shadow_silent"

    return result


# ── Alert derivation ──


def _derive_alerts(
    breaker: dict[str, Any],
    trade: dict[str, Any],
    bridge: dict[str, Any],
    positions: dict[str, Any],
    brains: dict[str, Any],
    shadow: dict[str, Any],
) -> tuple[str, list[dict[str, Any]]]:
    """Derive overall alert level and component-level alerts."""
    alerts: list[dict[str, Any]] = []

    def alert(component: str, level: str, message: str) -> None:
        alerts.append({"component": component, "level": level, "message": message})

    # Circuit breaker
    if breaker.get("blocked"):
        age = breaker.get("flag_age_seconds", 0)
        mins = int(age / 60) if age else 0
        alert("circuit_breaker", "CRITICAL", f"Protection flag active ({mins}m)")

    # Trade quality
    rejection_rate = trade.get("rejection_rate", 0.0)
    if rejection_rate >= 0.5 and trade.get("recent_total", 0) >= 3:
        alert("trade_quality", "CRITICAL", f"Rejection rate {rejection_rate:.0%}")
    elif rejection_rate >= 0.2 and trade.get("recent_total", 0) >= 5:
        alert("trade_quality", "WARNING", f"Rejection rate {rejection_rate:.0%}")

    if trade.get("tail_consecutive_rejected", 0) >= 3:
        alert(
            "trade_quality", "WARNING", f"{trade['tail_consecutive_rejected']} consecutive rejects"
        )

    # Bridge
    if bridge.get("outbox_stale_count", 0) > 5:
        alert("bridge", "CRITICAL", f"{bridge['outbox_stale_count']} stale outbox files")
    elif bridge.get("outbox_stale_count", 0) > 0:
        alert("bridge", "WARNING", f"{bridge['outbox_stale_count']} stale outbox files")

    if not bridge.get("bridge_alive") and bridge.get("bridge_log_age_seconds") is not None:
        alert("bridge", "WARNING", "Bridge log stale")
    elif bridge.get("bridge_log_age_seconds") is None:
        alert("bridge", "WARNING", "No bridge supervisor log found")

    # Positions
    if positions.get("available") and positions.get("count", 0) > 10:
        alert("positions", "WARNING", f"{positions['count']} open positions")

    # Brains
    if brains.get("active_brains"):
        if len(brains["active_brains"]) < 2:
            alert("brains", "WARNING", f"Only {len(brains['active_brains'])} active brains")
    else:
        alert("brains", "WARNING", "No recent brain activity")

    # Shadow alignment
    alignment = shadow.get("alignment", "no_data")
    if alignment == "shadow_active_live_silent":
        alert("shadow", "WARNING", "Shadow active but live silent")
    elif alignment == "live_active_shadow_silent":
        alert("shadow", "WARNING", "Live active but shadow silent")

    # Overall level
    levels = [a["level"] for a in alerts]
    if "CRITICAL" in levels:
        overall = "CRITICAL"
    elif "WARNING" in levels:
        overall = "WARNING"
    else:
        overall = "OK"

    return overall, alerts


# ── Snapshot builder ──


def build_snapshot(
    base_dir: Path,
    symbol: str,
    *,
    mt5_terminal_path: str | None = None,
    lookback_hours: float = 4.0,
) -> dict[str, Any]:
    """Collect all monitoring signals and produce a snapshot."""
    flag_path = base_dir / "live_dispatch_block.flag"
    journal_path = base_dir / "live_trade_journal.jsonl"
    outbox_dir = base_dir / "mt5_outbox"
    receipt_dir = base_dir / "receipts"
    bridge_log_path = base_dir / "reports" / "ops_logs" / "bridge_supervisor.log"
    decisions_dir = base_dir / "decisions"
    shadow_outbox_dir = base_dir / "mt5_shadow_outbox"

    breaker = _check_circuit_breaker(flag_path)
    trade = _check_trade_quality(journal_path, lookback_hours=lookback_hours)
    bridge = _check_bridge(outbox_dir, receipt_dir, bridge_log_path)
    positions = _check_positions(mt5_terminal_path, symbol)
    brains = _check_brains(decisions_dir)
    shadow = _check_shadow_alignment(shadow_outbox_dir, outbox_dir)

    overall, alerts = _derive_alerts(breaker, trade, bridge, positions, brains, shadow)

    return {
        "event": "monitor_snapshot",
        "schema_version": SCHEMA_VERSION,
        "time": _utc_now_iso(),
        "symbol": symbol,
        "alert_level": overall,
        "alerts": alerts,
        "components": {
            "circuit_breaker": breaker,
            "trade_quality": trade,
            "bridge": bridge,
            "positions": positions,
            "brains": brains,
            "shadow_alignment": shadow,
        },
    }


# ── CLI ──


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="live_monitor")
    p.add_argument("--base-dir", default="data", help="Base directory for runtime data")
    p.add_argument("--symbol", default="XAUUSDc", help="Trading symbol")
    p.add_argument(
        "--mt5-terminal-path", default=None, help="MT5 terminal64.exe path for position snapshots"
    )
    p.add_argument(
        "--interval", type=float, default=30.0, help="Seconds between snapshots (default: 30)"
    )
    p.add_argument(
        "--lookback-hours",
        type=float,
        default=4.0,
        help="Hours of journal history to scan (default: 4)",
    )
    p.add_argument("--once", action="store_true", help="Run a single snapshot and exit")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    base_dir = Path(args.base_dir)

    while True:
        try:
            snapshot = build_snapshot(
                base_dir=base_dir,
                symbol=args.symbol,
                mt5_terminal_path=args.mt5_terminal_path,
                lookback_hours=args.lookback_hours,
            )
            print(json.dumps(snapshot, ensure_ascii=False, default=str), flush=True)
        except Exception as exc:  # BLE001:FOG
            with fail_open_guard("live_monitor:main"):
                error_event = {
                    "event": "monitor_error",
                    "time": _utc_now_iso(),
                    "error": str(exc)[:500],
                }
                print(json.dumps(error_event, ensure_ascii=False), flush=True)
        if args.once:
            break

        time.sleep(args.interval)

    return 0


try:
    from core.deployment.scheduled_task_registry import register

    register("live_monitor_snapshot", build_snapshot)
except ImportError:
    pass

if __name__ == "__main__":
    raise SystemExit(main())
