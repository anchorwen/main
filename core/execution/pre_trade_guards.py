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

    # Weekend
    if weekday == 5 and hour >= 22:
        return {
            "session_name": "weekend",
            "volume_mult": 0.0,
            "sl_expand_mult": 2.0,
            "risk_tier": "off",
        }
    if weekday == 6:
        return {
            "session_name": "weekend",
            "volume_mult": 0.0,
            "sl_expand_mult": 2.0,
            "risk_tier": "off",
        }
    # Sunday open (potentially gap, low liquidity)
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

    # Price range sanity for XAUUSD
    if symbol.startswith("XAU"):
        if bid < 1500 or bid > 3500:
            issues.append(f"bid_out_of_range:{bid}")
        if ask < 1500 or ask > 3500:
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
