"""Broker-agnostic live order dispatch.

Extracted from scripts/send_live_order.py to eliminate reverse dependency
(core → scripts). The original script now delegates to this module.

Usage:
    from core.execution.live_order_sender import dispatch_live_order, dispatch_live_open_order
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from core.contracts.domain.communication_envelope import CommunicationEnvelope
from core.contracts.enums import CommunicationMessageType, CommunicationPriority
from core.deployment.environment_config import EnvironmentConfig
from core.deployment.service_container import ServiceContainer
from core.execution.broker_adapter import BrokerAdapter
from core.protocol.live_execution_contract import (
    attach_schema_metadata,
    execution_route,
    normalize_action,
)
from core.protocol.schema_versions import SCHEMA_COMMUNICATION_ENVELOPE

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _coerce_positive_float_sg(value: Any) -> float | None:
    try:
        if value is None:
            return None
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def resolve_protection_flag_path(base_dir: str, protection_flag_path: str) -> Path:
    """Resolve flag path when relative: prefer PROJECT_ROOT, then cwd, then base_dir."""
    raw = Path(protection_flag_path)
    if raw.is_absolute():
        return raw
    project_candidate = (PROJECT_ROOT / raw).resolve()
    if project_candidate.exists():
        return project_candidate
    cwd_candidate = (Path.cwd() / raw).resolve()
    if cwd_candidate.exists():
        return cwd_candidate
    base = Path(base_dir)
    base = base.resolve() if base.is_absolute() else (PROJECT_ROOT / base).resolve()
    if len(raw.parts) == 1:
        return (base / raw).resolve()
    return (base / raw.name).resolve()


def _validate_sl_tp(
    *, side: str, stop_loss: float, take_profit: float, reference_price: float
) -> None:
    if side == "long":
        if not (stop_loss < reference_price < take_profit):
            raise ValueError(
                f"invalid long sl/tp: require stop_loss < price < take_profit, "
                f"got {stop_loss} < {reference_price} < {take_profit}"
            )
        return
    if not (take_profit < reference_price < stop_loss):
        raise ValueError(
            f"invalid short sl/tp: require take_profit < price < stop_loss, "
            f"got {take_profit} < {reference_price} < {stop_loss}"
        )


def dispatch_live_order(
    *,
    base_dir: str,
    broker: BrokerAdapter | None,
    symbol: str,
    execution_payload: dict[str, Any],
    intent_id: str | None = None,
    correlation_id: str | None = None,
    skip_price_guard: bool = False,
    ignore_protection_flag: bool = False,
    protection_flag_path: str = "data/live_dispatch_block.flag",
    adapter_name: str = "mt5",
    extensions: dict[str, Any] | None = None,
) -> dict:
    """Broker-agnostic order dispatch — the canonical entry point for all venues.

    Accepts a :class:`BrokerAdapter` for price validation instead of
    hard-coding MT5.  When you swap MT5 for FIX / IB / cloud, you only
    need to provide a different ``broker`` — this function stays unchanged.
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
        mid, bid, ask = broker.fetch_prices(symbol)
        price = ask if side == "long" else bid
        _validate_sl_tp(side=side, stop_loss=sl, take_profit=tp, reference_price=price)

    intent_id = intent_id or f"live_exec_{uuid.uuid4().hex}"
    correlation_id = correlation_id or f"live_corr_{uuid.uuid4().hex}"

    cfg = EnvironmentConfig.production(
        base_dir=base_dir,
        adapter_name=adapter_name,
        live_dispatch_enabled=True,
        live_allowed_symbols=(symbol,),
        extensions=extensions or {},
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
    result = container.dispatcher.dispatch(envelope)
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
    magic: int | None = None,
    hard_sl: float = 0.0,
    brain_ids: list[str] | None = None,
    # ── Trade context (passthrough — logged to journal for later analysis) ──
    entry_context: dict[str, Any] | None = None,
) -> dict:
    """Open-market helper; dispatches via broker-agnostic :func:`dispatch_live_order`.

    ``entry_context`` carries feature vectors, regime, ATR, brain predictions,
    and other metadata that the bridge passes through to the journal for
    post-trade analysis.  It is never interpreted by the bridge itself.
    """
    iid = intent_id or f"live_open_{uuid.uuid4().hex}"
    execution_payload: dict[str, Any] = {
        "intent_id": iid,
        "action": "open",
        "side": side,
        "sl": stop_loss,
        "tp": take_profit,
    }
    if hard_sl > 0:
        execution_payload["hard_sl"] = round(hard_sl, 5)
    if volume is not None and volume > 0:
        execution_payload["volume"] = float(volume)
    if magic is not None:
        execution_payload["magic"] = int(magic)
    if brain_ids:
        execution_payload["brain_ids"] = list(brain_ids)
    if entry_context:
        execution_payload["entry_context"] = dict(entry_context)

    if skip_price_guard:
        return dispatch_live_order(
            base_dir=base_dir,
            broker=None,
            symbol=symbol,
            execution_payload=execution_payload,
            intent_id=iid,
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
            intent_id=iid,
            correlation_id=correlation_id,
            skip_price_guard=skip_price_guard,
            ignore_protection_flag=ignore_protection_flag,
            protection_flag_path=protection_flag_path,
            adapter_name="mt5",
            extensions={"mt5_terminal_path": mt5_terminal_path},
        )
    finally:
        _mt5.shutdown()
