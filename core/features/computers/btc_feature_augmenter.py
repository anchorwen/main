"""BTC Feature Augmenter — post-process XAU-centric feature arrays into BTC-corrected 37-dim vectors.

Part of Phase 5b Step B: BTC Live Feature Pipeline (FIX-20260606-134).

Architecture (Strangler Fig pattern):
    Does NOT modify existing computers.  Operates as a pure post-processing
    layer between existing computers and the feature assembler.  XAU pipeline
    is frozen and untouched.

Phase 1 / M1 (FIX-20260803-XXX, BTC 机构级训练管线重建):
    State-stripped pure assembly.  The 41-dim assembly is now a module-level
    PURE FUNCTION (``_assemble_41`` / ``assemble_41_vector`` / ``assemble_41_series``)
    shared by BOTH live inference (``BTCFeatureAugmenter.augment()``) and
    historical feature replay (``core/training/feature_replay.py``).  The
    only remaining instance state is the debounced warning counters and the
    live-only prev-OU/Hurst tracking used by ``augment()`` for consecutive-bar
    regime derivatives.  Historical replay computes prev from the bar sequence
    and flows through the SAME pure function — bit-identical output given
    identical components (test_feature_bit_identical.py).

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
from typing import TYPE_CHECKING, Any

import numpy as np

if TYPE_CHECKING:
    from core.execution.mt5_worker import MT5Worker

_log = logging.getLogger(__name__)

# ── Safeguard constants ──
_WARNING_DEBOUNCE_CYCLES = 100  # log at most once per 100 calls per error type


# ═══════════════════════════════════════════════════════════════════════════
# Cross-asset access helper (DQAF-20260804-002/003, single convergent point).
#
# DQAF-20260804-003 (IC 终局裁决): MT5 is the SINGLE SOURCE OF TRUTH for all
# cross-asset prices.  AUDJPYc_return [30], XAUUSDc_return [12] and the
# BTC/XAU ratio [39-40] ALL read MT5 directly through ``_bar_close``.  The
# earlier feature-store read path (``_latest_cross_record`` / ``_coerce_feature_store``)
# was REMOVED: its sporadic 4-6h cross-symbol feed left slot [12] zero-filled
# ~always behind the 5-min staleness guard, while the sibling slots (MT5
# direct) were live and continuous.  numpy.void rows have no ``.get()``
# (DQAF-20260804-002), so all row access converges on ``_bar_close``.
# ═══════════════════════════════════════════════════════════════════════════


def _bar_close(rates: Any, idx: int) -> float:
    """Read the ``close`` price of a rate bar — dict row OR numpy.void row.

    MT5 ``copy_rates_from_pos`` returns a numpy structured array whose rows are
    ``numpy.void`` — ``row.get()`` raises AttributeError.  Single convergent
    point for cross-asset rate reads; missing/invalid rows return 0.0.
    """
    if rates is None or len(rates) == 0:
        return 0.0
    if idx < 0:
        idx = len(rates) + idx
    if idx < 0 or idx >= len(rates):
        return 0.0
    row = rates[idx]
    try:
        raw = row.get("close", 0.0) if isinstance(row, dict) else row["close"]
    except (KeyError, IndexError, TypeError):
        return 0.0
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return 0.0
    return value if math.isfinite(value) else 0.0


# ═══════════════════════════════════════════════════════════════════════════
# Phase 1 / M1 (FIX-20260803-XXX): Pure-function assembly — THE shared code path.
#
# Live inference (BTCFeatureAugmenter.augment) and historical replay
# (core/training/feature_replay.py) BOTH flow through assemble_41_vector /
# assemble_41_series.  The only difference between them is WHO provides the
# cross-asset values and HOW prev-OU/Hurst is sourced:
#   - live:    cross-assets from feature store / MT5;  prev from instance state
#   - replay:  cross-assets from historical aligned CSV;  prev from bar sequence
# ═══════════════════════════════════════════════════════════════════════════


def _coerce_components(
    daily_arr_24: object,
    micro_arr_9: object,
) -> tuple[np.ndarray, np.ndarray]:
    """Coerce + pad daily/micro into canonical (24,) / (9,) float64 arrays.

    FIX-20260628-059 / DQAF-059: np.asarray(None, dtype=np.float64) produces
    array(nan) — a scalar NaN that propagates through padding into the output
    vector.  Defend with explicit None→zeros conversion BEFORE asarray().
    """
    if daily_arr_24 is None:
        daily = np.zeros(24, dtype=np.float64)
    else:
        daily = np.asarray(daily_arr_24, dtype=np.float64).ravel()
    if micro_arr_9 is None:
        micro = np.zeros(9, dtype=np.float64)
    else:
        micro = np.asarray(micro_arr_9, dtype=np.float64).ravel()

    if len(daily) < 24:
        daily = np.pad(daily, (0, 24 - len(daily)))
    if len(micro) < 9:
        micro = np.pad(micro, (0, 9 - len(micro)))

    return daily, micro


def _assemble_41(
    daily_arr_24: object,
    micro_arr_9: object,
    *,
    xau_return: float = 0.0,
    audjpy_return: float = 0.0,
    btc_xau_ratio: float = 0.0,
    btc_xau_ratio_roc: float = 0.0,
    tf_ou: float = 0.0,
    tf_hurst: float = 0.5,
    prev_ou: float | None = None,
    prev_hurst: float | None = None,
) -> np.ndarray:
    """Assemble the RAW 41-dim BTC feature vector (pre-sanitization).

    Pure: output depends ONLY on its arguments.  The stateful regime
    derivatives (TF_delta_OU / TF_delta_Hurst) are computed from explicit
    *prev_ou* / *prev_hurst* inputs — callers decide where those come from
    (live: instance state; replay: previous bar in the sequence).

    Feature layout (Schema Order B — matches btc_macro_enhanced_41_v2):
        [0-11]   daily_arr[0:12]      (daily_swing_24 layout)
        [12]     XAUUSDc_return       (computed, or zero-filled)
        [13-23]  daily_arr[13:24]     (daily_swing_24 layout)
        [24-29]  micro_arr[0:6]       (microstructure_9 layout)
        [30]     AUDJPYc_return       (from MT5, or zero-filled)
        [31-32]  micro_arr[7:9]       (microstructure_9 layout)
        [33-34]  tf_ou, tf_hurst
        [35]     TF_delta_OU       = tf_ou - prev_ou (regime acceleration)
        [36]     TF_delta_Hurst    = tf_hurst - prev_hurst (regime velocity)
        [37]     TF_OU_x_Hurst     = tf_ou * (1-tf_hurst) (mean-reversion signal)
        [38]     TF_OU_div_ADX     = tf_ou / max(ADX,1) (regime vs trend)
        [39]     Cross_BTC_Gold_Ratio (computed, or zero-filled)
        [40]     Cross_BTC_Gold_Ratio_ROC (computed, or zero-filled)
    """
    daily, micro = _coerce_components(daily_arr_24, micro_arr_9)

    # ── FIX-B3-feat: Regime derivatives — state now an explicit input ──
    delta_ou = tf_ou - prev_ou if prev_ou is not None else 0.0
    delta_hurst = tf_hurst - prev_hurst if prev_hurst is not None else 0.0
    ou_x_hurst = tf_ou * (1.0 - tf_hurst)
    ou_div_adx = tf_ou / max(float(daily[7]), 1.0)  # D1_ADX_14 at slot 7

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
    fv[34] = float(tf_hurst) if (tf_hurst is not None and math.isfinite(float(tf_hurst))) else 0.5

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

    return fv


def _sanitize_41(fv: np.ndarray) -> np.ndarray:
    """Shape-guard + NaN/Inf → zero (fail-open with audit trail, not crash).

    FIX-20260628-059 / DQAF-059: an assertion failure in a trading pipeline is
    a system crash; NaN in a feature slot is a data quality issue that should
    be sanitized, not escalated to crash.  Pure — no logging (callers own the
    debounced warning counters).
    """
    if fv.shape != (41,):
        if len(fv) < 41:
            fv = np.pad(fv, (0, 41 - len(fv)))
        else:
            fv = fv[:41]
    return np.nan_to_num(fv, nan=0.0, posinf=0.0, neginf=0.0, copy=False)


def assemble_41_vector(
    daily_arr_24: object,
    micro_arr_9: object,
    *,
    xau_return: float = 0.0,
    audjpy_return: float = 0.0,
    btc_xau_ratio: float = 0.0,
    btc_xau_ratio_roc: float = 0.0,
    tf_ou: float = 0.0,
    tf_hurst: float = 0.5,
    prev_ou: float | None = None,
    prev_hurst: float | None = None,
) -> np.ndarray:
    """Public pure API: assemble + sanitize ONE 41-dim BTC feature vector.

    This is the ONLY assembly path used by both live inference and replay.
    """
    return _sanitize_41(
        _assemble_41(
            daily_arr_24,
            micro_arr_9,
            xau_return=xau_return,
            audjpy_return=audjpy_return,
            btc_xau_ratio=btc_xau_ratio,
            btc_xau_ratio_roc=btc_xau_ratio_roc,
            tf_ou=tf_ou,
            tf_hurst=tf_hurst,
            prev_ou=prev_ou,
            prev_hurst=prev_hurst,
        )
    )


def _coerce_matrix(series: object, n: int, dim: int, name: str) -> np.ndarray:
    """Coerce a per-bar series into a (n, dim) float64 matrix.

    Accepts: None → zeros, (n, dim) ndarray, list of per-bar arrays, or a flat
    (n*dim,) ndarray (reshaped).  Short trailing dims are zero-padded.
    """
    if series is None:
        return np.zeros((n, dim), dtype=np.float64)
    arr = np.asarray(series, dtype=np.float64)
    if arr.ndim == 1:
        if arr.shape[0] == n * dim:
            arr = arr.reshape(n, dim)
        else:
            raise ValueError(f"{name}: expected (n,{dim}) series, got flat len={arr.shape[0]}")
    if arr.shape[0] != n:
        raise ValueError(f"{name}: expected {n} bars, got {arr.shape[0]}")
    out = np.zeros((n, dim), dtype=np.float64)
    k = min(arr.shape[1], dim)
    out[:, :k] = arr[:, :k]
    return out


def _coerce_vec(series: object, n: int, default: float = 0.0) -> np.ndarray:
    """Coerce a scalar-per-bar series into a (n,) float64 array."""
    if series is None:
        return np.full(n, default, dtype=np.float64)
    arr = np.asarray(series, dtype=np.float64).ravel()
    if len(arr) < n:
        arr = np.pad(arr, (0, n - len(arr)))
    return arr[:n]


def assemble_41_series(
    daily_series: Any,
    micro_series: Any,
    *,
    xau_return_series: Any = None,
    audjpy_return_series: Any = None,
    btc_xau_ratio_series: Any = None,
    btc_xau_ratio_roc_series: Any = None,
    tf_ou_series: Any = None,
    tf_hurst_series: Any = None,
    initial_prev_ou: float | None = None,
    initial_prev_hurst: float | None = None,
) -> np.ndarray:
    """Assemble an (n, 41) BTC feature matrix over a bar sequence — pure.

    Regime derivatives (TF_delta_OU / TF_delta_Hurst) are computed from the
    bar sequence: prev_ou[i] = tf_ou[i-1] (consecutive bars).  Bar 0 uses
    *initial_prev_ou* / *initial_prev_hurst* (default None = cold start →
    delta 0, matching a fresh live augmenter's first call).

    Bit-identical to calling ``augment()`` N times in a row with the same
    components — this is the replay path's guarantee.
    """
    n = len(daily_series)
    daily = _coerce_matrix(daily_series, n, 24, "daily_series")
    micro = _coerce_matrix(micro_series, n, 9, "micro_series")
    xau = _coerce_vec(xau_return_series, n)
    audjpy = _coerce_vec(audjpy_return_series, n)
    ratio = _coerce_vec(btc_xau_ratio_series, n)
    roc = _coerce_vec(btc_xau_ratio_roc_series, n)
    ou = _coerce_vec(tf_ou_series, n)
    hurst = _coerce_vec(tf_hurst_series, n, default=0.5)

    out = np.zeros((n, 41), dtype=np.float64)
    for i in range(n):
        prev_ou: float | None = initial_prev_ou if i == 0 else float(ou[i - 1])
        prev_hurst: float | None = initial_prev_hurst if i == 0 else float(hurst[i - 1])
        out[i] = assemble_41_vector(
            daily[i],
            micro[i],
            xau_return=float(xau[i]),
            audjpy_return=float(audjpy[i]),
            btc_xau_ratio=float(ratio[i]),
            btc_xau_ratio_roc=float(roc[i]),
            tf_ou=float(ou[i]),
            tf_hurst=float(hurst[i]),
            prev_ou=prev_ou,
            prev_hurst=prev_hurst,
        )
    return out


class BTCFeatureAugmenter:
    """Correct BTC feature slots that differ from the XAU-centric pipeline.

    The existing DailyFeatureComputer and MicrostructureFeatureComputer
    produce XAU-specific features at certain indices.  For BTC, these
    slots need different cross-asset values:

        [12] XAUUSDc_return     (replaces Cross_Gold_Silver_Ratio)
        [30] AUDJPYc_return     (replaces XAGUSDc_return)
        [39] Cross_BTC_Gold_Ratio
        [40] Cross_BTC_Gold_Ratio_ROC

    Usage::

        augmenter = BTCFeatureAugmenter(mt5_worker=worker)
        fv_41 = augmenter.augment(
            daily_arr_24, micro_arr_9, btc_price,
            tf_ou=theta, tf_hurst=hurst,
        )
        # replay / batch:
        fv_mat = augmenter.augment_series(daily_series, micro_series, ...)

    Phase 1 / M1 (FIX-20260803-XXX): ``augment()`` now delegates to the pure
    module-level ``_assemble_41`` / ``_sanitize_41`` — the same code path used
    by historical replay (``assemble_41_series``).  The only instance state is
    debounced warning counters + prev-OU/Hurst tracking for live consecutive
    calls.
    """

    def __init__(
        self,
        mt5_worker: MT5Worker | None = None,
    ):
        """Initialise the augmenter.

        Args:
            mt5_worker: MT5Worker serving AUDJPYc / XAUUSDc tick data
                        (DQAF-20260804-003 SSOT).  Can be None (all
                        cross-features zero-filled).
        """
        self._worker = mt5_worker

        # ── Debounced warning counters ──
        self._xau_fail_count = 0
        self._audjpy_fail_count = 0
        self._xau_price_fail_count = 0  # FIX-138: XAU price fetch for BTC/XAU ratio
        self._nan_fail_count = 0  # FIX-20260628-059: NaN/Inf sanitization counter
        self._first_augment_logged = False  # one-shot confirmation on first success

        # ── FIX-20260614-B3-feat: Stateful regime derivative tracking ──
        # LIVE-ONLY.  The replay path (assemble_41_series) computes prev from
        # the bar sequence instead; this state is untouched there.
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

        Phase 1 / M1: delegates to the shared pure assembly.  Cross-asset
        values are fetched live from MT5 (DQAF-20260804-003 SSOT); prev-OU/Hurst
        comes from instance state (consecutive live calls).
        """
        # ── Cross-asset features (live sources) ──
        xau_return = self._compute_xauusdc_return()
        audjpy_return = self._compute_audjpyc_return()
        btc_xau_ratio, btc_xau_ratio_roc = self._compute_btc_xau_ratio(btc_price)

        # ── Shared pure assembly (same code path as replay) ──
        fv_raw = _assemble_41(
            daily_arr_24,
            micro_arr_9,
            xau_return=xau_return,
            audjpy_return=audjpy_return,
            btc_xau_ratio=btc_xau_ratio,
            btc_xau_ratio_roc=btc_xau_ratio_roc,
            tf_ou=tf_ou,
            tf_hurst=tf_hurst,
            prev_ou=self._prev_ou,
            prev_hurst=self._prev_hurst,
        )
        # Update state for next bar (LIVE-ONLY tracking)
        self._prev_ou = tf_ou
        self._prev_hurst = tf_hurst

        # ── FIX-20260628-059 / DQAF-059: Sanitize, don't crash ──
        # Debounced warning on NaN/Inf BEFORE sanitization (audit trail).
        _nan_mask = np.isnan(fv_raw)
        _inf_mask = np.isinf(fv_raw)
        _bad_count = int(np.sum(_nan_mask)) + int(np.sum(_inf_mask))
        if _bad_count > 0:
            _nan_slots = np.where(_nan_mask)[0].tolist()
            _inf_slots = np.where(_inf_mask)[0].tolist()
            self._nan_fail_count += 1
            if self._nan_fail_count % _WARNING_DEBOUNCE_CYCLES == 0:
                _log.warning(
                    "BTCFeatureAugmenter: sanitized %d NaN/Inf values in output vector "
                    "(NaN slots=%s, Inf slots=%s). Zero-filling. (occurred %d times)",
                    _bad_count,
                    _nan_slots,
                    _inf_slots,
                    self._nan_fail_count,
                )

        fv = _sanitize_41(fv_raw)

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

    def augment_series(
        self,
        daily_series: object,
        micro_series: object,
        *,
        xau_return_series: object = None,
        audjpy_return_series: object = None,
        btc_xau_ratio_series: object = None,
        btc_xau_ratio_roc_series: object = None,
        tf_ou_series: object = None,
        tf_hurst_series: object = None,
        initial_prev_ou: float | None = None,
        initial_prev_hurst: float | None = None,
    ) -> np.ndarray:
        """Batch-assemble an (n, 41) BTC feature matrix (replay path).

        Delegates to the pure ``assemble_41_series`` — bit-identical to
        calling ``augment()`` N times in a row for the same components.
        Does NOT touch instance prev-OU/Hurst state (batch/replay usage).
        Cross-asset series are provided by the caller (historical data for
        replay; live fetch is per-bar via ``augment()``).
        """
        return assemble_41_series(
            daily_series,
            micro_series,
            xau_return_series=xau_return_series,
            audjpy_return_series=audjpy_return_series,
            btc_xau_ratio_series=btc_xau_ratio_series,
            btc_xau_ratio_roc_series=btc_xau_ratio_roc_series,
            tf_ou_series=tf_ou_series,
            tf_hurst_series=tf_hurst_series,
            initial_prev_ou=initial_prev_ou,
            initial_prev_hurst=initial_prev_hurst,
        )

    # ── Private helpers ─────────────────────────────────────────────────

    def _compute_xauusdc_return(self) -> float:
        """Compute XAUUSDc return from MT5 tick data (DQAF-20260804-003).

        Single Source of Truth: MT5 is the only authoritative live price source.
        Previously this read the feature store cross-symbol records; that feed
        was sporadic (4-6h), so the 5-min staleness guard zero-filled slot [12]
        almost always.  Now mirrors ``_compute_audjpyc_return`` (slot [30]):
        ``copy_rates_from_pos`` + ``_bar_close`` — consistent, continuous, live.
        """
        if self._worker is None:
            return 0.0

        try:
            rates = self._worker.copy_rates_from_pos(
                "XAUUSDc",
                5,
                0,
                2,
                timeout=3.0,  # M5, 2 bars
            )
            if rates is None or len(rates) < 2:
                self._xau_fail_count += 1
                if self._xau_fail_count % _WARNING_DEBOUNCE_CYCLES == 0:
                    _log.warning(
                        "BTCFeatureAugmenter: XAUUSDc rates unavailable "
                        "(symbol may not be in Market Watch). "
                        "Zero-filling XAUUSDc_return. (failed %d times)",
                        self._xau_fail_count,
                    )
                return 0.0

            # DQAF-20260804-003: convergent numpy.void-safe row read.
            prev_close = _bar_close(rates, -2)
            curr_close = _bar_close(rates, -1)
            if prev_close <= 0:
                return 0.0
            ret = (curr_close - prev_close) / prev_close
            return ret if math.isfinite(ret) else 0.0

        except Exception as exc:  # BLE001:FOG
            self._xau_fail_count += 1
            if self._xau_fail_count % _WARNING_DEBOUNCE_CYCLES == 0:
                _log.error(
                    "BTCFeatureAugmenter: failed to fetch XAUUSDc: %s. "
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

            # DQAF-20260804-002: numpy.void rows have no .get() — use helper.
            prev_close = _bar_close(rates, -2)
            curr_close = _bar_close(rates, -1)
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
                        "Zero-filling BTC/XAU ratio slots [39-40]. "
                        "(failed %d times)",
                        self._xau_price_fail_count,
                    )
                return (0.0, 0.0)

            # DQAF-20260804-002: numpy.void rows have no .get() — use helper.
            xau_close = _bar_close(rates, -1)
            xau_prev_close = _bar_close(rates, -2)

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
                    "BTC/XAU ratio: %s. Zero-filling slots [39-40]. "
                    "(failed %d times)",
                    exc,
                    self._xau_price_fail_count,
                )
            return (0.0, 0.0)
