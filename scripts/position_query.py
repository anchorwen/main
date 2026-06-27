"""Direct MT5 query — authoritative position & deal history from broker.

Usage:
    # Current positions (default)
    python scripts/position_query.py                         # BTC positions
    python scripts/position_query.py --xau                   # XAU positions
    python scripts/position_query.py --symbol BTCUSDc        # filter symbol

    # Deal history
    python scripts/position_query.py --deals                 # BTC deals (30d)
    python scripts/position_query.py --deals --xau           # XAU deals
    python scripts/position_query.py --deals --days 7        # last 7 days
    python scripts/position_query.py --deals --symbol BTCUSDc --days 3

    # JSON output (both modes)
    python scripts/position_query.py --deals --json

Terminal presets:
    --xau    → D:\\exness\\MetaTrader 5 EXNESS2\\terminal64.exe
    default  → D:\\MetaTrader 5\\terminal64.exe (BTC)
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BTC_TERMINAL = r"D:\MetaTrader 5\terminal64.exe"
XAU_TERMINAL = r"D:\exness\MetaTrader 5 EXNESS2\terminal64.exe"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="position_query")
    p.add_argument("--symbol", default=None, help="Filter by symbol (e.g. BTCUSDc, XAUUSDc)")
    p.add_argument(
        "--terminal",
        default=None,
        help="MT5 terminal path (overrides --xau / default BTC)",
    )
    p.add_argument("--xau", action="store_true", help="Use XAU/Exness terminal")
    p.add_argument(
        "--deals",
        action="store_true",
        help="Query deal history instead of current positions",
    )
    p.add_argument(
        "--days",
        type=int,
        default=30,
        help="Lookback days for --deals mode (default: 30)",
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
    except (RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:  # BLE001:FOG
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


def run_deals(
    symbol: str | None = None,
    terminal_path: str | None = None,
    days: int = 30,
) -> dict[str, Any]:
    """Query MT5 deal history. Returns structured dict with deals + summary."""
    result: dict[str, Any] = {
        "timestamp": _utc_now(),
        "connected": False,
        "account": {},
        "symbol_filter": symbol,
        "deal_count": 0,
        "deals": [],
        "summary": {},
        "error": None,
    }

    try:
        import MetaTrader5 as mt5
    except (RuntimeError, ValueError, KeyError, TypeError, OSError) as exc:
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

        # ── Deal History ──
        from datetime import datetime as dt
        from datetime import timedelta

        to_dt = dt.now(UTC)
        from_dt = to_dt - timedelta(days=days)

        deals_raw = mt5.history_deals_get(from_dt, to_dt)
        trade_types = {mt5.DEAL_TYPE_BUY, mt5.DEAL_TYPE_SELL}

        deals: list[dict[str, Any]] = []
        for d in deals_raw or []:
            if symbol and d.symbol != symbol:
                continue
            deal_type = (
                "BUY"
                if d.type == mt5.DEAL_TYPE_BUY
                else "SELL"
                if d.type == mt5.DEAL_TYPE_SELL
                else f"OTHER({d.type})"
            )
            pnl = (
                float(d.profit) + float(getattr(d, "commission", 0)) + float(getattr(d, "swap", 0))
            )
            deals.append(
                {
                    "ticket": d.ticket,
                    "position_ticket": getattr(d, "position_id", 0),
                    "time": dt.fromtimestamp(d.time, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "type": deal_type,
                    "symbol": d.symbol,
                    "volume": float(d.volume),
                    "price": float(d.price),
                    "profit": float(d.profit),
                    "commission": float(getattr(d, "commission", 0)),
                    "swap": float(getattr(d, "swap", 0)),
                    "pnl": round(pnl, 2),
                    "reason": d.reason,
                }
            )

        trade_deals = [d for d in deals if d["type"] in ("BUY", "SELL")]
        wins = sum(1 for d in trade_deals if d["pnl"] > 0)
        losses = sum(1 for d in trade_deals if d["pnl"] < 0)
        zeroes = sum(1 for d in trade_deals if d["pnl"] == 0)
        total_pnl = round(sum(d["pnl"] for d in trade_deals), 2)

        result["deal_count"] = len(deals)
        result["deals"] = deals
        result["summary"] = {
            "total_deals": len(deals),
            "trade_deals": len(trade_deals),
            "wins": wins,
            "losses": losses,
            "breakeven": zeroes,
            "total_pnl": total_pnl,
            "days": days,
            "lookback_from": from_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "lookback_to": to_dt.strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
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


REASON_LABELS: dict[int, str] = {
    0: "CLIENT",
    1: "MOBILE",
    2: "WEB",
    3: "EXPERT",
    4: "SL",
    5: "TP",
    6: "SO",
    7: "ROLL",
    8: "VMARGIN",
    9: "SPLIT",
}


def print_deals_table(result: dict[str, Any]) -> None:
    """Human-readable deal history table."""
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
        pnl_color = 32 if pnl >= 0 else 31
        print(
            f"Account: {acc['login']}  Balance: {bal:.2f}  "
            f"Equity: {eq:.2f}  "
            f"Float PnL: {_color(f'{pnl:+.2f}', pnl_color)}  "
            f"Margin: {acc['margin']:.2f} ({acc['margin_level']:.1f}%)"
        )
    else:
        print("Account info unavailable.")

    s = result["summary"]
    symbol_info = f" (filter: {result['symbol_filter']})" if result["symbol_filter"] else ""
    print(
        f"\nDeal History{symbol_info}: {s['total_deals']} deals "
        f"({s['trade_deals']} trades, {s['days']}d lookback)"
    )
    print(f"Lookback: {s['lookback_from']} → {s['lookback_to']}")
    total_pnl_color = 32 if s["total_pnl"] >= 0 else 31
    total_pnl_label = f"{s['total_pnl']:+.2f}"
    print(
        f"W/L/BE: {s['wins']}/{s['losses']}/{s['breakeven']}  "
        f"Total PnL: {_color(total_pnl_label, total_pnl_color)}"
    )

    deals = result["deals"]
    if not deals:
        print("  (no deals found)")
        return

    # Header
    header = (
        f"{'Time':<20} {'Deal':>10} {'Pos':>10} {'Type':<6} {'Symbol':<10} "
        f"{'Vol':>6} {'Price':>10} {'PnL':>10} {'Reason':<6}"
    )
    print(f"\n{header}")
    print("-" * len(header))

    shown_pnl = 0.0
    for d in deals:
        dt = d["time"][:19].replace("T", " ")
        pnl_val = d["pnl"]
        shown_pnl += pnl_val
        pnl_str = _color(f"{pnl_val:+.2f}", 32 if pnl_val >= 0 else 31)
        reason_label = REASON_LABELS.get(d["reason"], str(d["reason"]))
        print(
            f"{dt:<20} {d['ticket']:>10} {d['position_ticket']:>10} {d['type']:<6} "
            f"{d['symbol']:<10} {d['volume']:>6.2f} {d['price']:>10.2f} "
            f"{pnl_str:>13} {reason_label:<6}"
        )

    print("-" * len(header))
    total_str = _color(f"{shown_pnl:+.2f}", 32 if shown_pnl >= 0 else 31)
    print(f"{'TOTAL':>20} {'':>10} {'':>10} {'':6} {'':10} {'':>6} {'':>10} {total_str:>13}")
    print()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # Resolve terminal path: --terminal > --xau preset > default BTC
    if args.terminal:
        terminal = args.terminal
    elif args.xau:
        terminal = XAU_TERMINAL
    else:
        terminal = BTC_TERMINAL

    if args.deals:
        result = run_deals(symbol=args.symbol, terminal_path=terminal, days=args.days)
    else:
        result = run(symbol=args.symbol, terminal_path=terminal)

    if args.json:
        import json

        print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    else:
        if args.deals:
            print_deals_table(result)
        else:
            print_table(result)

    if result["error"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
