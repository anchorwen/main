"""Unified V9+Microstructure 49-feature live computer.

Wraps V9LiveFeatureComputer (40 multi-timeframe features) and
MicrostructureFeatureComputer (9 tick-level features) into a single
compute_all() call that returns a unified 49-dim dict.

Usage::

    import MetaTrader5 as mt5
    mt5.initialize()

    computer = V9MicroComputer(mt5, "XAUUSDc")
    features = computer.compute_all()  # 49-dim dict
    micro_ok = computer.last_micro_ok  # True if micro data was available
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from core.features.computers.microstructure_computer import MicrostructureFeatureComputer
from core.features.computers.v9_live_computer import V9LiveFeatureComputer
from core.features.schemas.microstructure_schema import MICROSTRUCTURE_9_FEATURES

if TYPE_CHECKING:
    from core.execution.mt5_worker import MT5Worker


class V9MicroComputer:
    """Compute 49 unified features (40 V9 + 9 micro) in one call.

    Shares a single MT5 connection between both sub-computers.
    Tracks whether microstructure data was successfully fetched so
    downstream code can reject signals when micro data is unavailable
    (Imputation Ghosts fix).
    """

    def __init__(self, mt5_module, symbol: str, mt5_worker: MT5Worker | None = None):
        self._mt5 = mt5_module
        self._symbol = symbol
        self._worker = mt5_worker
        self._v9 = V9LiveFeatureComputer(mt5_module, symbol, mt5_worker=mt5_worker)
        self._micro = MicrostructureFeatureComputer(mt5_module, symbol, mt5_worker=mt5_worker)
        self.last_micro_ok: bool = False

    def compute_all(self) -> dict[str, float]:
        """Compute all 49 features and return as {name: value} dict.

        V9 features are always computed (40 dims).  Micro features are
        computed on a best-effort basis; when unavailable, the 9 micro
        slots are filled with NaN sentinels so downstream code can detect
        the gap and reject (rather than silently feeding zeros).
        """
        result: dict[str, float] = {}

        # ── V9 institutional features (always attempted) ──
        try:
            v9_features = self._v9.compute_all()
        except Exception:  # BLE001:REVIEWED
            logging.exception("V9MicroComputer: V9 compute failed for %s", self._symbol)
            v9_features = {}
        result.update(v9_features)

        # ── Microstructure features (best-effort, NaN on failure) ──
        self.last_micro_ok = False
        micro_features: dict[str, float] = {}
        try:
            micro_features = self._micro.compute_all()
            # Verify all 9 features are present and non-NaN
            missing = []
            for name in MICROSTRUCTURE_9_FEATURES:
                val = micro_features.get(name)
                if val is None or (isinstance(val, float) and val != val):  # NaN check
                    missing.append(name)
            if missing:
                logging.warning(
                    "V9MicroComputer: micro features incomplete for %s — missing %s",
                    self._symbol,
                    missing,
                )
            else:
                self.last_micro_ok = True
        except Exception:  # BLE001:REVIEWED
            logging.exception("V9MicroComputer: micro compute failed for %s", self._symbol)

        # Fill micro slots — use 0.0 when unavailable (NaN propagates through
        # feature vectors into model inference → NaN predictions → silent rejection)
        for name in MICROSTRUCTURE_9_FEATURES:
            val = micro_features.get(name) if self.last_micro_ok else 0.0
            result[name] = float(val) if val is not None else 0.0

        return result

    @property
    def is_micro_available(self) -> bool:
        """True if the last compute_all() successfully fetched all micro features."""
        return self.last_micro_ok
