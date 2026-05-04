"""Optional MT5 spread probe for live_dispatch_policy (best-effort)."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def probe_spread(
    *, terminal_path: str | None, symbol: str, max_spread_points: float
) -> dict[str, Any]:
    """Return probe result; never raises — callers decide blocking."""
    result: dict[str, Any] = {
        "skipped": True,
        "blocked": False,
        "reasons": [],
        "spread_points": None,
        "detail": None,
    }
    try:
        import MetaTrader5 as mt5  # type: ignore[import-untyped]
    except Exception as exc:  # pragma: no cover - optional dependency
        result["detail"] = f"metaTrader5_import_failed:{exc}"
        return result

    kwargs: dict[str, Any] = {}
    if terminal_path:
        p = Path(terminal_path)
        if not p.exists():
            result["detail"] = "terminal_path_missing"
            return result
        kwargs["path"] = str(p)

    if not mt5.initialize(
        **kwargs
    ):  # pragma: no cover - needs terminal  # type: ignore[reportAttributeAccessIssue]
        result["detail"] = f"initialize_failed:{mt5.last_error()}"  # type: ignore[reportAttributeAccessIssue]
        mt5.shutdown()  # type: ignore[reportAttributeAccessIssue]
        return result

    try:
        info = mt5.symbol_info(symbol)  # type: ignore[reportAttributeAccessIssue]
        if info is None:
            result["detail"] = "symbol_info_none"
            return result
        if not info.visible:
            mt5.symbol_select(symbol, True)  # type: ignore[reportAttributeAccessIssue]
        tick = mt5.symbol_info_tick(symbol)  # type: ignore[reportAttributeAccessIssue]
        if tick is None:
            result["detail"] = "tick_none"
            return result
        point = float(getattr(info, "point", 0.0) or 0.0)
        if point <= 0:
            result["detail"] = "invalid_point"
            return result
        spread_points = (float(tick.ask) - float(tick.bid)) / point
        result["skipped"] = False
        result["spread_points"] = round(spread_points, 6)
        if spread_points > max_spread_points:
            result["blocked"] = True
            result["reasons"].append(
                f"spread_points_exceeded({spread_points:.3f}>{max_spread_points:.3f})"
            )
        result["detail"] = "ok"
        return result
    finally:
        mt5.shutdown()  # type: ignore[reportAttributeAccessIssue]
