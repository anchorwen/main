from __future__ import annotations

import argparse
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from core.contracts.domain.communication_envelope import CommunicationEnvelope
from core.contracts.enums import CommunicationMessageType, CommunicationPriority
from core.deployment.environment_config import EnvironmentConfig
from core.deployment.service_container import ServiceContainer
from core.protocol.live_execution_contract import (
    attach_schema_metadata,
    execution_route,
    normalize_action,
)


def _coerce_positive_float_sg(value: Any) -> float | None:
    try:
        if value is None:
            return None
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


from core.protocol.schema_versions import SCHEMA_COMMUNICATION_ENVELOPE


def resolve_protection_flag_path(base_dir: str, protection_flag_path: str) -> Path:
    """Resolve flag path when relative: prefer cwd legacy layout, else anchor under base_dir."""
    raw = Path(protection_flag_path)
    if raw.is_absolute():
        return raw
    cwd_candidate = (Path.cwd() / raw).resolve()
    if cwd_candidate.exists():
        return cwd_candidate
    base = Path(base_dir)
    base = base.resolve() if base.is_absolute() else (Path.cwd() / base).resolve()
    if len(raw.parts) == 1:
        return (base / raw).resolve()
    return (base / raw.name).resolve()


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
    """Generic MT5 handoff: ``execution_payload`` becomes envelope.payload (Phase B / Phase C hook).

    Recommended keys: action, side, sl/tp, volume, position_ticket, execution_payload_schema (auto-filled).
    """
    if not ignore_protection_flag:
        protection_flag = resolve_protection_flag_path(base_dir, protection_flag_path)
        if protection_flag.exists():
            raise RuntimeError(f"protection guard active: {protection_flag}")

    body = attach_schema_metadata(dict(execution_payload))
    body.setdefault("symbol", symbol)

    act = normalize_action(body.get("action"))
    route = execution_route(act)
    if not skip_price_guard and route == "market_open":
        sl = _coerce_positive_float_sg(body.get("sl")) or _coerce_positive_float_sg(
            body.get("stop_loss")
        )
        tp = _coerce_positive_float_sg(body.get("tp")) or _coerce_positive_float_sg(
            body.get("take_profit")
        )
        if sl is None or tp is None:
            raise ValueError(
                "market_open requires positive sl and tp (or stop_loss/take_profit) for price guard"
            )
        side = str(body.get("side") or "long")
        price = _fetch_reference_price(
            mt5_terminal_path=mt5_terminal_path, symbol=symbol, side=side
        )
        _validate_sl_tp(side=side, stop_loss=sl, take_profit=tp, reference_price=price)

    intent_id = intent_id or f"live_exec_{uuid.uuid4().hex}"
    correlation_id = correlation_id or f"live_corr_{uuid.uuid4().hex}"

    cfg = EnvironmentConfig.production(
        base_dir=base_dir,
        adapter_name="mt5",
        live_dispatch_enabled=True,
        live_allowed_symbols=(symbol,),
        extensions={"mt5_terminal_path": mt5_terminal_path},
    )
    container = ServiceContainer(cfg).build()
    envelope = CommunicationEnvelope(
        schema_version=SCHEMA_COMMUNICATION_ENVELOPE,
        message_id=intent_id,
        correlation_id=correlation_id,
        causation_id=None,
        event_time=datetime.now(UTC).replace(tzinfo=None),
        producer="decision_engine",
        target="exec_bridge",
        message_type=CommunicationMessageType.DECISION_INTENT,
        priority=CommunicationPriority.NORMAL,
        payload=body,
        deadline_at=datetime.now(UTC).replace(tzinfo=None) + timedelta(seconds=30),
    )
    result = container.dispatcher.dispatch(envelope)  # type: ignore[reportOptionalMemberAccess]
    return {
        "adapter": result.adapter_name,
        "status": str(result.status),
        "transport": getattr(result, "transport_metadata", None),
        "intent_id": intent_id,
    }


def dispatch_live_open_order(
    *,
    base_dir: str,
    mt5_terminal_path: str,
    symbol: str,
    side: str,
    stop_loss: float,
    take_profit: float,
    intent_id: str | None = None,
    correlation_id: str | None = None,
    skip_price_guard: bool = False,
    ignore_protection_flag: bool = False,
    protection_flag_path: str = "data/live_dispatch_block.flag",
    volume: float | None = None,
) -> dict:
    """Open-market helper; delegates to :func:`dispatch_live_mt5_execution`."""
    iid = intent_id or f"live_open_{uuid.uuid4().hex}"
    execution_payload: dict[str, Any] = {
        "intent_id": iid,
        "action": "open",
        "side": side,
        "sl": stop_loss,
        "tp": take_profit,
    }
    if volume is not None and volume > 0:
        execution_payload["volume"] = float(volume)

    return dispatch_live_mt5_execution(
        base_dir=base_dir,
        mt5_terminal_path=mt5_terminal_path,
        symbol=symbol,
        execution_payload=execution_payload,
        intent_id=iid,
        correlation_id=correlation_id,
        skip_price_guard=skip_price_guard,
        ignore_protection_flag=ignore_protection_flag,
        protection_flag_path=protection_flag_path,
    )


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
    import MetaTrader5 as mt5  # type: ignore

    if not mt5.initialize(path=mt5_terminal_path):  # type: ignore[reportAttributeAccessIssue]
        raise RuntimeError(f"mt5 initialize failed: {mt5.last_error()}")  # type: ignore[reportAttributeAccessIssue]
    try:
        tick = mt5.symbol_info_tick(symbol)  # type: ignore[reportAttributeAccessIssue]
        if tick is None:
            raise RuntimeError(f"symbol tick unavailable: {symbol}")
        if side == "long":
            return float(tick.ask)
        return float(tick.bid)
    finally:
        mt5.shutdown()  # type: ignore[reportAttributeAccessIssue]


def _validate_sl_tp(
    *, side: str, stop_loss: float, take_profit: float, reference_price: float
) -> None:
    if side == "long":
        if not (stop_loss < reference_price < take_profit):
            raise ValueError(
                f"invalid long sl/tp: require stop_loss < price < take_profit, got {stop_loss} < {reference_price} < {take_profit}"
            )
        return
    if not (take_profit < reference_price < stop_loss):
        raise ValueError(
            f"invalid short sl/tp: require take_profit < price < stop_loss, got {take_profit} < {reference_price} < {stop_loss}"
        )


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
