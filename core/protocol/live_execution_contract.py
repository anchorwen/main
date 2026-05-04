"""Canonical MT5 bridge envelope.payload extensions for live execution (Phase B).

Any signal producer (anchor loop, ONNX runner, batch jobs) SHOULD populate the same keys so
``mt5_bridge_worker`` can execute without per-model forks.

See docs/LIVE_EXECUTION_CONTRACT.md."""

from __future__ import annotations

from typing import Any

# Bump when adding required fields for downstream analytics (journal, replay).
SCHEMA_LIVE_MT5_EXECUTION_PAYLOAD_V2 = "live_mt5_execution_payload.v2"

ACTION_OPEN = "open"
ACTION_REVERSE = "reverse"
ACTION_CLOSE = "close"
ACTION_MODIFY_SLTP = "modify_sltp"


def normalize_action(raw: Any) -> str:
    """Default missing action to ``open`` for backward compatibility with older handoffs."""
    text = str(raw or "").strip().lower()
    if not text:
        return ACTION_OPEN
    return text


def effective_volume(msg_payload: dict[str, Any], *, default_volume: float) -> float:
    """Use explicit positive ``volume`` / ``lots`` from payload when present;
    else bridge default."""
    for key in ("volume", "lots"):
        v = msg_payload.get(key)
        if v is None:
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f > 0:
            return f
    return float(default_volume)


def coerce_position_ticket(msg_payload: dict[str, Any]) -> int | None:
    for key in ("position_ticket", "ticket", "position_id"):
        raw = msg_payload.get(key)
        if raw is None:
            continue
        try:
            tid = int(raw)
        except (TypeError, ValueError):
            continue
        if tid > 0:
            return tid
    return None


def execution_route(action_norm: str) -> str:
    """Route bucket for bridge: market_open | close | modify_sltp | unsupported."""
    if action_norm in {ACTION_OPEN, ACTION_REVERSE}:
        return "market_open"
    if action_norm == ACTION_CLOSE:
        return "close"
    if action_norm in {ACTION_MODIFY_SLTP, "modify"}:
        return "modify_sltp"
    return "unsupported"


def attach_schema_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    """Ensure execution_payload_schema marker for Phase C lineage tracking."""
    out = dict(payload)
    out.setdefault("execution_payload_schema", SCHEMA_LIVE_MT5_EXECUTION_PAYLOAD_V2)
    return out
