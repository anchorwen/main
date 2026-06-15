"""Immutable dispatch routing context — Single Source of Truth.

Eliminates the "Primitive Obsession" anti-pattern where 20+ individual kwargs
(adapter_name, zmq_order_endpoint, base_dir, ...) were threaded through
multiple layers of closures.  Any single omission caused a runtime crash
(see DQAF-20260615-010/P0-1 — live_cycle.py forgot adapter_name in
handle_net_out_close closure).

Frozen dataclass — zero serialization overhead on the hot path, thread-safe
by construction (Iron Law #4 — Decoupling Non-Degradation).

Usage::

    ctx = DispatchContext(
        adapter_name=config.adapter_name,
        base_dir=config.base_dir,
        symbol=config.symbol,
        mt5_terminal_path=config.mt5_terminal_path,
        zmq_order_endpoint=config.zmq_order_endpoint,
        zmq_ack_endpoint=config.zmq_ack_endpoint,
    )
    dispatch_live_open_order(ctx=ctx, side="long", stop_loss=..., take_profit=...)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class DispatchContext:
    """Immutable bundle of all dispatch routing parameters.

    Every function in the dispatch pipeline receives this single object
    instead of a dozen scattered kwargs.  mypy catches missing fields at
    pre-push time — no more runtime TypeErrors from incomplete closures.

    Fields:
        adapter_name: Adapter to use — "mt5" (file IPC) or "mt5_zmq" (ZMQ).
        base_dir: Per-symbol data root (e.g. "data", "data_btc").
        symbol: MT5 trading symbol (e.g. "XAUUSDc", "BTCUSDc").
        mt5_terminal_path: Path to MT5 terminal64.exe for this symbol.
        zmq_order_endpoint: ZMQ PULL endpoint for order dispatch.
        zmq_ack_endpoint: ZMQ PUB endpoint for ACK subscription.
        protection_flag_path: Path to the dispatch-block sentinel file.
        ignore_protection_flag: If True, skip protection flag check.
    """

    adapter_name: str
    base_dir: str
    symbol: str
    mt5_terminal_path: str
    zmq_order_endpoint: str = ""
    zmq_ack_endpoint: str = ""
    protection_flag_path: str = "data/live_dispatch_block.flag"
    ignore_protection_flag: bool = False


def build_dispatch_context(config: Any) -> DispatchContext:
    """Build a :class:`DispatchContext` from any config object carrying the
    standard dispatch routing fields (LiveCycleConfig, argparse.Namespace, …).

    Uses ``getattr`` with safe defaults so it works with configs that don't
    have every field (e.g. CLI args, test fixtures).  The frozen dataclass
    guarantees thread-safe immutability once built.
    """
    return DispatchContext(
        adapter_name=getattr(config, "adapter_name", "mt5"),
        base_dir=getattr(config, "base_dir", "data"),
        symbol=getattr(config, "symbol", "XAUUSDc"),
        mt5_terminal_path=getattr(config, "mt5_terminal_path", ""),
        zmq_order_endpoint=getattr(config, "zmq_order_endpoint", ""),
        zmq_ack_endpoint=getattr(config, "zmq_ack_endpoint", ""),
        protection_flag_path=getattr(config, "protection_flag_path", "data/live_dispatch_block.flag"),
        ignore_protection_flag=getattr(config, "ignore_protection_flag", False),
    )
