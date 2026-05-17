"""Trading session detection, VaR check, and data quality guards.

Institutional-grade pre-trade checks that run every cycle before
strategy evaluation.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

# ── Session detection ──────────────────────────────────────────────────

# UTC session boundaries for XAUUSD spot
_SESSIONS = [
    ("asian_low", 0, 8, 0.70, 1.40, "reduced"),  # Asian: low liquidity
    ("london", 8, 13, 1.00, 1.00, "full"),  # London morning: best
    ("london_ny_overlap", 13, 16, 1.00, 1.00, "full"),  # Peak liquidity
    ("ny", 16, 21, 0.85, 1.10, "reduced"),  # NY afternoon: ok
    ("pre_close", 21, 22, 0.50, 1.50, "caution"),  # Pre-close Friday
    ("closed", 22, 24, 0.00, 2.00, "off"),  # After close
]


def detect_session(now_utc: datetime | None = None) -> dict[str, Any]:
    """Detect current trading session and return risk multipliers.

    Returns dict with: session_name, volume_mult, sl_expand_mult, risk_tier.
    """
    if now_utc is None:
        now_utc = datetime.now(UTC).replace(tzinfo=None)
    elif now_utc.tzinfo is not None:
        now_utc = now_utc.astimezone(UTC).replace(tzinfo=None)

    weekday = now_utc.weekday()
    hour = now_utc.hour + now_utc.minute / 60.0

    # ── Weekend: Fri 22:00 UTC → Sun ~22:00 UTC ──
    # Friday after market close
    if weekday == 4 and hour >= 22:
        return {
            "session_name": "weekend",
            "volume_mult": 0.0,
            "sl_expand_mult": 2.0,
            "risk_tier": "off",
        }
    # Saturday all day
    if weekday == 5:
        return {
            "session_name": "weekend",
            "volume_mult": 0.0,
            "sl_expand_mult": 2.0,
            "risk_tier": "off",
        }
    # Sunday (market re-opens ~22:00 UTC)
    if weekday == 6:
        if hour < 22:
            return {
                "session_name": "weekend",
                "volume_mult": 0.0,
                "sl_expand_mult": 2.0,
                "risk_tier": "off",
            }
        return {
            "session_name": "sunday_open",
            "volume_mult": 0.50,
            "sl_expand_mult": 1.50,
            "risk_tier": "caution",
        }
    # Monday early hours (market just re-opened — gap risk)
    if weekday == 0 and hour < 1:
        return {
            "session_name": "sunday_open",
            "volume_mult": 0.50,
            "sl_expand_mult": 1.50,
            "risk_tier": "caution",
        }

    # Friday afternoon — reduce exposure
    if weekday == 4 and hour >= 16:
        return {
            "session_name": "friday_afternoon",
            "volume_mult": 0.60,
            "sl_expand_mult": 1.30,
            "risk_tier": "caution",
        }

    # Regular sessions
    for name, start_h, end_h, vol_mult, sl_mult, tier in _SESSIONS:
        if start_h <= hour < end_h:
            return {
                "session_name": name,
                "volume_mult": vol_mult,
                "sl_expand_mult": sl_mult,
                "risk_tier": tier,
            }

    return {
        "session_name": "unknown",
        "volume_mult": 0.70,
        "sl_expand_mult": 1.20,
        "risk_tier": "reduced",
    }


# ── Daily close window detection (for scheduling maintenance) ───────────


def _compute_us_dst_boundaries(year: int) -> tuple[float, float]:
    """Return (dst_start_utc_hour, dst_end_utc_hour) for a given year.

    US DST: second Sunday of March 02:00 local → first Sunday of November 02:00 local.
    Returned as UTC decimal hours (e.g. 7.0 for EST→EDT transition at 07:00 UTC).

    On transition Sundays the market is closed, so edge precision doesn't matter.
    """
    from datetime import timedelta

    # Second Sunday of March (transition at 07:00 UTC from EST)
    mar1 = datetime(year, 3, 1)
    mar_second_sun = mar1 + timedelta(days=(6 - mar1.weekday()) % 7 + 7)
    dst_start = mar_second_sun.day + 7.0 / 24.0  # 07:00 UTC

    # First Sunday of November (transition at 06:00 UTC from EDT)
    nov1 = datetime(year, 11, 1)
    nov_first_sun = nov1 + timedelta(days=(6 - nov1.weekday()) % 7)
    dst_end = nov_first_sun.day + 6.0 / 24.0  # 06:00 UTC

    return dst_start, dst_end


def _is_us_dst(now_utc: datetime) -> bool:
    """Return True if US is currently observing Daylight Saving Time."""
    month = now_utc.month
    day_frac = now_utc.day + now_utc.hour / 24.0 + now_utc.minute / 1440.0
    dst_start, dst_end = _compute_us_dst_boundaries(now_utc.year)

    if month < 3 or month > 11:
        return False
    if 3 < month < 11:
        return True
    if month == 3:
        return day_frac >= dst_start
    return day_frac < dst_end  # month == 11


def _is_daily_close_window(now_utc: datetime | None = None) -> bool:
    """Check whether current UTC time falls within the XAUUSD daily close window.

    The CME daily maintenance break shifts by 1 hour between US DST/EST:
      - US DST (summer): 20:58–22:02 UTC
      - US EST (winter): 21:58–23:02 UTC

    Beijing time (+8): 04:58–06:02 (DST) / 05:58–07:02 (EST).

    Uses integer seconds arithmetic to avoid floating-point boundary errors.
    """
    if now_utc is None:
        now_utc = datetime.now(UTC).replace(tzinfo=None)
    elif now_utc.tzinfo is not None:
        now_utc = now_utc.astimezone(UTC).replace(tzinfo=None)

    total_sec = now_utc.hour * 3600 + now_utc.minute * 60 + now_utc.second

    if _is_us_dst(now_utc):
        start_sec = 20 * 3600 + 58 * 60  # 20:58:00
        end_sec = 22 * 3600 + 2 * 60  # 22:02:00
    else:
        start_sec = 21 * 3600 + 58 * 60  # 21:58:00
        end_sec = 23 * 3600 + 2 * 60  # 23:02:00

    return start_sec <= total_sec <= end_sec


# ── Pre-trade VaR check ────────────────────────────────────────────────

XAUUSD_CONTRACT_SIZE = 100.0  # 1 lot = 100 oz
XAUUSD_PIP_VALUE = 1.0  # 1 pip = $1 per 0.01 lot (approx)


def check_pre_trade_var(
    *,
    volume: float,
    atr: float,
    sl_atr_mult: float,
    account_balance: float | None,
    max_risk_pct: float = 0.02,
) -> dict[str, Any]:
    """Estimate max loss and check against account risk budget.

    Returns dict with: passed, var_absolute, var_pct, max_risk_pct.
    """
    if account_balance is None or account_balance <= 0:
        return {
            "passed": True,
            "var_absolute": 0.0,
            "var_pct": 0.0,
            "max_risk_pct": max_risk_pct,
            "skipped": True,
        }

    sl_distance = atr * sl_atr_mult
    var_absolute = volume * sl_distance * XAUUSD_CONTRACT_SIZE
    var_pct = var_absolute / account_balance

    return {
        "passed": var_pct <= max_risk_pct,
        "var_absolute": round(var_absolute, 2),
        "var_pct": round(var_pct, 4),
        "max_risk_pct": max_risk_pct,
        "sl_distance": round(sl_distance, 2),
        "volume": volume,
        "atr": round(atr, 4),
    }


# ── Vol-targeted position sizing ────────────────────────────────────────


def compute_position_size(
    *,
    risk_budget_usd: float,
    atr: float,
    sl_atr_mult: float,
    contract_size: float = 100.0,
    min_lot: float = 0.01,
    max_lot: float = 0.10,
    lot_step: float = 0.01,
) -> float:
    """Compute position size so every trade risks the same USD amount.

    Formula: position = risk_budget / (ATR × SL_mult × contract_size)

    Clamped to [min_lot, max_lot] at lot_step granularity.
    """
    import math

    if atr <= 0 or sl_atr_mult <= 0:
        return min_lot

    sl_distance = atr * sl_atr_mult
    raw = risk_budget_usd / (sl_distance * contract_size)
    raw = max(min_lot, min(max_lot, raw))
    # Use floor-round to avoid banker's rounding (2.5 → 2)
    ticks = math.floor(raw / lot_step + 0.5)
    return round(ticks * lot_step, 2)


# ── Intraday drawdown kill ─────────────────────────────────────────────


class IntradayDrawdownKill:
    """Real-time equity-based circuit breaker.

    Tracks intraday high-water mark and blocks trading when
    drawdown from peak exceeds the configured threshold.
    Reset daily — resets on the first update after midnight UTC
    (or when the reset hour is reached), so missed cycles don't
    skip the reset.

    When ``force_close_enabled``, the kill returns ``force_close=True``
    if drawdown exceeds ``force_close_pct`` (typically > kill_pct).
    The caller should close all open positions when this flag is set.
    """

    def __init__(
        self,
        *,
        kill_pct: float = 0.02,
        force_close_enabled: bool = False,
        force_close_pct: float = 0.03,
        reset_hour_utc: int = 0,
        initial_equity: float = 0.0,
    ):
        self.kill_pct = kill_pct
        self.force_close_enabled = force_close_enabled
        self.force_close_pct = force_close_pct
        self.reset_hour_utc = reset_hour_utc
        self._high_watermark = initial_equity
        self._last_reset_date: str = datetime.now(UTC).date().isoformat()

    def update(self, current_equity: float, now_utc: datetime | None = None) -> dict[str, Any]:
        """Update watermark and check kill condition.

        Returns dict with: blocked, drawdown_pct, high_watermark,
        current_equity, kill_pct, force_close.
        """
        if now_utc is None:
            now_utc = datetime.now(UTC).replace(tzinfo=None)

        today = now_utc.date().isoformat()

        # Daily reset: trigger on first update after crossing into a new day
        # (not just at exact reset hour — handles process restart)
        if today != self._last_reset_date:
            self._high_watermark = current_equity
            self._last_reset_date = today

        if current_equity > self._high_watermark:
            self._high_watermark = current_equity

        dd_pct = 0.0
        if self._high_watermark > 0:
            dd_pct = (self._high_watermark - current_equity) / self._high_watermark

        blocked = dd_pct >= self.kill_pct
        force_close = self.force_close_enabled and dd_pct >= self.force_close_pct

        return {
            "blocked": blocked,
            "force_close": force_close,
            "drawdown_pct": round(dd_pct, 6),
            "high_watermark": round(self._high_watermark, 2),
            "current_equity": round(current_equity, 2),
            "kill_pct": self.kill_pct,
            "force_close_pct": self.force_close_pct if self.force_close_enabled else 0.0,
        }


# ── Kelly criterion position sizing ─────────────────────────────────────


def compute_kelly_fraction(
    *,
    win_rate: float,
    avg_win: float,
    avg_loss: float,
    max_fraction: float = 0.25,
    use_half_kelly: bool = True,
) -> dict[str, Any]:
    """Optimal fraction of capital to risk per trade (Kelly criterion).

    f* = (bp - q) / b

    where:
      b = avg_win / avg_loss  (odds ratio — must be > 0)
      p = win_rate
      q = 1 - p

    Returns dict with: kelly_fraction, half_kelly, recommended_fraction, viable.
    Clamped to [0, max_fraction] for safety (fractional Kelly).
    """
    if avg_loss <= 0 or win_rate <= 0 or win_rate >= 1.0:
        return {
            "kelly_fraction": 0.0,
            "half_kelly": 0.0,
            "recommended_fraction": 0.0,
            "viable": False,
            "reason": "insufficient_data",
        }

    b = avg_win / avg_loss
    p = win_rate
    q = 1.0 - p

    f_star = (b * p - q) / b
    f_star = max(0.0, min(max_fraction, f_star))
    half_kelly = f_star / 2.0

    recommended = half_kelly if use_half_kelly else f_star

    return {
        "kelly_fraction": round(f_star, 6),
        "half_kelly": round(half_kelly, 6),
        "recommended_fraction": round(recommended, 6),
        "viable": f_star > 0,
        "win_rate": win_rate,
        "odds_ratio": round(b, 4),
    }


def compute_kelly_risk_budget(
    *,
    equity: float,
    win_rate: float,
    avg_win: float,
    avg_loss: float,
    max_risk_pct: float = 0.02,
) -> dict[str, Any]:
    """Compute USD risk budget from Kelly fraction × equity.

    Combines Kelly optimal fraction with a hard max_risk_pct cap.
    """
    kelly = compute_kelly_fraction(
        win_rate=win_rate,
        avg_win=avg_win,
        avg_loss=avg_loss,
        max_fraction=max_risk_pct,
        use_half_kelly=True,
    )

    recommended = kelly["recommended_fraction"]
    risk_budget = round(equity * recommended, 2) if equity > 0 else 0.0

    return {
        **kelly,
        "equity": round(equity, 2),
        "risk_budget_usd": risk_budget,
    }


# ── Data quality auto-repair ─────────────────────────────────────────────


def repair_feature_vector(feature_vector: Any) -> tuple[Any, dict[str, Any]]:
    """Repair NaN/Inf values in a feature vector before inference.

    Strategy:
      - NaN → forward-fill from preceding valid value (or 0 if leading)
      - +Inf → column median of valid values (or 0 if all Inf)
      - -Inf → column median of valid values (or 0 if all Inf)

    Returns (repaired_vector, repair_log) where repair_log records what was fixed.
    """
    import numpy as np

    fv = np.asarray(feature_vector, dtype=np.float64).ravel().copy()
    total = len(fv)
    repair_log: dict[str, Any] = {
        "total_features": total,
        "nan_filled": 0,
        "inf_filled": 0,
        "repaired": False,
    }

    if total == 0:
        return fv, repair_log

    # Forward-fill NaN
    nan_mask = np.isnan(fv)
    if np.any(nan_mask):
        repair_log["nan_filled"] = int(nan_mask.sum())
        last_valid = 0.0
        for i in range(total):
            if np.isnan(fv[i]):
                fv[i] = last_valid
            else:
                last_valid = float(fv[i])

    # Replace Inf with median of column (exclude Inf/NaN)
    inf_mask = np.isinf(fv)
    if np.any(inf_mask):
        repair_log["inf_filled"] = int(inf_mask.sum())
        finite_vals = fv[np.isfinite(fv)]
        median = float(np.median(finite_vals)) if len(finite_vals) > 0 else 0.0
        fv[inf_mask] = median

    repair_log["repaired"] = repair_log["nan_filled"] > 0 or repair_log["inf_filled"] > 0
    return fv, repair_log


def check_feature_freshness(
    feature_timestamp: float | None,
    max_age_seconds: float = 300.0,
) -> dict[str, Any]:
    """Check whether feature data is fresh enough for live inference.

    Args:
        feature_timestamp: Unix timestamp (seconds) when features were computed.
        max_age_seconds: Maximum allowed age before flagging as stale.

    Returns dict with: fresh, age_seconds, max_age_seconds.
    """
    import time

    if feature_timestamp is None or feature_timestamp <= 0:
        return {
            "fresh": False,
            "age_seconds": None,
            "max_age_seconds": max_age_seconds,
            "reason": "missing_timestamp",
        }

    now = time.time()
    age = now - feature_timestamp

    # Reject future timestamps — clock skew or misconfigured data warmer
    if age < 0:
        return {
            "fresh": False,
            "age_seconds": round(age, 3),
            "max_age_seconds": max_age_seconds,
            "reason": "future_timestamp",
        }

    return {
        "fresh": age <= max_age_seconds,
        "age_seconds": round(age, 3),
        "max_age_seconds": max_age_seconds,
    }


# ── Data quality guards ─────────────────────────────────────────────────


def check_tick_sanity(bid: float, ask: float, symbol: str = "XAUUSDc") -> dict[str, Any]:
    """Basic tick data sanity checks.

    Returns dict with: passed, spread_bps, issues[].
    """
    issues: list[str] = []

    # Zero/negative prices
    if bid <= 0 or ask <= 0:
        issues.append("zero_or_negative_price")

    # Inverted spread
    if ask < bid:
        issues.append("inverted_spread")

    # Price range sanity for XAUUSD (1000-8000, covers current ~4700 + headroom)
    if symbol.startswith("XAU"):
        if bid < 1000 or bid > 8000:
            issues.append(f"bid_out_of_range:{bid}")
        if ask < 1000 or ask > 8000:
            issues.append(f"ask_out_of_range:{ask}")

    # Excessive spread (>100 bps ≈ $1 on XAUUSD)
    spread_bps = 0.0
    if bid > 0:
        spread_bps = (ask - bid) / bid * 10000
    if spread_bps > 100:
        issues.append(f"spread_too_wide:{spread_bps:.0f}bps")

    return {
        "passed": len(issues) == 0,
        "spread_bps": round(spread_bps, 1),
        "bid": bid,
        "ask": ask,
        "issues": issues,
    }


def check_feature_vector(feature_vector: Any, max_nan_ratio: float = 0.20) -> dict[str, Any]:
    """Check feature vector for NaN/Inf/zero-vector before inference.

    Returns dict with: passed, nan_ratio, all_zero, issues[].
    """
    import numpy as np

    fv = np.asarray(feature_vector, dtype=np.float64).ravel()
    issues: list[str] = []
    total = len(fv)

    if total == 0:
        return {"passed": False, "nan_ratio": 1.0, "all_zero": True, "issues": ["empty_vector"]}

    nan_count = int(np.isnan(fv).sum())
    inf_count = int(np.isinf(fv).sum())
    nan_ratio = (nan_count + inf_count) / total

    if nan_ratio > max_nan_ratio:
        issues.append(f"nan_inf_ratio:{nan_ratio:.2f}")

    all_zero = bool(np.all(fv == 0))
    if all_zero:
        issues.append("all_zero_vector")

    # Individual feature range check (no single feature should be >50 std off)
    fv_clean = fv[~np.isnan(fv) & ~np.isinf(fv)]
    if len(fv_clean) > 0:
        std = float(np.std(fv_clean))
        if std > 0:
            max_z = float(np.max(np.abs((fv_clean - np.mean(fv_clean)) / std)))
            if max_z > 50:
                issues.append(f"extreme_outlier:max_z={max_z:.1f}")

    return {
        "passed": len(issues) == 0,
        "nan_ratio": round(nan_ratio, 4),
        "all_zero": all_zero,
        "vector_len": total,
        "issues": issues,
    }
