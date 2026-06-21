#!/usr/bin/env python3
"""
purge_backtest_from_governance.py — DQAF-20260621-042 P1-B
============================================================
Detects and purges backtest-contaminated performance metrics from
governance_state.json, replacing them with journal-derived (live
execution) values.

Per Iron Law IMMUTABLE_LEDGER_AND_EPHEMERAL_PROJECTION:
  governance_state.json is an ephemeral projection computed from
  the journal SSOT.  If backtest data contaminated it, the fix is
  to regenerate from the journal — never to manually edit the JSON.

Detection logic:
  A brain's performance_metrics are flagged as BACKTEST_CONTAMINATED
  when total_trades exceeds the physically possible maximum for the
  live trading period.  BTC live trading started 2026-05-31, giving
  ~22 days × ~10 trades/day = ~220 max.

Usage:
  python scripts/purge_backtest_from_governance.py --base-dir data_btc [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# ── Configuration ──
LIVE_START_DATE: dict[str, str] = {
    "BTC": "2026-05-31",
    "XAU": "2026-05-04",
}
MAX_TRADES_PER_DAY: int = 15  # generous upper bound
BACKTEST_MARKER: str = "BACKTEST_CONTAMINATED_PURGED_DQAF-20260621-042"


def _max_possible_trades(symbol: str) -> int:
    """Compute upper bound on possible live trades for a symbol."""
    start_str = LIVE_START_DATE.get(symbol, "2026-05-01")
    start = datetime.strptime(start_str, "%Y-%m-%d").replace(tzinfo=UTC)
    now = datetime.now(UTC)
    days = max((now - start).days, 1)
    return days * MAX_TRADES_PER_DAY


def _is_contaminated(metrics: dict[str, Any], symbol: str) -> bool:
    """Check if performance_metrics are physically impossible for live trading."""
    total_trades = metrics.get("total_trades", 0)
    pnl_r = metrics.get("pnl_r", 0)
    sharpe = metrics.get("sharpe_ratio", 0)

    max_possible = _max_possible_trades(symbol)

    # Flag 1: Impossibly high trade count
    if total_trades > max_possible:
        return True

    # Flag 2: Impossibly large negative PnL_R (training loss, not live)
    #   Live BTC total PnL is ~+110R.  -10000R is clearly backtest.
    if pnl_r < -500 and total_trades > 100:
        return True

    # Flag 3: Sharpe ratio impossible for live (< -5 is training artifact)
    if sharpe < -5.0 and total_trades > 50:
        return True

    return False


def _journal_metrics_for_brain(
    brain_id: str, journal_metrics: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    """Get journal-derived metrics for a brain."""
    return journal_metrics.get(brain_id)


def purge(
    base_dir: str | Path,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Purge backtest contamination from governance_state.json.

    Returns a report of what was changed.
    """
    base = Path(base_dir)
    gov_path = base / "governance_state.json"

    if not gov_path.exists():
        print(f"ERROR: {gov_path} not found")
        sys.exit(1)

    # Load governance state
    with open(gov_path, encoding="utf-8") as f:
        gov = json.load(f)

    # Load journal metrics (SSOT for live performance)
    from core.feedback.live_journal_metrics import compute_journal_brain_metrics

    journal_metrics = compute_journal_brain_metrics(base)
    print(f"Journal metrics: {len(journal_metrics)} brains")

    # Determine symbol from base_dir
    symbol = "XAU" if "data_btc" not in str(base) else "BTC"
    if str(base).endswith("data_btc") or "data_btc" in str(base):
        symbol = "BTC"
    elif str(base).endswith("data") or "/data" in str(base):
        symbol = "XAU"
    else:
        symbol = "BTC"  # default

    max_possible = _max_possible_trades(symbol)
    print(f"Symbol: {symbol}, max possible live trades: {max_possible}")
    print(f"Live started: {LIVE_START_DATE.get(symbol, 'unknown')}")
    print()

    brain_states = gov.get("brain_states", {})
    report: dict[str, Any] = {
        "base_dir": str(base),
        "symbol": symbol,
        "max_possible_trades": max_possible,
        "brains_scanned": len(brain_states),
        "brains_purged": 0,
        "purge_details": [],
        "dry_run": dry_run,
    }

    for brain_id, state in brain_states.items():
        metrics = state.get("performance_metrics", {})
        if not metrics:
            continue

        if not _is_contaminated(metrics, symbol):
            continue

        jm = _journal_metrics_for_brain(brain_id, journal_metrics)
        journal_trades = jm.get("trade_count", 0) if jm else 0
        journal_pnl = jm.get("pnl_r", 0.0) if jm else 0.0
        journal_wr = jm.get("win_rate", 0.0) if jm else 0.0
        journal_sharpe = jm.get("sharpe_ratio", 0.0) if jm else 0.0

        old_trades = metrics.get("total_trades", 0)
        old_pnl = metrics.get("pnl_r", 0)
        old_sharpe = metrics.get("sharpe_ratio", 0)

        detail = {
            "brain_id": brain_id,
            "old_total_trades": old_trades,
            "old_pnl_r": old_pnl,
            "old_sharpe_ratio": old_sharpe,
            "new_total_trades": journal_trades,
            "new_pnl_r": round(journal_pnl, 4),
            "new_sharpe_ratio": round(journal_sharpe, 4),
            "new_win_rate": round(journal_wr, 4),
            "source": "journal" if jm else "zeroed",
        }

        if not dry_run:
            if jm:
                # Replace with journal-derived live metrics
                state["performance_metrics"] = {
                    "win_rate": round(journal_wr, 4),
                    "profit_factor": round(jm.get("profit_factor", 0), 4),
                    "sharpe_ratio": round(journal_sharpe, 4),
                    "total_trades": journal_trades,
                    "pnl_r": round(journal_pnl, 4),
                }
            else:
                # No journal data — zero out (brain never traded live)
                state["performance_metrics"] = {
                    "win_rate": 0.0,
                    "profit_factor": 0.0,
                    "sharpe_ratio": 0.0,
                    "total_trades": 0,
                    "pnl_r": 0.0,
                }
            state["performance_metrics"]["_data_source"] = BACKTEST_MARKER

        report["brains_purged"] += 1
        report["purge_details"].append(detail)
        print(
            f"  PURGED {brain_id}: "
            f"{old_trades} trades → {journal_trades}, "
            f"{old_pnl}R → {journal_pnl:.1f}R, "
            f"Sharpe {old_sharpe} → {journal_sharpe:.2f}"
        )

    if not dry_run and report["brains_purged"] > 0:
        # Atomic write via temp file
        import os
        import tempfile

        gov_json = json.dumps(gov, indent=2, ensure_ascii=False, default=str)
        fd, tmp_path = tempfile.mkstemp(
            suffix=".json", prefix="governance_purged_", dir=str(base)
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(gov_json)
            os.replace(tmp_path, str(gov_path))
            print(f"\nWrote corrected governance_state.json ({len(gov_json)} bytes)")
        except Exception:
            os.unlink(tmp_path)
            raise

    print(f"\nBrains scanned: {report['brains_scanned']}")
    print(f"Brains purged: {report['brains_purged']}")
    if dry_run:
        print("DRY RUN — no changes written.")

    return report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Purge backtest-contaminated performance metrics from governance_state.json"
    )
    parser.add_argument(
        "--base-dir",
        default="data_btc",
        help="Base directory for the trading asset (default: data_btc)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Detect contamination without writing changes",
    )
    args = parser.parse_args()

    report = purge(args.base_dir, dry_run=args.dry_run)

    # Write report
    report_path = Path(args.base_dir) / "reports" / "backtest_purge_report.json"
    if not args.dry_run:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        print(f"Report written to {report_path}")


if __name__ == "__main__":
    main()
