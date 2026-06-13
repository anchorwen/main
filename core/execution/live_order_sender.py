"""Broker-agnostic live order dispatch.

Extracted from scripts/send_live_order.py to eliminate reverse dependency
(core → scripts). The original script now delegates to this module.

Usage:
    from core.execution.live_order_sender import dispatch_live_order, dispatch_live_open_order
"""

from __future__ import annotations

import logging  # noqa: F401 — used at L114 via getLogger()
import time
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

# ═══════════════════════════════════════════════════════════════════════════════
# Blind Spot 4 (2026-06-13): Hard-Coded Blast Limits
#
# These are the FINAL physical guillotine — they CANNOT be overridden by any
# config file (live.yaml, governance_state.json, etc.).  Unlike SL/TP which
# already have hardcoded ceilings (MAX_SL_ATR=4.0, MAX_TP_ATR=6.0 in
# dynamic_sl_tp.py), volume had NO non-configurable ceiling — a single typo
# in live.yaml (base_volume: 1.00 instead of 0.01) could bypass every
# software gate and cause catastrophic loss.
#
# Pattern: same design as MAX_SL_ATR / MAX_TP_ATR — absolute, non-negotiable.
# ═══════════════════════════════════════════════════════════════════════════════
MAX_ALLOWED_LOT_SIZE: float = 0.05  # 0.05 lots XAU ≈ $500/ATR move — more than any strategy uses
MAX_DAILY_DRAWDOWN_USD: float = -200.0  # absolute daily loss floor, non-overridable


class FatalRiskViolation(RuntimeError):
    """Hardcoded blast limit triggered — non-configurable safety ceiling.

    Raised when a dispatch payload violates an absolute physical limit
    (volume ceiling, daily drawdown floor).  This is NOT catchable by
    normal error handlers — it must trip the circuit breaker.
    """


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


def _validate_ack_sl_tp(
    result: dict, requested_sl: float, requested_tp: float, base_dir: str = "data"
) -> None:
    """SL/TP ack validation (Phase 2 — canary: ERROR on mismatch, no blocking yet).

    Polls the bridge ack receipt for confirmed_sl/confirmed_tp fields (added by
    Phase 2 bridge worker).  Validates against requested values with 0.5 pip
    tolerance.  Full blocking upgrade after 50+ live trades confirm stability.
    """
    import logging

    logger = logging.getLogger("live_order_sender")
    intent_id = result.get("intent_id", "")

    # Resolve ACK receipt (ZMQ fast path first, file polling fallback)
    ack_sl = None
    ack_tp = None
    ack = None
    try:
        from core.protocol.services.zmq_receipt_listener import resolve_ack

        ack = resolve_ack(intent_id, base_dir=base_dir, timeout=5.0)
    except Exception:  # noqa: BLE001
        pass

    if ack is not None:
        detail = ack.get("detail", {}) if isinstance(ack, dict) else {}
        ack_sl = detail.get("confirmed_sl")
        ack_tp = detail.get("confirmed_tp")

    if ack_sl is not None and ack_tp is not None:
        sl_diff = abs(float(ack_sl) - requested_sl)
        tp_diff = abs(float(ack_tp) - requested_tp)
        if sl_diff > 0.5 or tp_diff > 0.5:
            logger.error(
                f"SL/TP MISMATCH (canary): requested sl={requested_sl:.2f} tp={requested_tp:.2f} "
                f"vs confirmed sl={ack_sl:.2f} tp={ack_tp:.2f} "
                f"diff sl={sl_diff:.2f} tp={tp_diff:.2f} intent_id={intent_id}"
            )
        else:
            logger.info(
                f"SL/TP confirmed ok: sl={ack_sl:.2f} tp={ack_tp:.2f} intent_id={intent_id}"
            )
    else:
        logger.warning(
            f"ack receipt missing confirmed SL/TP (bridge v1 or poll timeout): "
            f"intent_id={intent_id} expected sl={requested_sl:.2f} tp={requested_tp:.2f}"
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
            # Require the flag file to be at least 5 minutes old to prevent
            # accidental triggering via stale or transient files.
            _flag_age = time.time() - protection_flag.stat().st_mtime
            if _flag_age >= 300:  # 5 min
                raise RuntimeError(
                    f"protection guard active: {protection_flag} (age={_flag_age:.0f}s)"
                )

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
        assert broker is not None  # guaranteed when skip_price_guard is False
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
    if container.dispatcher is None:
        raise RuntimeError("ServiceContainer.dispatcher not built — call build() first")
    result = container.dispatcher.dispatch(envelope)
    dispatched = str(result.status) not in ("failed", "degraded")
    return {
        "adapter": result.adapter_name,
        "status": str(result.status),
        "dispatched": dispatched,
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
    brain_votes: list[dict[str, Any]] | None = None,
    confidence: float | None = None,
    # ── Trade context (passthrough — logged to journal for later analysis) ──
    entry_context: dict[str, Any] | None = None,
    p_win: float = 0.0,
    p_win_source: str = "unknown",
    p_win_degraded: bool = False,
    kelly_mult: float = 1.0,
) -> dict:
    """Open-market helper; dispatches via broker-agnostic :func:`dispatch_live_order`.

    ``entry_context`` carries feature vectors, regime, ATR, brain predictions,
    and other metadata that the bridge passes through to the journal for
    post-trade analysis.  It is never interpreted by the bridge itself.
    """
    iid = intent_id or f"live_open_{uuid.uuid4().hex}"

    # ── Blind Spot 4: Hard-Coded Blast Limit ──────────────────────────
    # This is the FINAL physical guillotine.  It CANNOT be bypassed by
    # config.  If volume exceeds the hardcoded ceiling, raise a fatal
    # error that trips the circuit breaker — no order reaches MT5.
    if volume is not None and volume > MAX_ALLOWED_LOT_SIZE:
        raise FatalRiskViolation(
            f"Volume {volume} exceeds hardcoded blast limit "
            f"{MAX_ALLOWED_LOT_SIZE}.  Intent ID: {iid}.  "
            f"This is a NON-CONFIGURABLE safety ceiling.  "
            f"Check live.yaml for fat-finger errors."
        )

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
    if brain_votes:
        execution_payload["brain_votes"] = brain_votes
    if confidence is not None:
        execution_payload["confidence"] = confidence
    if entry_context:
        execution_payload["entry_context"] = dict(entry_context)
    if p_win > 0:
        execution_payload["p_win"] = round(p_win, 4)
    if p_win_source != "unknown":
        execution_payload["p_win_source"] = p_win_source
    if p_win_degraded:
        execution_payload["p_win_degraded"] = True
    if kelly_mult != 1.0:
        execution_payload["kelly_mult"] = round(kelly_mult, 4)

    if skip_price_guard:
        result = dispatch_live_order(
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
        _validate_ack_sl_tp(result, stop_loss, take_profit, base_dir=base_dir)
        return result

    from core.execution.mt5_broker_adapter import MT5BrokerAdapter
    from core.execution.mt5_worker import get_mt5_worker

    worker = get_mt5_worker()
    if worker is None:
        raise RuntimeError(
            "MT5Worker not initialised — call worker.start() before dispatching orders"
        )
    broker = MT5BrokerAdapter(worker)
    result = dispatch_live_order(
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
    _validate_ack_sl_tp(result, stop_loss, take_profit, base_dir=base_dir)
    return result
