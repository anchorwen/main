"""BTC Feature Augmenter — post-process XAU-centric feature arrays into BTC-corrected 37-dim vectors.

Part of Phase 5b Step B: BTC Live Feature Pipeline (FIX-20260606-134).

Architecture (Strangler Fig pattern):
    Does NOT modify existing computers.  Operates as a pure post-processing
    layer between existing computers and the feature assembler.  XAU pipeline
    is frozen and untouched.

Data sources (resolved at init, gracefully degraded at runtime):
    - XAUUSDc prices → feature store (already available in BTC pipeline)
    - AUDJPYc prices  → MT5 worker (optional, zero-fill if unavailable)

Safeguards:
    1. Time-alignment: stale XAU/AUDJPY data (>300s) → zero-fill to prevent
       fake volatility from timestamp drift.
    2. Graceful degradation: symbol-not-found, MT5 errors → debounced warning
       + zero-fill.  Never crashes the trading cycle.
    3. Post-assertion: (37,) shape + NaN-free guard before return.
"""

from __future__ import annotations

import logging
import math
import time as _time
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from core.execution.mt5_worker import MT5Worker

_log = logging.getLogger(__name__)

# ── Safeguard constants ──
_MAX_STALENESS_SECONDS = 300  # 5 min — data older than this is untrustworthy
_WARNING_DEBOUNCE_CYCLES = 100  # log at most once per 100 calls per error type


class BTCFeatureAugmenter:
    """Correct BTC feature slots that differ from the XAU-centric pipeline.

    The existing DailyFeatureComputer and MicrostructureFeatureComputer
    produce XAU-specific features at certain indices.  For BTC, these
    slots need different cross-asset values:

        [12] XAUUSDc_return     (replaces Cross_Gold_Silver_Ratio)
        [30] AUDJPYc_return     (replaces XAGUSDc_return)
        [35] Cross_BTC_Gold_Ratio
        [36] Cross_BTC_Gold_Ratio_ROC

    Usage::

        augmenter = BTCFeatureAugmenter(feature_store, mt5_worker=worker)
        fv_37 = augmenter.augment(
            daily_arr_24, micro_arr_9, btc_price,
            tf_ou=theta, tf_hurst=hurst,
        )
    """

    def __init__(
        self,
        feature_store=None,
        mt5_worker: MT5Worker | None = None,
    ):
        """Initialise the augmenter.

        Args:
            feature_store: LocalFeatureStore instance for reading XAUUSDc
                           records.  Can be None (all cross-features zero-filled).
            mt5_worker: MT5Worker for AUDJPYc tick data.  Can be None
                        (AUDJPYc_return zero-filled).
        """
        self._store = feature_store
        self._worker = mt5_worker

        # ── Debounced warning counters ──
        self._xau_fail_count = 0
        self._xau_stale_count = 0
        self._audjpy_fail_count = 0
        self._xau_price_fail_count = 0  # FIX-138: XAU price fetch for BTC/XAU ratio
        self._first_augment_logged = False  # one-shot confirmation on first success

        # ── FIX-20260614-B3-feat: Stateful regime derivative tracking ──
        self._prev_ou: float | None = None
        self._prev_hurst: float | None = None

    # ── Public API ──────────────────────────────────────────────────────

    def augment(
        self,
        daily_arr_24: np.ndarray,
        micro_arr_9: np.ndarray,
        btc_price: float,
        *,
        tf_ou: float = 0.0,
        tf_hurst: float = 0.5,
    ) -> np.ndarray:
        """Build corrected 41-dim BTC feature vector (FIX-B3: 37→41).

        Args:
            daily_arr_24: 24-dim from DailyFeatureComputer.
            micro_arr_9:  9-dim from MicrostructureFeatureComputer.
            btc_price:    Current BTC mid price (for ratio computation).
            tf_ou:        OU theta value.
            tf_hurst:     Hurst exponent value.

        Returns:
            np.ndarray of shape (41,) with corrected BTC feature slots.

        Feature layout (training order):
            [0-11]   daily_arr[0:12]
            [12]     XAUUSDc_return (computed, or zero-filled)
            [13-23]  daily_arr[13:24]
            [24-29]  micro_arr[0:6]
            [30]     AUDJPYc_return (from MT5, or zero-filled)
            [31-32]  micro_arr[7:9]
            [33-34]  tf_ou, tf_hurst
            [35]     Cross_BTC_Gold_Ratio (computed, or zero-filled)
            [36]     Cross_BTC_Gold_Ratio_ROC (computed, or zero-filled)
            [37]     TF_delta_OU       = tf_ou - prev_ou (regime acceleration)
            [38]     TF_delta_Hurst    = tf_hurst - prev_hurst (regime velocity)
            [39]     TF_OU_x_Hurst     = tf_ou * (1-tf_hurst) (mean-reversion signal)
            [40]     TF_OU_div_ADX     = tf_ou / max(ADX,1) (regime vs trend)
        """
        daily = np.asarray(daily_arr_24, dtype=np.float64).ravel()
        micro = np.asarray(micro_arr_9, dtype=np.float64).ravel()

        if len(daily) < 24:
            daily = np.pad(daily, (0, 24 - len(daily)))
        if len(micro) < 9:
            micro = np.pad(micro, (0, 9 - len(micro)))

        # ── Cross-asset features ──
        xau_return = self._compute_xauusdc_return()
        audjpy_return = self._compute_audjpyc_return()
        btc_xau_ratio, btc_xau_ratio_roc = self._compute_btc_xau_ratio(btc_price)

        # ── FIX-B3-feat: Stateful regime derivatives ──
        delta_ou = tf_ou - self._prev_ou if self._prev_ou is not None else 0.0
        delta_hurst = tf_hurst - self._prev_hurst if self._prev_hurst is not None else 0.0
        ou_x_hurst = tf_ou * (1.0 - tf_hurst)
        ou_div_adx = tf_ou / max(float(daily[7]), 1.0)  # D1_ADX_14 at slot 7
        # Update state for next bar
        self._prev_ou = tf_ou
        self._prev_hurst = tf_hurst

        # ── Assemble 41-dim vector ──
        fv = np.zeros(41, dtype=np.float64)

        # Block 0: daily features [0-11]
        fv[0:12] = daily[0:12]

        # Slot [12]: XAUUSDc_return
        fv[12] = xau_return

        # Block 1: daily features [13-23]
        fv[13:24] = daily[13:24]

        # Block 2: micro features [24-29]
        fv[24:30] = micro[0:6]

        # Slot [30]: AUDJPYc_return
        fv[30] = audjpy_return

        # Block 3: micro features [31-32]
        fv[31:33] = micro[7:9]

        # Block 4: TF features [33-34]
        fv[33] = float(tf_ou) if (tf_ou is not None and math.isfinite(float(tf_ou))) else 0.0
        fv[34] = (
            float(tf_hurst) if (tf_hurst is not None and math.isfinite(float(tf_hurst))) else 0.5
        )

        # ── FIX-20260625-137: Regime derivative slots [35-38] ──
        # Order aligned to btc_macro_enhanced_41 Schema canonical (TF → REGIME → BTC_MACRO).
        # Previously slots 35-40 were Order C (TF → BTC_MACRO → REGIME) which caused
        # feature value swap when build_lake() zipped by position with schema names.
        fv[35] = delta_ou
        fv[36] = delta_hurst
        fv[37] = ou_x_hurst
        fv[38] = ou_div_adx

        # Slots [39-40]: BTC/XAU ratio
        fv[39] = btc_xau_ratio
        fv[40] = btc_xau_ratio_roc

        # ── Safeguard 3: Post-assertion ──
        assert fv.shape == (41,), f"CRITICAL: BTCFeatureAugmenter output shape {fv.shape} != (41,)"
        assert not np.isnan(fv).any(), "CRITICAL: NaN detected in BTC augmented feature vector"

        if not self._first_augment_logged:
            self._first_augment_logged = True
            _log.info(
                "BTCFeatureAugmenter: 41-dim pipeline activated (FIX-20260625-137 Schema Order B). "
                "Regime slots: [35]=%.4f [36]=%.4f [37]=%.4f [38]=%.4f, "
                "BTC/XAU slots: [39]=%.4f [40]=%.4f",
                fv[35],
                fv[36],
                fv[37],
                fv[38],
                fv[39],
                fv[40],
            )

        return fv

    # ── Private helpers ─────────────────────────────────────────────────

    def _compute_xauusdc_return(self) -> float:
        """Compute XAUUSDc return from feature store cross-symbol records.

        Safeguard 1 (time-alignment): if the XAU record is more than
        _MAX_STALENESS_SECONDS old, zero-fill to prevent fake volatility.
        """
        if self._store is None:
            return 0.0

        try:
            record = self._store.get_latest("XAUUSDc")
            if record is None:
                self._xau_fail_count += 1
                if self._xau_fail_count % _WARNING_DEBOUNCE_CYCLES == 0:
                    _log.warning(
                        "BTCFeatureAugmenter: XAUUSDc record not found in feature store "
                        "(failed %d times). Zero-filling XAUUSDc_return.",
                        self._xau_fail_count,
                    )
                return 0.0

            # ── Safeguard 1: time-alignment check ──
            record_time = record.get("event_time", record.get("timestamp", 0))
            if isinstance(record_time, str):
                from datetime import datetime

                try:
                    record_time = datetime.fromisoformat(record_time.replace("Z", "+00:00"))
                    record_ts = record_time.timestamp()
                except (ValueError, TypeError):
                    record_ts = 0
            else:
                record_ts = float(record_time) if record_time else 0

            now = _time.time()
            staleness = now - record_ts if record_ts > 0 else float("inf")

            if staleness > _MAX_STALENESS_SECONDS:
                self._xau_stale_count += 1
                if self._xau_stale_count % _WARNING_DEBOUNCE_CYCLES == 0:
                    _log.warning(
                        "BTCFeatureAugmenter: XAUUSDc data stale (%.0fs > %ds). "
                        "Zero-filling XAUUSDc_return and BTC/XAU ratio features. "
                        "(stale %d times)",
                        staleness,
                        _MAX_STALENESS_SECONDS,
                        self._xau_stale_count,
                    )
                return 0.0

            # Extract return from V9 features (M5_Ret_1 is the M5 bar return)
            values = record.get("values", record.get("features", {}))
            ret = float(values.get("M5_Ret_1", 0.0))
            return ret if math.isfinite(ret) else 0.0

        except Exception as exc:  # BLE001:FOG
            self._xau_fail_count += 1
            if self._xau_fail_count % _WARNING_DEBOUNCE_CYCLES == 0:
                _log.error(
                    "BTCFeatureAugmenter: failed to compute XAUUSDc_return: %s. "
                    "Zero-filling. (failed %d times)",
                    exc,
                    self._xau_fail_count,
                )
            return 0.0

    def _compute_audjpyc_return(self) -> float:
        """Compute AUDJPYc return from MT5 tick data.

        Safeguard 2 (graceful degradation): if MT5 worker is unavailable,
        AUDJPYc symbol is not in Market Watch, or any other error occurs,
        return 0.0 with debounced warning.
        """
        if self._worker is None:
            return 0.0

        try:
            rates = self._worker.copy_rates_from_pos(
                "AUDJPYc",
                5,
                0,
                2,
                timeout=3.0,  # M5, 2 bars
            )
            if rates is None or len(rates) < 2:
                self._audjpy_fail_count += 1
                if self._audjpy_fail_count % _WARNING_DEBOUNCE_CYCLES == 0:
                    _log.warning(
                        "BTCFeatureAugmenter: AUDJPYc rates unavailable "
                        "(symbol may not be in Market Watch). "
                        "Zero-filling AUDJPYc_return. (failed %d times)",
                        self._audjpy_fail_count,
                    )
                return 0.0

            prev_close = float(rates[-2].get("close", rates[-2]["close"]))
            curr_close = float(rates[-1].get("close", rates[-1]["close"]))
            if prev_close <= 0:
                return 0.0
            ret = (curr_close - prev_close) / prev_close
            return ret if math.isfinite(ret) else 0.0

        except Exception as exc:  # BLE001:FOG
            self._audjpy_fail_count += 1
            if self._audjpy_fail_count % _WARNING_DEBOUNCE_CYCLES == 0:
                _log.error(
                    "BTCFeatureAugmenter: failed to fetch AUDJPYc: %s. "
                    "Zero-filling. (failed %d times)",
                    exc,
                    self._audjpy_fail_count,
                )
            return 0.0

    def _compute_btc_xau_ratio(self, btc_price: float) -> tuple[float, float]:
        """Compute Cross_BTC_Gold_Ratio and its 1-bar ROC from MT5 XAU price.

        Ratio = BTC_price / XAU_price  (macro risk proxy: high→risk-on).
        ROC   = (ratio - ratio_prev) / ratio_prev  (1-bar momentum).

        Safeguard 2 (graceful degradation): if MT5 worker is unavailable,
        XAUUSDc not in Market Watch, or any other error occurs, zero-fill
        both slots with debounced warning.

        Uses the same ``copy_rates_from_pos`` pattern as
        ``_compute_audjpyc_return()`` (slot [30]).
        """
        if self._worker is None or btc_price <= 0:
            return (0.0, 0.0)

        try:
            rates = self._worker.copy_rates_from_pos(
                "XAUUSDc",
                5,  # MT5_TIMEFRAME_M5
                0,
                2,  # 2 bars: current + previous for ROC
                timeout=3.0,
            )
            if rates is None or len(rates) < 2:
                self._xau_price_fail_count += 1
                if self._xau_price_fail_count % _WARNING_DEBOUNCE_CYCLES == 0:
                    _log.warning(
                        "BTCFeatureAugmenter: XAUUSDc rates unavailable "
                        "(symbol may not be in Market Watch). "
                        "Zero-filling BTC/XAU ratio slots [35-36]. "
                        "(failed %d times)",
                        self._xau_price_fail_count,
                    )
                return (0.0, 0.0)

            xau_close = float(rates[-1].get("close", 0))
            xau_prev_close = float(rates[-2].get("close", 0))

            if xau_close <= 0 or xau_prev_close <= 0:
                return (0.0, 0.0)

            ratio = btc_price / xau_close
            ratio_prev = btc_price / xau_prev_close

            if ratio_prev <= 0 or not math.isfinite(ratio_prev):
                return (0.0, 0.0)

            roc = (ratio - ratio_prev) / ratio_prev

            ratio_out = ratio if math.isfinite(ratio) else 0.0
            roc_out = roc if math.isfinite(roc) else 0.0
            return (ratio_out, roc_out)

        except Exception as exc:  # BLE001:FOG
            self._xau_price_fail_count += 1
            if self._xau_price_fail_count % _WARNING_DEBOUNCE_CYCLES == 0:
                _log.error(
                    "BTCFeatureAugmenter: failed to fetch XAUUSDc for "
                    "BTC/XAU ratio: %s. Zero-filling slots [35-36]. "
                    "(failed %d times)",
                    exc,
                    self._xau_price_fail_count,
                )
            return (0.0, 0.0)
