"""
Live Journal-Based Brain Metrics (FIX-20260621-032)
====================================================
Computes per-brain PnL metrics from the live trade journal (actual MT5 execution),
NOT from the shadow PnL ledger (which tracks paper signals with pnl_per_unit).

This replaces the leaderboard's dependence on BrainPnLStore.pnl_per_unit
with journal-based pnl_r (risk-normalized return).

Iron Law #11 compliant: all statistics are computed from structured data,
not from text-snippet reading.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any


def _classify_exit(label: str | None, detail: dict | None) -> str:
    """Classify exit reason for a closed trade."""
    if label in ("tp_hit_first", "tp_hit", "win"):
        return "tp_hit"
    if label in ("sl_hit_first", "sl_hit_trailed", "sl_hit", "loss"):
        return "sl_hit"
    if label and label.startswith("exit_watchdog:"):
        return "watchdog"
    if label == "breakeven":
        return "breakeven"
    reason = ""
    if isinstance(detail, dict):
        reason = detail.get("reason", "")
    if reason in ("tp_hit",):
        return "tp_hit"
    if reason in ("sl_hit",):
        return "sl_hit"
    if reason in ("mia_close", "mt5_deal_reason_3"):
        return "mia_close"
    if reason in ("unknown_close", "client_close", "position_not_found",
                  "auto_orphan_rejected", "auto_orphan_stale"):
        return "unknown_close"
    if label in ("win",):
        return "tp_hit"
    if label in ("loss",):
        return "sl_hit"
    return "other"


def compute_journal_brain_metrics(data_dir: str | Path) -> dict[str, dict[str, Any]]:
    """Compute per-brain performance metrics from live_trade_journal.jsonl.

    Returns a dict: brain_id -> {
        brain_id, win_rate, profit_factor, sharpe, sample_count,
        cumulative_pnl, max_drawdown, avg_win, avg_loss, long_trades,
        short_trades, long_wr, short_wr, exit_reasons, health_signal,
        recommendation
    }

    All PnL values are in R-units (risk-normalized), sourced from the
    close event's "pnl" field.
    """
    journal_path = Path(data_dir) / "live_trade_journal.jsonl"
    if not journal_path.exists():
        return {}

    records: list[dict] = []
    with open(journal_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    # Build trade map: ticket -> {open, close_pnl, close_label, detail}
    trades: dict[int, dict] = defaultdict(
        lambda: {"open": None, "close_pnl": None, "close_label": None, "detail": None}
    )

    for rec in records:
        ticket = rec.get("position_ticket")
        if ticket is None:
            continue
        action = rec.get("action", "")
        if action == "open":
            trades[ticket]["open"] = rec
        elif action == "close":
            pnl = rec.get("pnl")
            if pnl is not None:
                trades[ticket]["close_pnl"] = pnl
                trades[ticket]["close_label"] = rec.get("label")
                trades[ticket]["detail"] = rec.get("detail")

    # Accumulate per-brain
    brain_pnls: dict[str, list[float]] = defaultdict(list)
    brain_sides: dict[str, list[str]] = defaultdict(list)
    brain_exits: dict[str, defaultdict[str, int]] = defaultdict(
        lambda: defaultdict(int)
    )

    for ticket, td in trades.items():
        open_rec = td["open"]
        pnl = td["close_pnl"]
        if open_rec is None or pnl is None:
            continue
        brain_ids = open_rec.get("brain_ids") or ["unknown"]
        side = open_rec.get("side", "unknown")
        exit_cat = _classify_exit(td["close_label"], td["detail"])

        for bid in brain_ids:
            brain_pnls[bid].append(pnl)
            brain_sides[bid].append(side)
            brain_exits[bid][exit_cat] += 1

    # Compute metrics per brain
    result: dict[str, dict[str, Any]] = {}
    for bid, pnls in brain_pnls.items():
        n = len(pnls)
        if n == 0:
            continue

        wins = sum(1 for p in pnls if p > 0)
        losses = sum(1 for p in pnls if p < 0)
        bes = sum(1 for p in pnls if p == 0)
        wr = wins / max(n - bes, 1)

        total_pnl = sum(pnls)
        avg_win = sum(p for p in pnls if p > 0) / max(wins, 1)
        avg_loss = sum(p for p in pnls if p < 0) / max(losses, 1)

        total_win_pnl = sum(p for p in pnls if p > 0)
        total_loss_pnl = abs(sum(p for p in pnls if p < 0))
        pf = total_win_pnl / max(total_loss_pnl, 0.01)

        # Max drawdown
        cum = 0.0
        peak = 0.0
        max_dd = 0.0
        for p in pnls:
            cum += p
            if cum > peak:
                peak = cum
            dd = peak - cum
            if dd > max_dd:
                max_dd = dd

        # Sharpe (approx)
        if n >= 2:
            mean_pnl = total_pnl / n
            variance = sum((x - mean_pnl) ** 2 for x in pnls) / (n - 1)
            std = math.sqrt(variance) if variance > 0 else 0.0
            sharpe = (mean_pnl / std) * math.sqrt(n) if std > 0 else 0.0
        else:
            sharpe = 0.0

        # Direction breakdown
        sides = brain_sides.get(bid, [])
        long_pnls = [pnls[i] for i, s in enumerate(sides) if s == "long" and i < len(pnls)]
        short_pnls = [pnls[i] for i, s in enumerate(sides) if s == "short" and i < len(pnls)]
        l_wr = sum(1 for p in long_pnls if p > 0) / max(len(long_pnls), 1)
        s_wr = sum(1 for p in short_pnls if p > 0) / max(len(short_pnls), 1)

        # Health signal
        if n < 10:
            health = "insufficient_data"
        elif sharpe < -1.0:
            health = "critical"
        elif sharpe < 0.0:
            health = "degraded"
        elif sharpe < 0.5:
            health = "warning"
        elif sharpe >= 1.0 and wr >= 0.40:
            health = "healthy"
        else:
            health = "stable"

        # Recommendation
        if health == "insufficient_data":
            rec = "observe"
        elif health == "critical":
            rec = "retire" if n >= 20 else "freeze"
        elif health == "degraded":
            rec = "demote_to_probation"
        elif health == "warning":
            rec = "limit_exposure"
        elif health == "healthy" and n >= 50:
            rec = "promote_to_live"
        else:
            rec = "maintain"

        result[bid] = {
            "brain_id": bid,
            "win_rate": wr,
            "profit_factor": pf,
            "sharpe_ratio": sharpe,       # BrainLeaderboard.rank() expects 'sharpe_ratio'
            "sample_count": n,             # BrainLeaderboard expects 'sample_count'
            "cumulative_pnl": total_pnl,   # BrainLeaderboard expects 'cumulative_pnl'
            "max_drawdown": max_dd,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "long_count": len(long_pnls),  # BrainLeaderboard expects 'long_count'
            "short_count": len(short_pnls),
            "long_win_rate": l_wr,          # BrainLeaderboard expects 'long_win_rate'
            "short_win_rate": s_wr,         # BrainLeaderboard expects 'short_win_rate'
            "total_spread_cost": 0.0,
            "total_slippage_cost": 0.0,
            "exit_reasons": dict(brain_exits.get(bid, {})),
            "health_signal": health,
            "recommendation": rec,
        }

    return result
