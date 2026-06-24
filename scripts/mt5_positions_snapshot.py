"""Best-effort MT5 positions snapshot for ops reporting."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="mt5_positions_snapshot")
    parser.add_argument("--mt5-terminal-path", default=None)
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--output", default=None)
    return parser


def _utc_now() -> str:
    return (
        datetime.now(UTC)
        .replace(tzinfo=None)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def build_snapshot(*, mt5_terminal_path: str | None, symbol: str | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "mt5_positions_snapshot.v1",
        "generated_at": _utc_now(),
        "symbol_filter": symbol,
        "connected": False,
        "position_count": 0,
        "positions": [],
        "error": None,
    }
    try:
        import MetaTrader5 as mt5
    except Exception as exc:  # pragma: no cover  # noqa: BLE001
        try:  # noqa: BLE001 (was: FOG/LAC)
            payload["error"] = f"metaTrader5_import_failed:{exc}"
            return payload
        except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # noqa: BLE001
            pass
    kwargs: dict[str, Any] = {}
    if mt5_terminal_path:
        p = Path(mt5_terminal_path)
        if not p.exists():
            payload["error"] = "terminal_path_missing"
            return payload
        kwargs["path"] = str(p)

    if not mt5.initialize(**kwargs):  # pragma: no cover  # type: ignore[reportAttributeAccessIssue]
        payload["error"] = f"initialize_failed:{mt5.last_error()}"
        mt5.shutdown()
        return payload
    payload["connected"] = True
    try:
        raw = mt5.positions_get(symbol=symbol) if symbol else mt5.positions_get()
        rows = list(raw or [])
        positions = []
        for row in rows:
            positions.append(
                {
                    "ticket": getattr(row, "ticket", None),
                    "symbol": getattr(row, "symbol", None),
                    "type": getattr(row, "type", None),
                    "volume": getattr(row, "volume", None),
                    "price_open": getattr(row, "price_open", None),
                    "sl": getattr(row, "sl", None),
                    "tp": getattr(row, "tp", None),
                    "profit": getattr(row, "profit", None),
                    "time": getattr(row, "time", None),
                }
            )
        payload["positions"] = positions
        payload["position_count"] = len(positions)
        return payload
    finally:
        mt5.shutdown()


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    snap = build_snapshot(mt5_terminal_path=args.mt5_terminal_path, symbol=args.symbol)
    rendered = json.dumps(snap, indent=2, ensure_ascii=False)
    print(rendered)
    if args.output:
        out = Path(args.output)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
