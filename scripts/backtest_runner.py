"""Run a complete backtest with attribution analysis.

Usage:
    python scripts/backtest_runner.py                          # default: all 3 rules, 12mo data
    python scripts/backtest_runner.py --strategies statarb     # single strategy
    python scripts/backtest_runner.py --days 180               # 6-month window
    python scripts/backtest_runner.py --output results.json    # save to file
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.backtest.data_feed import DataFeed
from core.backtest.engine import BacktestEngine, BacktestResult
from core.backtest.metrics import compute_backtest_metrics
from core.backtest.strategy_adapter import rule_based_strategies
from core.runtime.fault_handler import fail_open_guard

# ── Attribution (using core modules) ────────────────────────────────────────


def run_attribution(result: BacktestResult) -> dict[str, Any]:
    """Run factor attribution and Brinson on backtest trades."""
    trades = result.trades
    if not trades:
        return {"error": "no_trades", "message": "No trades to attribute"}

    # Build a price proxy from equity curve for factor returns
    equity = np.array([p["equity"] for p in result.equity_curve], dtype=np.float64)
    if len(equity) < 3:
        return {"error": "insufficient_equity", "message": f"Only {len(equity)} equity points"}

    # Use equity as price proxy for factor decomposition
    direction_signs = np.sign(np.diff(equity))
    direction_signs = np.concatenate([[1], direction_signs])

    try:
        from core.metrics.factor_attribution import build_factor_returns, decompose_pnl

        factor_returns = build_factor_returns(
            prices=equity,
            direction_signs=direction_signs,
        )
        # Compute bar-level returns from equity
        bar_returns = np.zeros(len(equity))
        bar_returns[1:] = np.diff(equity) / np.maximum(equity[:-1], 1.0)
        attr = decompose_pnl(bar_returns, factor_returns)
        factor_d = attr.to_dict()
    except Exception as e:  # BLE001:FOG
        with fail_open_guard("backtest_runner:run_attribution"):
            factor_d = {"error": str(e)}
    # Run Brinson on strategy-level decomposition
    try:
        from core.metrics.brinson_attribution import brinson_decompose

        # Group by magic to separate strategy lines
        strategy_pnl: dict[str, float] = {}
        for t in trades:
            magic = t.get("magic", 0)
            from core.contracts.strategy_magic import MAGIC_TO_STRATEGY

            strat_name = MAGIC_TO_STRATEGY.get(magic, f"magic_{magic}")
            pnl = t.get("pnl", 0.0)
            strategy_pnl[strat_name] = strategy_pnl.get(strat_name, 0.0) + pnl

        if len(strategy_pnl) >= 2:
            sectors = list(strategy_pnl.keys())
            total_pnl = sum(abs(v) for v in strategy_pnl.values()) or 1.0
            port_w = [abs(strategy_pnl[s]) / total_pnl for s in sectors]
            bench_w = [1.0 / len(sectors)] * len(sectors)
            port_r = [strategy_pnl[s] / max(abs(strategy_pnl[s]), 1) * 0.01 for s in sectors]
            bench_r = [0.0] * len(sectors)

            brinson = brinson_decompose(
                sectors=sectors,
                port_weights=port_w,
                bench_weights=bench_w,
                port_returns=port_r,
                bench_returns=bench_r,
            )
            brinson_d = brinson.to_dict()
        else:
            brinson_d = {"error": "need at least 2 strategies for Brinson"}
    except Exception as e:  # BLE001:FOG
        with fail_open_guard("backtest_runner:run_attribution"):
            brinson_d = {"error": str(e)}
    return {
        "factor_attribution": factor_d,
        "brinson": brinson_d,
        "trading_days": len({t.get("bar_time", "")[:10] for t in trades}) if trades else 0,
    }


# ── Main ────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description="Backtest runner with attribution")
    parser.add_argument(
        "--data",
        type=str,
        default="data/raw/xauusdc_m5_1y.csv",
        help="Path to OHLC CSV (default: data/raw/xauusdc_m5_1y.csv)",
    )
    parser.add_argument(
        "--strategies",
        type=str,
        nargs="*",
        default=["barrier_12bar", "micro_3bar", "statarb_dynamic"],
        help="Strategy names to run (default: all three)",
    )
    parser.add_argument(
        "--cash",
        type=float,
        default=10000.0,
        help="Initial cash (default: 10000)",
    )
    parser.add_argument(
        "--max-positions",
        type=int,
        default=3,
        help="Max concurrent positions (default: 3)",
    )
    parser.add_argument(
        "--cooldown",
        type=int,
        default=0,
        help="Cooldown bars between trades (default: 0)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default="",
        help="JSON output path (default: print to stdout)",
    )
    parser.add_argument(
        "--attribution",
        action="store_true",
        default=True,
        help="Run attribution after backtest (default: True)",
    )
    parser.add_argument(
        "--no-attribution",
        action="store_false",
        dest="attribution",
        help="Skip attribution",
    )
    args = parser.parse_args()

    # Load data
    data_path = Path(args.data)
    if not data_path.exists():
        print(f"ERROR: data file not found: {data_path}")
        return 1

    print(f"Loading {data_path} ...")
    try:
        feed = DataFeed.from_csv(str(data_path))
    except Exception as e:  # BLE001:FOG
        with fail_open_guard("backtest_runner:main"):
            print(f"ERROR: failed to load data: {e}")
            return 1
    print(f"  {len(feed.bars)} bars loaded")
    if feed.bars:
        print(f"  Range: {feed.bars[0].timestamp} → {feed.bars[-1].timestamp}")

    # Create strategy
    print(f"\nStrategies: {args.strategies}")
    strategy_fn = rule_based_strategies(args.strategies)

    # Run backtest
    print(
        f"\nRunning backtest (cash={args.cash}, max_positions={args.max_positions}, cooldown={args.cooldown}) ..."
    )
    engine = BacktestEngine(
        feed,
        strategy_fn,
        initial_cash=args.cash,
        max_positions=args.max_positions,
        cooldown_bars=args.cooldown,
    )
    result = engine.run()

    # Compute metrics
    metrics = compute_backtest_metrics(result)
    metrics["bars_processed"] = result.bars_processed

    print(f"\n{'='*60}")
    print("Backtest Results")
    print(f"{'='*60}")
    print(f"  Trades:           {metrics.get('total_trades', 0)}")
    print(f"  Win Rate:         {metrics.get('win_rate', 0):.2%}")
    print(f"  Net PnL:          ${metrics.get('net_pnl', 0):.2f}")
    print(f"  Total Cost:       ${metrics.get('total_cost', 0):.2f}")
    print(f"  Sharpe:           {metrics.get('sharpe_ratio', 0):.3f}")
    print(f"  Sortino:          {metrics.get('sortino_ratio', 0):.3f}")
    print(f"  Max DD:           {metrics.get('max_drawdown_pct', 0):.2%}")
    print(f"  Profit Factor:    {metrics.get('profit_factor', 0):.3f}")
    print(f"  Calmar:           {metrics.get('calmar_ratio', 0):.3f}")
    print(f"  Omega:            {metrics.get('omega_ratio', 0):.3f}")
    print(f"  Total Return:     {metrics.get('return_pct', 0):.2f}%")
    print(f"  Final Equity:     ${metrics.get('final_equity', 0):.2f}")

    # Attribution
    output: dict[str, Any] = {
        "config": {
            "data": args.data,
            "strategies": args.strategies,
            "cash": args.cash,
            "max_positions": args.max_positions,
            "cooldown": args.cooldown,
        },
        "metrics": metrics,
    }

    if args.attribution:
        print(f"\n{'='*60}")
        print("Attribution Analysis")
        print(f"{'='*60}")
        attr = run_attribution(result)
        output["attribution"] = attr
        if "error" in attr:
            print(f"  {attr['error']}: {attr.get('message', '')}")
        else:
            fa = attr.get("factor_attribution", {})
            if "error" not in fa:
                print(f"  Factor R²:        {fa.get('r_squared', 'N/A')}")
                print(f"  Trading Days:     {attr.get('trading_days', 0)}")
            br = attr.get("brinson", {})
            if "error" not in br:
                print(f"  Active Return:    {br.get('active_return', 'N/A')}")
                print(f"  Allocation Eff:   {br.get('total_allocation', 'N/A')}")
                print(f"  Selection Eff:    {br.get('total_selection', 'N/A')}")

    # Output
    serializable = _make_serializable(output)
    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(serializable, indent=2, default=str), encoding="utf-8")
        print(f"\nResults saved to {out_path}")
    else:
        print(f"\n{json.dumps(serializable, indent=2, default=str)}")

    return 0


def _make_serializable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _make_serializable(v) for k, v in obj.items()}
    if isinstance(obj, list | tuple):
        return [_make_serializable(v) for v in obj]
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, np.bool_):
        return bool(obj)
    return obj


if __name__ == "__main__":
    sys.exit(main())
