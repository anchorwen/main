"""Global Asset Registry — single source of truth for all tradable instruments.

Architectural Defense 1 (FIX-20260531-005):
    Every physical property of a tradable asset lives here.
    Hardcoding "XAUUSDc" or "BTCUSDc" anywhere else is FORBIDDEN.
    To add a new instrument (TSLA, AAPL, etc.), add one entry below.

Usage:
    from core.config.asset_registry import ASSET_REGISTRY, AssetConfig

    cfg = ASSET_REGISTRY["BTCUSDc"]
    print(cfg.contract_size)  # 1.0
    print(cfg.min_price)      # 5000.0
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AssetConfig:
    """Immutable physical properties of a single tradable asset."""

    symbol: str
    contract_size: float
    min_price: float
    max_price: float
    pip_size: float
    digits: int  # MT5 _Digits — decimal places for price display (XAU=2, BTC=1)


# ── Registry ──────────────────────────────────────────────────────────────────

ASSET_REGISTRY: dict[str, AssetConfig] = {
    "XAUUSDc": AssetConfig(
        symbol="XAUUSDc",
        contract_size=100.0,
        min_price=1000.0,
        max_price=10000.0,  # FIX-029: data-error guard, not a market forecast — set wide enough to be permanent
        pip_size=0.01,
        digits=2,
    ),
    "BTCUSDc": AssetConfig(
        symbol="BTCUSDc",
        contract_size=1.0,
        min_price=5000.0,
        max_price=200000.0,
        pip_size=0.1,
        digits=1,
    ),
}


def get_asset(symbol: str) -> AssetConfig:
    """Look up an asset by symbol.  Raises KeyError if unregistered."""
    if symbol not in ASSET_REGISTRY:
        raise KeyError(
            f"Unknown symbol '{symbol}'. "
            f"Registered symbols: {list(ASSET_REGISTRY.keys())}. "
            f"Add it to core/config/asset_registry.py."
        )
    return ASSET_REGISTRY[symbol]


def is_registered(symbol: str) -> bool:
    """Check whether a symbol is registered (safe lookup)."""
    return symbol in ASSET_REGISTRY
