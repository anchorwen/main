#!/usr/bin/env python
"""End-to-end backtest of the v3.2 three-refactor architecture + three knives.

Combines:
  R1: 2D regime matrix (Hurst x RV) + Schmitt trigger hysteresis
  R2: PnL-aware Z-score exit + toxic flow stop + time deadline
  R3: Sigmoid convex bandit sizing + MVS drop-to-zero + Z depth penalty
  K1: Passive limit execution — Buy/Sell Limit at Close +/- 0.1 ATR (Maker)
  K2: Volume absorption filter — climax/contraction required at inflection
  K3: Second-leg re-entry — larger size on deeper |Z| after hard_stop W/M pattern

Usage:
  python scripts/backtest/backtest_v3_combined.py \
    --price-data data/raw/xauusdc_m5_1y.csv \
    --output data/backtest/v3_combined/
"""

from __future__ import annotations

import argparse
import csv
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

# -- Constants --

Z_LOOKBACK = 20
Z_ENTRY = 1.5
Z_EXIT = 0.3
MAX_HOLD = 8
MIN_BARS_FOR_RV = 500
SIGMOID_Z_MID = 1.75
SIGMOID_K = 4.0
MVS_THRESHOLD = 0.20
HURST_WINDOW = 60
HURST_K = 6

# Knife 1: Limit order offset (fraction of ATR)
LIMIT_OFFSET_ATR = 0.1
LIMIT_MAX_WAIT_BARS = 1  # max bars to wait for limit fill

# Knife 2: Volume climax thresholds
VOL_CLIMAX_MULT = 2.0  # volume > N × 20-bar mean
VOL_CLIMAX_WICK_RATIO = 0.5  # wick > N% of total range for absorption
VOL_CLIMAX_LOOKBACK = 20

# Knife 3: Second-leg window (bars after hard_stop to allow re-entry)
SECOND_LEG_WINDOW = 12  # 1 hour in M5


# -- Session config (UTC hours) --


def get_session_factor(hour_utc: int) -> float:
    """Volatility-parity session sizing: Asian=1.0, London=0.8, NY=0.6."""
    if 0 <= hour_utc <= 7:
        return 1.0
    elif 8 <= hour_utc <= 12:
        return 0.8
    return 0.6


def session_name(hour_utc: int) -> str:
    if 0 <= hour_utc <= 7:
        return "asian"
    elif 8 <= hour_utc <= 12:
        return "london"
    return "ny"


# -- Helpers --


def load_ohlc(path):
    opens, highs, lows, closes, tick_volumes, timestamps = [], [], [], [], [], []
    with open(path, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            opens.append(float(row.get("open", 0)))
            highs.append(float(row.get("high", 0)))
            lows.append(float(row.get("low", 0)))
            closes.append(float(row.get("close", 0)))
            tick_volumes.append(float(row.get("tick_volume", row.get("volume", 0))))
            ts = row.get("timestamp", row.get("time", row.get("datetime", "")))
            timestamps.append(ts)
    return {
        "open": np.array(opens, dtype=np.float64),
        "high": np.array(highs, dtype=np.float64),
        "low": np.array(lows, dtype=np.float64),
        "close": np.array(closes, dtype=np.float64),
        "tick_volume": np.array(tick_volumes, dtype=np.float64),
        "timestamps": timestamps,
        "n_bars": len(closes),
    }


def compute_z_score(closes, i, lookback=Z_LOOKBACK):
    if i < lookback:
        return 0.0
    window = closes[i - lookback : i]
    mean = float(np.mean(window))
    std = float(np.std(window))
    if std < 1e-10:
        return 0.0
    return float((closes[i] - mean) / std)


def compute_atr(closes, i, period=14):
    if i < period:
        return abs(closes[i]) * 0.001
    trs = [abs(closes[j] - closes[j - 1]) for j in range(i - period + 1, i + 1)]
    return float(np.mean(trs)) if trs else abs(closes[i]) * 0.001


def compute_hurst_vr(price, i, window=HURST_WINDOW, k=HURST_K):
    if i < window:
        return 0.5
    p = price[max(0, i - window + 1) : i + 1]
    n = len(p)
    if n < k + 1:
        return 0.5
    log_p = np.log(p)
    ret_1bar = np.diff(log_p)
    var_1bar = float(np.var(ret_1bar))
    if var_1bar < 1e-12:
        return 0.5
    ret_kbar = log_p[k:] - log_p[:-k]
    var_kbar = float(np.var(ret_kbar))
    if var_kbar < 1e-12:
        return 0.5
    vr = var_kbar / (k * var_1bar)
    hurst_est = 0.5 + 0.5 * (vr - 1.0) / (abs(vr - 1.0) + 1.0)
    return float(max(0.01, min(0.99, hurst_est)))


def compute_rv_percentile(rv_values, i):
    if i < MIN_BARS_FOR_RV:
        return 0.5
    window = rv_values[max(0, i - MIN_BARS_FOR_RV + 1) : i + 1]
    current = rv_values[i]
    rank = float(np.searchsorted(np.sort(window), current, side="right"))
    return rank / float(len(window))


def sigmoid_exhaustion(abs_z, z_mid=SIGMOID_Z_MID, k=SIGMOID_K):
    return 1.0 / (1.0 + math.exp(-k * (abs_z - z_mid)))


def apply_mvs(effective_mult, threshold=MVS_THRESHOLD):
    return 0.0 if effective_mult < threshold else effective_mult


def z_depth_penalty(abs_z: float, z_entry: float = Z_ENTRY, strength: float = 0.3) -> float:
    """Dynamic decay for deep Z excursions — volatility parity."""
    if abs_z <= z_entry:
        return 1.0
    return 1.0 / (1.0 + strength * (abs_z - z_entry))


def resample_to_h1(closes):
    n = len(closes) // 12
    return closes[: n * 12 : 12].copy()


# -- Knife 2: Volume Absorption Filter --


def check_volume_climax(
    tick_volumes,
    highs,
    lows,
    opens,
    closes,
    i,
    lookback=VOL_CLIMAX_LOOKBACK,
    climax_mult=VOL_CLIMAX_MULT,
    wick_ratio=VOL_CLIMAX_WICK_RATIO,
):
    """Check if inflection bar shows distinctive volume pattern.

    Returns (valid, reason).

    Two valid patterns:
      a) Volume contraction: current tick_volume < previous bar
         → selling/buying pressure exhausted (无量下跌/上涨).
      b) Volume climax + absorption wick: tick_volume > 2× 20-bar mean
         AND wick > 50% of total range → institutional passive wall absorbed.

    Normal volume at inflection = fake turnaround → skip.
    """
    if i < 1:
        return False, "vol_insufficient_data"

    vol_i = float(tick_volumes[i])
    vol_prev = float(tick_volumes[i - 1])

    # Pattern A: Volume contraction — exhaustion
    if vol_i < vol_prev:
        return True, "vol_contraction"

    # Pattern B: Volume climax + long wick — absorption
    if i >= lookback:
        mean_vol = float(np.mean(tick_volumes[i - lookback : i]))
        if mean_vol > 0 and vol_i > climax_mult * mean_vol:
            body = abs(float(closes[i]) - float(opens[i]))
            total_range = float(highs[i]) - float(lows[i])
            if total_range > 0 and body / total_range < wick_ratio:
                return True, "vol_climax_absorption"

    # Normal volume = fake inflection
    return False, "vol_normal"


# -- Regime 2D --


def classify_hurst_zone(hurst):
    if hurst < 0.4:
        return "ranging"
    elif hurst > 0.6:
        return "trending"
    return "mild"


def classify_rv_zone(rv_pct):
    if rv_pct >= 0.95:
        return "extreme"
    elif rv_pct >= 0.80:
        return "elevated"
    return "normal"


OU_REGIME_MATRIX = {
    ("ranging", "normal"): 1.0,
    ("mild", "normal"): 0.5,
    ("trending", "normal"): 0.0,
    ("ranging", "elevated"): 0.5,
    ("mild", "elevated"): 0.5,
    ("trending", "elevated"): 0.0,
    ("ranging", "extreme"): 0.0,
    ("mild", "extreme"): 0.0,
    ("trending", "extreme"): 0.0,
}


# -- Exit simulator: Toxic flow detection --


def _detect_toxic_flow_m5(opens, highs, lows, closes, bar_idx, side, atr):
    if bar_idx < 2:
        return False
    body_threshold = 0.3 * atr
    b0_open, b0_close = opens[bar_idx - 1], closes[bar_idx - 1]
    b0_high, b0_low = highs[bar_idx - 1], lows[bar_idx - 1]
    b1_open, b1_close = opens[bar_idx], closes[bar_idx]
    b1_high, b1_low = highs[bar_idx], lows[bar_idx]
    body0, body1 = abs(b0_close - b0_open), abs(b1_close - b1_open)
    if side == "short":
        if b0_close <= b0_open or body0 < body_threshold:
            return False
        if b1_close <= b1_open or body1 < body_threshold:
            return False
        return b1_high > b0_high and b1_low < b0_low
    else:
        if b0_close >= b0_open or body0 < body_threshold:
            return False
        if b1_close >= b1_open or body1 < body_threshold:
            return False
        return b1_high > b0_high and b1_low < b0_low


# -- Knife 1 + Exit simulator: Limit-order-aware trade simulation --


def simulate_trade_v3_limit(
    closes,
    highs,
    lows,
    opens,
    signal_bar,
    direction,
    entry_atr,
    entry_z,
    ou_regime_factor,
    exhaustion_factor,
    session_factor=1.0,
    depth_penalty=1.0,
    second_leg=False,
    z_exit=Z_EXIT,
    max_hold=MAX_HOLD,
    z_lookback=Z_LOOKBACK,
    limit_offset_atr=LIMIT_OFFSET_ATR,
    limit_max_wait=LIMIT_MAX_WAIT_BARS,
):
    """Simulate one trade with limit-order entry (K1) + all v3.2 features.

    K1 (Passive Limit Execution):
      Instead of market-order at signal bar Close, place a limit order:
        Long:  Buy Limit  at Close - 0.1 ATR (wait for dip)
        Short: Sell Limit at Close + 0.1 ATR (wait for pop)
      If the limit fills within `limit_max_wait` bars → entry at limit price.
      If not filled → missed trade (0 PnL, no cost).

    Opt1: Pessimistic H/L proxy for hard_stop.
    Opt3: Bleed stop — 3 consecutive negative bars → cut.
    K3:  Second-leg flag — tracked for analysis (sizing already deeper).
    """
    signal_close = closes[signal_bar]
    n_bars = len(closes)

    # Determine limit price
    if direction == 1:
        limit_price = signal_close - limit_offset_atr * entry_atr
    else:
        limit_price = signal_close + limit_offset_atr * entry_atr

    # Wait for limit fill
    fill_bar = -1
    for w in range(1, limit_max_wait + 1):
        check_bar = signal_bar + w
        if check_bar >= n_bars:
            break
        if direction == 1:
            if lows[check_bar] <= limit_price:
                fill_bar = check_bar
                break
        else:
            if highs[check_bar] >= limit_price:
                fill_bar = check_bar
                break

    if fill_bar < 0:
        # Limit never filled → missed trade, zero cost
        miss_tag = "missed_limit" if not second_leg else "missed_limit_2nd"
        return 0.0, 0, miss_tag, 0.0

    # Filled at limit price (better than market Close)
    entry_price = limit_price
    effective_mult = apply_mvs(
        ou_regime_factor * exhaustion_factor * session_factor * depth_penalty
    )
    end = min(fill_bar + max_hold, n_bars - 1)

    pnls_by_bar = []

    for j in range(fill_bar + 1, end + 1):
        z = compute_z_score(closes, j, z_lookback)
        bars_in = j - fill_bar

        if direction == 1:
            close_pnl = (closes[j] - entry_price) / entry_atr
            pessimistic_pnl = (lows[j] - entry_price) / entry_atr
        else:
            close_pnl = (entry_price - closes[j]) / entry_atr
            pessimistic_pnl = (entry_price - highs[j]) / entry_atr

        # Opt1: Pessimistic hard_stop — H/L proxy, capped at -2.3R
        if pessimistic_pnl <= -2.0:
            realized = max(pessimistic_pnl, -2.3)
            tag = "hard_stop_2nd" if second_leg else "hard_stop"
            return realized, bars_in, tag, effective_mult

        # Opt3: Bleed stop — 3 consecutive negative bars
        pnls_by_bar.append(close_pnl)
        if bars_in == 3 and all(p < 0 for p in pnls_by_bar):
            tag = "bleed_stop_2nd" if second_leg else "bleed_stop"
            return close_pnl, bars_in, tag, effective_mult

        # Profitable reversion
        if abs(z) < z_exit and close_pnl > 0:
            tag = "profit_revert_2nd" if second_leg else "profit_revert"
            return close_pnl, bars_in, tag, effective_mult

        # Toxic flow (bars 6+)
        if bars_in >= 6 and bars_in < max_hold:
            if _detect_toxic_flow_m5(opens, highs, lows, closes, j, direction, entry_atr):
                tag = "toxic_flow_2nd" if second_leg else "toxic_flow"
                return close_pnl, bars_in, tag, effective_mult

    # Deadline exit
    final = closes[end]
    if direction == 1:
        pnl = (final - entry_price) / entry_atr
    else:
        pnl = (entry_price - final) / entry_atr
    tag = "deadline_2nd" if second_leg else "deadline"
    return pnl, end - fill_bar, tag, effective_mult


# -- Backtest --


def parse_hour_utc(ts_str):
    """Parse UTC hour from a timestamp string. Returns -1 on failure."""
    if not ts_str:
        return -1
    try:
        ts_str_clean = ts_str.replace("T", " ").split(".")[0].split("+")[0].split("Z")[0]
        parts = ts_str_clean.split(" ")
        if len(parts) >= 2:
            time_part = parts[1]
            hour = int(time_part.split(":")[0])
            return hour
    except (ValueError, IndexError):
        pass
    try:
        dt = datetime.fromisoformat(ts_str.replace(" ", "T"))
        return dt.hour
    except (ValueError, TypeError):
        pass
    return -1


def run_backtest(ohlc):
    closes = ohlc["close"]
    highs = ohlc["high"]
    lows = ohlc["low"]
    opens = ohlc["open"]
    tick_volumes = ohlc["tick_volume"]
    timestamps = ohlc.get("timestamps", [])
    n = len(closes)

    # Pre-compute features
    print("  Computing Z-scores...")
    z_scores = np.array([compute_z_score(closes, i) for i in range(n)], dtype=np.float64)

    print("  Computing ATRs...")
    atrs = np.array([compute_atr(closes, i) for i in range(n)], dtype=np.float64)

    print("  Computing 12-bar RV percentiles...")
    rv_values = np.zeros(n, dtype=np.float64)
    for i in range(12, n):
        window = closes[i - 11 : i + 1]
        log_rets = np.log(window[1:] / window[:-1])
        rv_values[i] = float(np.std(log_rets))
    rv_pcts = np.array([compute_rv_percentile(rv_values, i) for i in range(n)], dtype=np.float64)

    print("  Computing H1 Hurst (VR)...")
    h1_closes = resample_to_h1(closes)
    h1_hursts = np.zeros(len(h1_closes), dtype=np.float64)
    for i in range(HURST_WINDOW, len(h1_closes)):
        h1_hursts[i] = compute_hurst_vr(h1_closes, i)
    h1_hurst_m5 = np.zeros(n, dtype=np.float64)
    for i in range(n):
        h1_idx = i // 12
        if h1_idx < len(h1_hursts):
            h1_hurst_m5[i] = h1_hursts[h1_idx]

    # Schmitt trigger
    force_off = False
    cooldown = 0
    schmitt_states = np.zeros(n, dtype=bool)
    for i in range(n):
        if rv_pcts[i] >= 0.95:
            force_off = True
            cooldown = 0
        elif force_off:
            if rv_pcts[i] < 0.80:
                cooldown += 1
                if cooldown >= 3:
                    force_off = False
                    cooldown = 0
            else:
                cooldown = 0
        schmitt_states[i] = force_off

    # Run backtest
    warmup = max(Z_LOOKBACK, MIN_BARS_FOR_RV, VOL_CLIMAX_LOOKBACK, 500)
    print(f"  Running from bar {warmup} to {n}...")

    trades = []
    pnls = []
    sized_pnls = []
    equity = [0.0]
    sized_equity = [0.0]

    # Track hard_stop trades by hour for 24h histogram
    hard_stop_hours: list[int] = []
    # Counters
    inflection_skipped = 0
    volume_skipped = 0
    total_attempted = 0

    # K3: Second-leg re-entry tracker
    last_hard_stop: dict[str, Any] | None = None  # {bar, direction, exit_bar}

    for i in range(warmup, n - MAX_HOLD - LIMIT_MAX_WAIT_BARS):
        z = z_scores[i]
        if abs(z) < Z_ENTRY:
            continue

        total_attempted += 1

        # Opt2: Z-score inflection — require z moving back toward zero
        if i > 0:
            z_prev = z_scores[i - 1]
            if z > Z_ENTRY and z >= z_prev:
                inflection_skipped += 1
                continue
            if z < -Z_ENTRY and z <= z_prev:
                inflection_skipped += 1
                continue

        # K2: Volume absorption filter — inflection must have distinctive volume
        vol_valid, vol_reason = check_volume_climax(
            tick_volumes,
            highs,
            lows,
            opens,
            closes,
            i,
        )
        if not vol_valid:
            volume_skipped += 1
            continue

        direction = -1 if z > Z_ENTRY else 1
        entry_atr = atrs[i]
        entry_z = z_scores[i]
        h1_h = h1_hurst_m5[i]
        rv_p = rv_pcts[i]
        force_off_i = schmitt_states[i]

        # R1: 2D regime matrix + Schmitt
        if force_off_i:
            ou_regime_factor = 0.0
        else:
            hurst_zone = classify_hurst_zone(h1_h)
            rv_zone = classify_rv_zone(rv_p)
            ou_regime_factor = OU_REGIME_MATRIX.get((hurst_zone, rv_zone), 0.5)

        # R3: Sigmoid exhaustion
        exhaustion_factor = sigmoid_exhaustion(abs(entry_z))

        # v3.2: Z-axis depth penalty
        _depth_pen = z_depth_penalty(abs(entry_z))

        # Opt4: Session factor
        s_factor = 1.0
        if i < len(timestamps):
            hour_utc = parse_hour_utc(timestamps[i])
            if hour_utc >= 0:
                s_factor = get_session_factor(hour_utc)

        effective_mult = apply_mvs(ou_regime_factor * exhaustion_factor * s_factor * _depth_pen)

        # K3: Second-leg detection
        is_second_leg = False
        if last_hard_stop is not None:
            bars_since_hs = i - last_hard_stop["exit_bar"]
            if bars_since_hs <= SECOND_LEG_WINDOW and direction == last_hard_stop["direction"]:
                is_second_leg = True
                last_hard_stop = None  # reset after catching the re-entry

        # K1 + R2 + Opt1 + Opt3: Simulate with limit-order entry
        pnl_r, bars_held, exit_reason, eff = simulate_trade_v3_limit(
            closes,
            highs,
            lows,
            opens,
            i,
            direction,
            entry_atr,
            entry_z,
            ou_regime_factor,
            exhaustion_factor,
            session_factor=s_factor,
            depth_penalty=_depth_pen,
            second_leg=is_second_leg,
        )

        # K3: Track hard_stop for potential second-leg
        if "hard_stop" in exit_reason and effective_mult > 0:
            exit_bar = i + 1 + bars_held  # signal_bar + limit_wait + bars_held
            last_hard_stop = {
                "bar": i,
                "direction": direction,
                "exit_bar": exit_bar,
            }
        elif "hard_stop" not in exit_reason:
            # Reset on any non-hard-stop exit
            if last_hard_stop is not None and i - last_hard_stop["exit_bar"] > SECOND_LEG_WINDOW:
                last_hard_stop = None

        # Track hard_stop hours (only for actually-traded positions)
        hs_tag = "hard_stop" in exit_reason
        if hs_tag and effective_mult > 0 and i < len(timestamps):
            hour_utc = parse_hour_utc(timestamps[i])
            if hour_utc >= 0:
                hard_stop_hours.append(hour_utc)

        # Normalize exit reason for grouping (strip _2nd suffix)
        exit_group = exit_reason.replace("_2nd", "")

        trades.append(
            {
                "bar": i,
                "direction": direction,
                "entry_z": round(entry_z, 3),
                "pnl_r": round(pnl_r, 4),
                "bars": bars_held,
                "exit": exit_reason,
                "exit_group": exit_group,
                "hurst_zone": classify_hurst_zone(h1_h),
                "rv_pct": round(rv_p, 3),
                "ou_regime_factor": ou_regime_factor,
                "exhaustion": round(exhaustion_factor, 4),
                "session_factor": s_factor,
                "depth_penalty": round(_depth_pen, 4),
                "effective_mult": round(effective_mult, 4),
                "traded": effective_mult > 0 and "missed" not in exit_reason,
                "second_leg": is_second_leg,
                "vol_reason": vol_reason,
            }
        )

        pnls.append(pnl_r)
        equity.append(equity[-1] + pnl_r)

        if effective_mult > 0 and "missed" not in exit_reason:
            sized_pnls.append(pnl_r * effective_mult)
        else:
            sized_pnls.append(0.0)
        sized_equity.append(
            sized_equity[-1]
            + (
                pnl_r * effective_mult
                if (effective_mult > 0 and "missed" not in exit_reason)
                else 0.0
            )
        )

    # -- Metrics --
    pnls_arr = np.array(pnls)
    sized_pnls_arr = np.array(sized_pnls)
    n_traded = int(np.sum([1 for t in trades if t["traded"]]))

    def compute_metrics(p, prefix=""):
        wr = float(np.mean(p > 0))
        avg = float(np.mean(p))
        std = float(np.std(p)) if len(p) > 1 else 0.0
        total = float(np.sum(p))
        sharpe = avg / std if std > 0 else 0.0
        neg = p[p < 0]
        down_std = float(np.std(neg)) if len(neg) > 1 else std
        sortino = avg / down_std if down_std > 0 else 0.0
        wins = p[p > 0]
        losses = p[p < 0]
        avg_win = float(np.mean(wins)) if len(wins) > 0 else 0.0
        avg_loss = float(np.mean(losses)) if len(losses) > 0 else 0.0
        return {
            f"{prefix}n_trades": len(p),
            f"{prefix}win_rate": round(wr, 4),
            f"{prefix}avg_pnl_r": round(avg, 6),
            f"{prefix}total_r": round(total, 4),
            f"{prefix}sharpe": round(sharpe, 4),
            f"{prefix}sortino": round(sortino, 4),
            f"{prefix}avg_win_r": round(avg_win, 4),
            f"{prefix}avg_loss_r": round(avg_loss, 4),
        }

    # Exit breakdown (by group)
    exit_breakdown: dict[str, dict[str, Any]] = {}
    for t in trades:
        key = t["exit_group"] if t["traded"] else "filtered_by_regime"
        if "missed" in t["exit"]:
            key = "missed_limit"
        if key not in exit_breakdown:
            exit_breakdown[key] = {"pnls": [], "wins": 0, "total": 0}
        exit_breakdown[key]["pnls"].append(t["pnl_r"])
        exit_breakdown[key]["total"] += 1
        if t["pnl_r"] > 0:
            exit_breakdown[key]["wins"] += 1

    exit_summary = {}
    for key, data in sorted(exit_breakdown.items()):
        p = np.array(data["pnls"])
        exit_summary[key] = {
            "n": data["total"],
            "pct": round(data["total"] / max(len(trades), 1), 4),
            "win_rate": round(data["wins"] / max(data["total"], 1), 4),
            "avg_pnl_r": round(float(np.mean(p)), 6),
            "total_r": round(float(np.sum(p)), 4),
        }

    # K3: Second-leg stats
    sl_trades = [t for t in trades if t["second_leg"]]
    sl_pnls = [t["pnl_r"] for t in sl_trades if t["traded"]]
    sl_sized_pnls = [t["pnl_r"] * t["effective_mult"] for t in sl_trades if t["traded"]]

    # 24h histogram of hard_stop trades
    hour_counts = [0] * 24
    for h in hard_stop_hours:
        if 0 <= h < 24:
            hour_counts[h] += 1

    session_hs = {"asian": 0, "london": 0, "ny": 0}
    for h in hard_stop_hours:
        session_hs[session_name(h)] += 1

    return {
        "unweighted": compute_metrics(pnls_arr),
        "bandit_sized": compute_metrics(sized_pnls_arr, "sized_"),
        "n_traded": n_traded,
        "n_filtered": len(trades) - n_traded,
        "filter_rate": round(1.0 - n_traded / max(len(trades), 1), 4),
        "exit_breakdown": exit_summary,
        "config": {
            "z_entry": Z_ENTRY,
            "z_exit": Z_EXIT,
            "max_hold": MAX_HOLD,
            "sigmoid_z_mid": SIGMOID_Z_MID,
            "sigmoid_k": SIGMOID_K,
            "mvs_threshold": MVS_THRESHOLD,
            "limit_offset_atr": LIMIT_OFFSET_ATR,
            "vol_climax_mult": VOL_CLIMAX_MULT,
            "second_leg_window": SECOND_LEG_WINDOW,
        },
        "hard_stop_24h": hour_counts,
        "hard_stop_by_session": session_hs,
        "inflection_skipped": inflection_skipped,
        "volume_skipped": volume_skipped,
        "total_attempted": total_attempted,
        "second_leg": {
            "n_trades": len(sl_trades),
            "n_traded": len([t for t in sl_trades if t["traded"]]),
            "total_r": round(float(np.sum(sl_pnls)) if sl_pnls else 0.0, 4),
            "sized_total_r": round(float(np.sum(sl_sized_pnls)) if sl_sized_pnls else 0.0, 4),
        },
    }


def build_parser():
    p = argparse.ArgumentParser(prog="backtest_v3_combined")
    p.add_argument("--price-data", type=Path, required=True)
    p.add_argument("--output", type=Path, default=None)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    data_path = Path(args.price_data)
    if not data_path.exists():
        print(f"[ERROR] Price data not found: {data_path}")
        return 1

    print(f"[1/2] Loading: {data_path}")
    ohlc = load_ohlc(data_path)
    print(f"       {ohlc['n_bars']} M5 bars")

    print("[2/2] Running v3.2 + Three Knives backtest...")
    results = run_backtest(ohlc)

    print()
    print("=" * 80)
    print("  v3.2 + Three Knives — Final Results")
    print("=" * 80)
    print(
        f"  Total signals (|Z|>=1.5): {results['total_attempted'] + results['inflection_skipped'] + results['volume_skipped']}"
    )
    print(f"  Inflection skipped: {results['inflection_skipped']}")
    print(f"  Volume skipped: {results['volume_skipped']}")
    print(f"  After gates: {results['unweighted']['n_trades']}")
    print(f"  Traded (after regime+MVS+session): {results['n_traded']}")
    print(f"  Filtered out: {results['n_filtered']} ({results['filter_rate']:.1%})")
    print()
    print(
        f"  K3 Second-Leg: {results['second_leg']['n_trades']} signals, "
        f"{results['second_leg']['n_traded']} traded, "
        f"sized R={results['second_leg']['sized_total_r']:.2f}"
    )

    headers = ["Metric", "Unweighted", "Bandit-Sized"]
    fmt = "  {:<25} {:>15} {:>15}"
    print()
    print(fmt.format(*headers))
    print("  " + "-" * 55)
    for key, label in [
        ("n_trades", "N Trades"),
        ("win_rate", "Win Rate"),
        ("avg_pnl_r", "Avg PnL(R)"),
        ("total_r", "Total R"),
        ("sharpe", "Sharpe"),
        ("sortino", "Sortino"),
        ("avg_win_r", "Avg Win(R)"),
        ("avg_loss_r", "Avg Loss(R)"),
    ]:
        uw = results["unweighted"].get(key, "N/A")
        bs = results["bandit_sized"].get(f"sized_{key}", "N/A")
        uw_str = f"{uw:.4f}" if isinstance(uw, float) else str(uw)
        bs_str = f"{bs:.4f}" if isinstance(bs, float) else str(bs)
        print(fmt.format(label, uw_str, bs_str))

    print()
    print("=" * 80)
    print("  Exit Breakdown")
    print("=" * 80)
    print(
        "  {:<22} {:>6} {:>8} {:>10} {:>12} {:>10}".format(
            "Exit Type", "N", "Pct", "Win Rate", "Avg PnL(R)", "Total R"
        )
    )
    print("  " + "-" * 72)
    for key, data in results["exit_breakdown"].items():
        print(
            "  {:<22} {:>6} {:>8} {:>10} {:>12} {:>10}".format(
                key,
                data["n"],
                f"{data['pct']:.1%}",
                f"{data['win_rate']:.1%}",
                f"{data['avg_pnl_r']:.4f}",
                f"{data['total_r']:.2f}",
            )
        )

    # 24h histogram
    print()
    print("=" * 80)
    print("  Hard Stop 24h Histogram (UTC) — Traded Only")
    print("=" * 80)
    hour_counts = results["hard_stop_24h"]
    max_count = max(hour_counts) if max(hour_counts) > 0 else 1
    bar_max = 40
    for h in range(24):
        count = hour_counts[h]
        bar_len = int(count / max_count * bar_max) if count > 0 else 0
        bar = "#" * bar_len
        label = session_name(h)
        print(f"  {h:02d}:00 [{label:<7}] {count:>4} {bar}")

    print()
    total_hs = sum(results["hard_stop_by_session"].values())
    print("  Hard stops by session:")
    for sess, count in results["hard_stop_by_session"].items():
        pct = count / max(total_hs, 1) * 100
        print(f"    {sess:<8}: {count:>4} ({pct:.1f}%)")

    if args.output:
        out_dir = Path(args.output)
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
        out_path = out_dir / f"v3_combined_{ts}.json"
        results["timestamp"] = ts
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False, default=str)
        print(f"\n  Results saved to: {out_path}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
