"""Direct MT5 position query — authoritative position count from broker.

Usage:
    python scripts/position_query.py                    # all positions
    python scripts/position_query.py --symbol XAUUSDc   # gold only
    python scripts/position_query.py --terminal "D:\\MetaTrader 5\\terminal64.exe"
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from core.runtime.fault_handler import fail_open_guard


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="position_query")
    p.add_argument("--symbol", default=None, help="Filter by symbol (e.g. XAUUSDc)")
    p.add_argument(
        "--terminal",
        default=None,
        help="MT5 terminal path (default: D:\\MetaTrader 5\\terminal64.exe)",
    )
    p.add_argument("--json", action="store_true", help="Output as JSON instead of table")
    return p


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def run(symbol: str | None = None, terminal_path: str | None = None) -> dict[str, Any]:
    """Query MT5 positions and account info. Returns structured dict."""
    result: dict[str, Any] = {
        "timestamp": _utc_now(),
        "connected": False,
        "account": {},
        "symbol_filter": symbol,
        "position_count": 0,
        "positions": [],
        "error": None,
    }

    try:
        import MetaTrader5 as mt5
    except Exception as exc:  # BLE001:FOG
        with fail_open_guard("position_query:run"):
            result["error"] = f"MetaTrader5 import failed: {exc}"
            return result
    kw: dict[str, Any] = {}
    if terminal_path:
        p = Path(terminal_path)
        if not p.exists():
            result["error"] = f"terminal_path_missing: {terminal_path}"
            return result
        kw["path"] = str(p)

    if not mt5.initialize(**kw):
        result["error"] = f"initialize_failed: {mt5.last_error()}"
        mt5.shutdown()
        return result

    result["connected"] = True
    try:
        # ── Account info ──
        acc = mt5.account_info()
        if acc is not None:
            result["account"] = {
                "login": getattr(acc, "login", None),
                "balance": float(getattr(acc, "balance", 0)),
                "equity": float(getattr(acc, "equity", 0)),
                "margin": float(getattr(acc, "margin", 0)),
                "margin_free": float(getattr(acc, "margin_free", 0)),
                "margin_level": float(getattr(acc, "margin_level", 0)),
                "currency": getattr(acc, "currency", ""),
            }

        # ── Positions ──
        raw = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
        for row in raw or []:
            pos_type = "BUY" if getattr(row, "type", -1) == 0 else "SELL"
            result["positions"].append(
                {
                    "ticket": getattr(row, "ticket", 0),
                    "symbol": getattr(row, "symbol", ""),
                    "type": pos_type,
                    "volume": float(getattr(row, "volume", 0)),
                    "price_open": float(getattr(row, "price_open", 0)),
                    "price_current": float(getattr(row, "price_current", 0)),
                    "sl": float(getattr(row, "sl", 0)),
                    "tp": float(getattr(row, "tp", 0)),
                    "profit": float(getattr(row, "profit", 0)),
                    "swap": float(getattr(row, "swap", 0)),
                    "comment": str(getattr(row, "comment", "")),
                    "magic": getattr(row, "magic", 0),
                }
            )
        result["position_count"] = len(result["positions"])
        return result
    finally:
        mt5.shutdown()


def _color(text: str, code: int) -> str:
    """ANSI color if stdout is a terminal."""
    if sys.stdout.isatty():
        return f"\033[{code}m{text}\033[0m"
    return text


def print_table(result: dict[str, Any]) -> None:
    """Human-readable position table."""
    if result["error"]:
        print(f"ERROR: {result['error']}")
        return

    if not result["connected"]:
        print("MT5 not connected.")
        return

    acc = result["account"]
    if acc:
        eq = acc["equity"]
        bal = acc["balance"]
        pnl = eq - bal
        pnl_color = 32 if pnl >= 0 else 31  # green/red
        print(
            f"Account: {acc['login']}  Balance: {bal:.2f}  "
            f"Equity: {eq:.2f}  "
            f"Float PnL: {_color(f'{pnl:+.2f}', pnl_color)}  "
            f"Margin: {acc['margin']:.2f} ({acc['margin_level']:.1f}%)"
        )
    else:
        print("Account info unavailable.")

    positions = result["positions"]
    print(f"\nPositions: {len(positions)} (filter: {result['symbol_filter'] or 'ALL'})")
    print(f"Snapshot: {result['timestamp']}")

    if not positions:
        print("  (no open positions)")
        return

    # Header
    header = (
        f"{'Ticket':>10}  {'Symbol':<10}  {'Type':<5}  {'Vol':>6}  "
        f"{'Open':>10}  {'Current':>10}  {'PnL':>10}  {'SL':>8}  {'TP':>8}  {'Magic':>8}  {'Comment'}"
    )
    print(f"\n{header}")
    print("-" * len(header))

    total_pnl = 0.0
    total_vol = 0.0
    for p in positions:
        pnl = p["profit"]
        total_pnl += pnl
        total_vol += p["volume"]
        pnl_str = _color(f"{pnl:+.2f}", 32 if pnl >= 0 else 31)
        comment = str(p["comment"])[:30] if p["comment"] else ""
        print(
            f"{p['ticket']:>10}  {p['symbol']:<10}  {p['type']:<5}  {p['volume']:>6.2f}  "
            f"{p['price_open']:>10.2f}  {p['price_current']:>10.2f}  {pnl_str:>13}  "
            f"{p['sl']:>8.2f}  {p['tp']:>8.2f}  {p['magic']:>8}  {comment}"
        )

    print("-" * len(header))
    total_pnl_str = _color(f"{total_pnl:+.2f}", 32 if total_pnl >= 0 else 31)
    print(
        f"{'TOTAL':>10}  {'':10}  {'':5}  {total_vol:>6.2f}  {'':>10}  {'':>10}  {total_pnl_str:>13}"
    )
    print()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    terminal = args.terminal or r"D:\MetaTrader 5\terminal64.exe"
    result = run(symbol=args.symbol, terminal_path=terminal)

    if args.json:
        import json

        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    else:
        print_table(result)

    if result["error"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
