"""Online-offline evaluation alignment: compare live P&L labels against backtest results.

Aligns metrics from label_builder (live trade P&L from journal) with backtest
result.json (offline evaluation from trainers). Flags discrepancies where live
performance deviates significantly from expected backtest baselines.

Usage:
  python scripts/training/eval_alignment.py --labels data/labels/training_labels.jsonl --backtest result.json
  python scripts/training/eval_alignment.py --labels data/labels/training_labels.jsonl --backtest result.json --output data/reports/alignment.json
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "eval_alignment.v1"

# Thresholds for flagging alignment issues
WIN_RATE_DELTA_WARN = 0.10  # warn if live win rate differs from backtest by >10pp
WIN_RATE_DELTA_CRITICAL = 0.20
MIN_TRADES_FOR_COMPARISON = 3  # need at least 3 live trades to compare


def _utc_now_iso() -> str:
    return (
        datetime.now(UTC)
        .replace(tzinfo=None)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def load_labels(labels_path: Path) -> list[dict[str, Any]]:
    """Load live P&L labels JSONL (output of label_builder.py)."""
    if not labels_path.exists():
        return []
    labels: list[dict[str, Any]] = []
    for line in labels_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("schema_version") == "training_label.v1":
            labels.append(rec)
    return labels


def load_backtest_result(result_path: Path) -> dict[str, Any]:
    """Load backtest result.json from any CRT trainer."""
    if not result_path.exists():
        return {}
    return json.loads(result_path.read_text(encoding="utf-8"))


def compute_label_metrics(labels: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute live performance metrics from P&L labels."""
    closed = [r for r in labels if r.get("is_closed") and r.get("pnl") is not None]
    if not closed:
        return {
            "total_labels": len(labels),
            "closed_trades": 0,
            "win_rate": None,
            "avg_pnl": None,
            "total_pnl": 0.0,
            "profit_factor": None,
            "direction_bias": None,
            "error": "no_closed_trades",
        }

    wins = [r for r in closed if r["label"] == "win"]
    losses = [r for r in closed if r["label"] == "loss"]
    pnls = [r["pnl"] for r in closed]
    total_pnl = round(sum(pnls), 6)
    win_rate = round(len(wins) / len(closed), 4) if closed else None
    avg_pnl = round(sum(pnls) / len(pnls), 6) if pnls else None

    gross_profit = sum(r["pnl"] for r in wins) if wins else 0.0
    gross_loss = abs(sum(r["pnl"] for r in losses)) if losses else 1e-8
    profit_factor = round(gross_profit / gross_loss, 4) if gross_loss > 0 else None

    long_trades = sum(1 for r in closed if r.get("side") == "long")
    direction_bias = round(long_trades / len(closed), 4) if closed else None

    return {
        "total_labels": len(labels),
        "closed_trades": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": sum(1 for r in closed if r["label"] == "breakeven"),
        "win_rate": win_rate,
        "avg_pnl": avg_pnl,
        "total_pnl": total_pnl,
        "profit_factor": profit_factor,
        "direction_bias": direction_bias,
        "pnl_range": {
            "min": round(min(pnls), 6),
            "max": round(max(pnls), 6),
        },
    }


def extract_backtest_metrics(backtest: dict[str, Any]) -> dict[str, Any]:
    """Extract comparable metrics from backtest result.json.

    Handles arb_trainer metrics (sharpe, winrate_pct, etc.) and generic CRT format.
    """
    metrics = backtest.get("metrics", {})
    if not metrics:
        return {"source": "backtest", "available": False, "error": "no_metrics_section"}

    # Try arb-style metrics
    win_rate = None
    if "winrate_pct" in metrics:
        win_rate = round(metrics["winrate_pct"] / 100, 4)
    elif "backtest_metrics" in metrics:
        bm = metrics["backtest_metrics"]
        if "winrate" in bm:
            win_rate = round(bm["winrate"], 4)

    total_pnl = metrics.get("total_pnl")
    sharpe = metrics.get("sharpe")
    profit_factor = metrics.get("profit_factor")

    # Try backtest_metrics sub-dict (arb_trainer style)
    if "backtest_metrics" in metrics:
        bm = metrics["backtest_metrics"]
        total_pnl = total_pnl or bm.get("total_pnl")
        sharpe = sharpe or bm.get("sharpe")
        profit_factor = profit_factor or bm.get("profit_factor")

    return {
        "source": "backtest",
        "available": True,
        "model_id": backtest.get("model_id", ""),
        "lane": backtest.get("lane", ""),
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "sharpe": sharpe,
        "profit_factor": profit_factor,
    }


def compare_metrics(
    live_metrics: dict[str, Any],
    backtest_metrics: dict[str, Any],
) -> dict[str, Any]:
    """Compare live vs backtest metrics and flag alignment issues."""
    issues: list[str] = []
    comparisons: list[dict[str, Any]] = []

    live_wr = live_metrics.get("win_rate")
    bt_wr = backtest_metrics.get("win_rate")

    # Win rate alignment
    if live_wr is not None and bt_wr is not None:
        delta = abs(live_wr - bt_wr)
        severity = (
            "critical"
            if delta >= WIN_RATE_DELTA_CRITICAL
            else "warning"
            if delta >= WIN_RATE_DELTA_WARN
            else "ok"
        )
        comparisons.append(
            {
                "metric": "win_rate",
                "live": live_wr,
                "backtest": bt_wr,
                "delta": round(delta, 4),
                "severity": severity,
            }
        )
        if severity == "critical":
            issues.append(f"win_rate_divergence_critical={delta:.1%}")
        elif severity == "warning":
            issues.append(f"win_rate_divergence_warning={delta:.1%}")

    # Total P&L alignment (direction only — magnitudes differ by volume)
    live_total = live_metrics.get("total_pnl")
    bt_total = backtest_metrics.get("total_pnl")
    if live_total is not None and bt_total is not None:
        same_sign = (live_total >= 0 and bt_total >= 0) or (live_total <= 0 and bt_total <= 0)
        comparisons.append(
            {
                "metric": "total_pnl_sign",
                "live": round(live_total, 6),
                "backtest": round(bt_total, 6),
                "same_sign": same_sign,
                "severity": "ok" if same_sign else "warning",
            }
        )
        if not same_sign:
            issues.append("pnl_sign_mismatch")

    # Volume / trade count comparison
    live_trades = live_metrics.get("closed_trades", 0)
    comparisons.append(
        {
            "metric": "trade_count",
            "live": live_trades,
            "backtest": backtest_metrics.get("total_trades", "n/a"),
            "severity": "ok" if live_trades >= MIN_TRADES_FOR_COMPARISON else "warning",
            "note": "insufficient_live_sample" if live_trades < MIN_TRADES_FOR_COMPARISON else None,
        }
    )
    if live_trades < MIN_TRADES_FOR_COMPARISON:
        issues.append(f"insufficient_live_trades={live_trades}")

    severity = (
        "critical"
        if any(c["severity"] == "critical" for c in comparisons)
        else "warning"
        if issues
        else "ok"
    )

    # Insufficient live sample caps severity at warning
    if live_trades < MIN_TRADES_FOR_COMPARISON:
        severity = "warning"

    return {
        "severity": severity,
        "issues": issues,
        "comparisons": comparisons,
    }


def build_report(
    labels_path: Path,
    backtest_path: Path,
) -> dict[str, Any]:
    """Build alignment report between live labels and backtest results."""
    labels = load_labels(labels_path)
    backtest = load_backtest_result(backtest_path)

    if not labels:
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _utc_now_iso(),
            "labels_path": str(labels_path),
            "backtest_path": str(backtest_path),
            "error": "no_labels_found",
        }
    if not backtest:
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _utc_now_iso(),
            "labels_path": str(labels_path),
            "backtest_path": str(backtest_path),
            "error": "no_backtest_found",
        }

    live_metrics = compute_label_metrics(labels)
    bt_metrics = extract_backtest_metrics(backtest)
    alignment = compare_metrics(live_metrics, bt_metrics)

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "labels_path": str(labels_path),
        "backtest_path": str(backtest_path),
        "live_metrics": live_metrics,
        "backtest_metrics": bt_metrics,
        "alignment": alignment,
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="eval_alignment")
    p.add_argument(
        "--labels",
        type=Path,
        required=True,
        help="Path to training_labels.jsonl (label_builder output)",
    )
    p.add_argument(
        "--backtest",
        type=Path,
        required=True,
        help="Path to backtest result.json (trainer output)",
    )
    p.add_argument(
        "--output",
        default=None,
        help="Write alignment report JSON to file",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    report = build_report(Path(args.labels), Path(args.backtest))
    text = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    print(text)

    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
        print(f"[eval_alignment] Report written to {out}")

    if report.get("error"):
        return 2
    if report.get("alignment", {}).get("severity") == "critical":
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
