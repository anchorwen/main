"""Live order dispatch CLI — delegates to core.execution.live_order_sender.

Kept as thin wrapper for backward compatibility. The canonical implementations
are in ``core.execution.live_order_sender``.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

# Re-export canonical implementations
from core.execution.live_order_sender import (  # noqa: F401
    _validate_sl_tp,
    dispatch_live_open_order,
    dispatch_live_order,
    resolve_protection_flag_path,
)

# ── MT5-specific wrapper (backward compat) ──


def dispatch_live_mt5_execution(
    *,
    base_dir: str,
    mt5_terminal_path: str,
    symbol: str,
    execution_payload: dict[str, Any],
    intent_id: str | None = None,
    correlation_id: str | None = None,
    skip_price_guard: bool = False,
    ignore_protection_flag: bool = False,
    protection_flag_path: str = "data/live_dispatch_block.flag",
) -> dict:
    """MT5-specific handoff — **backward compat only, prefer** :func:`dispatch_live_order`."""
    if skip_price_guard:
        return dispatch_live_order(
            base_dir=base_dir,
            broker=None,
            symbol=symbol,
            execution_payload=execution_payload,
            intent_id=intent_id,
            correlation_id=correlation_id,
            skip_price_guard=True,
            ignore_protection_flag=ignore_protection_flag,
            protection_flag_path=protection_flag_path,
            adapter_name="mt5",
            extensions={"mt5_terminal_path": mt5_terminal_path},
        )

    import MetaTrader5 as _mt5

    if not _mt5.initialize(path=mt5_terminal_path):
        raise RuntimeError(f"mt5 initialize failed: {_mt5.last_error()}")
    try:
        from core.execution.mt5_broker_adapter import MT5BrokerAdapter

        broker = MT5BrokerAdapter(_mt5)
        return dispatch_live_order(
            base_dir=base_dir,
            broker=broker,
            symbol=symbol,
            execution_payload=execution_payload,
            intent_id=intent_id,
            correlation_id=correlation_id,
            skip_price_guard=skip_price_guard,
            ignore_protection_flag=ignore_protection_flag,
            protection_flag_path=protection_flag_path,
            adapter_name="mt5",
            extensions={"mt5_terminal_path": mt5_terminal_path},
        )
    finally:
        _mt5.shutdown()


# ── CLI ──


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="send_live_order")
    parser.add_argument("--base-dir", default="data")
    parser.add_argument("--mt5-terminal-path", required=True)
    parser.add_argument("--symbol", default="XAUUSDc")
    parser.add_argument("--side", choices=["long", "short"], default="long")
    parser.add_argument("--stop-loss", type=float, required=True)
    parser.add_argument("--take-profit", type=float, required=True)
    parser.add_argument(
        "--volume", type=float, default=None, help="Optional lots; overrides bridge default-volume"
    )
    parser.add_argument("--intent-id", required=True)
    parser.add_argument("--correlation-id", required=True)
    parser.add_argument("--skip-price-guard", action="store_true")
    parser.add_argument(
        "--protection-flag-path",
        default="data/live_dispatch_block.flag",
        help="Relative paths: try cwd first, then anchor under --base-dir (resolve_protection_flag_path).",
    )
    parser.add_argument("--ignore-protection-flag", action="store_true")
    return parser


def _fetch_reference_price(*, mt5_terminal_path: str, symbol: str, side: str) -> float:
    import MetaTrader5 as mt5

    if not mt5.initialize(path=mt5_terminal_path):
        raise RuntimeError(f"mt5 initialize failed: {mt5.last_error()}")
    try:
        tick = mt5.symbol_info_tick(symbol)
        if tick is None:
            raise RuntimeError(f"symbol tick unavailable: {symbol}")
        if side == "long":
            return float(tick.ask)
        return float(tick.bid)
    finally:
        mt5.shutdown()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        out = dispatch_live_open_order(
            base_dir=args.base_dir,
            mt5_terminal_path=args.mt5_terminal_path,
            symbol=args.symbol,
            side=args.side,
            stop_loss=float(args.stop_loss),
            take_profit=float(args.take_profit),
            intent_id=args.intent_id,
            correlation_id=args.correlation_id,
            skip_price_guard=args.skip_price_guard,
            ignore_protection_flag=args.ignore_protection_flag,
            protection_flag_path=args.protection_flag_path,
            volume=args.volume,
        )
    except RuntimeError as exc:
        print(
            json.dumps({"error": "protection_or_dispatch", "detail": str(exc)}, ensure_ascii=False)
        )
        return 2
    except ValueError as exc:
        print(json.dumps({"error": "validation", "detail": str(exc)}, ensure_ascii=False))
        return 1
    print(json.dumps(out, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
