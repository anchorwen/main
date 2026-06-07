"""Pre-warm LocalFeatureStore from CSV price data.

Computes V9 40-dim features from historical OHLCV CSV and writes to the
LocalFeatureStore JSONL cache. This eliminates Tier-3 zero-vector fallback
when the live system starts with an empty store.

Usage:
  python scripts/features/feature_store_warmer.py --csv D:/ai/Meta_ppo_v6/Exness_XAUUSDm_2026_04.csv --store-dir data/feature_store
  python scripts/features/feature_store_warmer.py --csv data.csv --store-dir data/feature_store --max-rows 10000
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from core.features.local_feature_store import LocalFeatureStore
from core.features.schemas.v9_institutional_schema import V9_INSTITUTIONAL_40_FEATURES
from core.features.store_contracts import FeatureRecord, FeatureSchema

SCHEMA_NAME = "v9_institutional_40"
SCHEMA_VERSION_STORE = "1.0.0"
ATR_PERIOD = 14
RSI_PERIOD = 14
MACD_FAST = 12
MACD_SLOW = 26
VOL_ZS_LOOKBACK = 20
OU_LOOKBACK = 20
HURST_MAX_LAG = 20
MIN_BARS = (
    max(ATR_PERIOD, RSI_PERIOD, MACD_SLOW + 9, VOL_ZS_LOOKBACK, OU_LOOKBACK, HURST_MAX_LAG) + 2
)


def _body_ratio(o, h, l, c):
    denom = np.where(h - l == 0, 1e-8, h - l)
    return float(np.clip((c - o) / denom, -1.0, 1.0))


def _returns(close_arr):
    if len(close_arr) < 2:
        return 0.0
    return float((close_arr[-1] - close_arr[-2]) / close_arr[-2] * 100.0)


def _atr(high_arr, low_arr, close_arr, period=ATR_PERIOD):
    if len(close_arr) < period + 1:
        return 0.0
    prev_c = close_arr[-(period + 1) : -1]
    cur_h = high_arr[-period:]
    cur_l = low_arr[-period:]
    tr = np.maximum(cur_h - cur_l, np.maximum(np.abs(cur_h - prev_c), np.abs(cur_l - prev_c)))
    return float(np.mean(tr))


def _rsi(close_arr, period=RSI_PERIOD):
    if len(close_arr) < period + 1:
        return 50.0
    deltas = np.diff(close_arr[-(period + 1) :])
    gain = np.mean(np.maximum(deltas, 0))
    loss = np.mean(np.abs(np.minimum(deltas, 0)))
    if loss == 0:
        return 100.0
    return float(100.0 - 100.0 / (1.0 + gain / loss))


def _ema(data, period):
    if len(data) < period:
        return float(np.mean(data))
    alpha = 2.0 / (period + 1.0)
    result = np.mean(data[:period])
    for val in data[period:]:
        result = alpha * val + (1 - alpha) * result
    return float(result)


def _macd(close_arr):
    need = MACD_SLOW + 9
    if len(close_arr) < need:
        return 0.0
    return float(_ema(close_arr, MACD_FAST) - _ema(close_arr, MACD_SLOW))


def _vol_zscore(volume_arr, lookback=VOL_ZS_LOOKBACK):
    if len(volume_arr) < lookback + 1:
        return 0.0
    window = volume_arr[-lookback:]
    mean = np.mean(window)
    std = np.std(window)
    if std == 0:
        return 0.0
    return float((volume_arr[-1] - mean) / std)


def _ou_theta(price_arr, lookback=OU_LOOKBACK):
    if len(price_arr) < lookback + 1:
        return 0.0
    y = np.diff(price_arr[-lookback:])
    x = price_arr[-lookback:-1]
    if len(x) < 2:
        return 0.0
    x_mean = np.mean(x)
    y_mean = np.mean(y)
    beta = np.sum((x - x_mean) * (y - y_mean)) / (np.sum((x - x_mean) ** 2) + 1e-12)
    theta = -beta
    return float(max(theta, 1e-6))


def _hurst(price_arr, max_lag=HURST_MAX_LAG):
    n = len(price_arr)
    if n < max_lag + 1:
        return 0.5
    returns = np.diff(price_arr[-max_lag - 1 :])
    if len(returns) < 4:
        return 0.5
    lags = range(2, min(max_lag, len(returns)))
    rs_values = []
    for lag in lags:
        segments = len(returns) // lag
        if segments < 1:
            continue
        rs = 0.0
        for s in range(segments):
            chunk = returns[s * lag : (s + 1) * lag]
            mean = np.mean(chunk)
            cum_dev = np.cumsum(chunk - mean)
            r: float = float(np.max(cum_dev) - np.min(cum_dev))
            s_val = float(np.std(chunk))
            if s_val > 0:
                rs += r / s_val
        rs_values.append(rs / max(segments, 1))
    if len(rs_values) < 2:
        return 0.5
    log_lags = np.log(list(lags)[: len(rs_values)])
    log_rs = np.log(np.array(rs_values) + 1e-8)
    slope = np.polyfit(log_lags, log_rs, 1)[0]
    return round(float(max(0.0, min(1.0, slope))), 6)


def compute_features_from_ohlc(ohlc_df: pd.DataFrame) -> dict[str, float]:
    """Compute all 40 features from a DataFrame with columns: time, open, high, low, close, volume."""
    o = ohlc_df["open"].values.astype(np.float64)
    h = ohlc_df["high"].values.astype(np.float64)
    l = ohlc_df["low"].values.astype(np.float64)
    c = ohlc_df["close"].values.astype(np.float64)
    v = (
        ohlc_df["volume"].values.astype(np.float64)
        if "volume" in ohlc_df.columns
        else np.ones_like(c)
    )
    label = "M5"

    return {
        f"{label}_Ret_1": _returns(c),
        f"{label}_Body_Ratio": _body_ratio(o[-1], h[-1], l[-1], c[-1]),
        f"{label}_ATR_14": _atr(h, l, c),
        f"{label}_RSI_14": _rsi(c),
        f"{label}_MACD": _macd(c),
        f"{label}_Vol_ZScore": _vol_zscore(v),
        f"{label}_Macro1_Corr": _returns(c) / 100.0 if abs(_returns(c)) < 100 else 0.0,
        f"{label}_Price_ZScore": float((c[-1] - np.mean(c[-20:])) / (np.std(c[-20:]) + 1e-8))
        if len(c) >= 20
        else 0.0,
        f"{label}_OU_Theta": _ou_theta(c),
        f"{label}_Hurst": _hurst(c),
    }


def _resample_ohlc(df: pd.DataFrame, factor: int) -> pd.DataFrame:
    """Resample M5 OHLC bars to a higher timeframe by aggregating `factor` rows."""
    if factor <= 1:
        return df.copy()
    rows = []
    for start in range(0, len(df), factor):
        chunk = df.iloc[start : start + factor]
        rows.append(
            {
                "open": chunk["open"].iloc[0],
                "high": chunk["high"].max(),
                "low": chunk["low"].min(),
                "close": chunk["close"].iloc[-1],
                "volume": chunk["volume"].sum() if "volume" in chunk.columns else factor,
            }
        )
    return pd.DataFrame(rows)


def warm_store(
    csv_path: Path,
    store_dir: Path,
    *,
    symbol: str = "XAUUSDc",
    timeframe: str = "M5",
    max_rows: int = 50000,
    step: int = 5,
) -> dict[str, Any]:
    """Read CSV, compute features, write to LocalFeatureStore."""
    if not csv_path.exists():
        return {"error": f"csv_not_found: {csv_path}"}

    df = pd.read_csv(csv_path)
    if len(df) > max_rows:
        df = df.iloc[:: len(df) // max_rows][:max_rows]

    price_col = None
    for col in ["close", "Close", "CLOSE", "Bid", "bid", "Price", "price"]:
        if col in df.columns:
            price_col = col
            break
    if price_col is None:
        for col in df.columns:
            lower = col.lower()
            if any(kw in lower for kw in ["close", "bid", "price", "mid"]):
                price_col = col
                break
    if price_col is None:
        return {"error": "no_price_column_found", "columns": list(df.columns)}

    # Ensure OHLCV columns
    # Ensure OHLCV columns — derive from available data
    for target in ["open", "high", "low", "volume"]:
        if target not in df.columns or df[target].isna().all():
            if target == "open":
                df["open"] = df[price_col]
            elif target == "high":
                df["high"] = df[price_col]
            elif target == "low":
                df["low"] = df[price_col]
            elif target == "volume":
                df["volume"] = 1
    if "close" not in df.columns:
        df["close"] = df[price_col]

    store = LocalFeatureStore(str(store_dir))

    # Register schema
    schema = FeatureSchema(
        name=SCHEMA_NAME,
        version=SCHEMA_VERSION_STORE,
        fields=tuple(V9_INSTITUTIONAL_40_FEATURES),
        symbol=symbol,
        timeframe=timeframe,
        description="V9 Institutional 40-dim feature vector",
    )
    try:  # noqa: SIM105
        store.register_schema(schema)
    except Exception:  # noqa: BLE001
        pass

    # Generate time index (M5 bars from CSV start)
    base_time = datetime(2026, 1, 1, 0, 0, tzinfo=UTC).replace(tzinfo=None)
    time_col = None
    for tc in ["time", "Time", "timestamp", "Timestamp", "datetime", "DateTime"]:
        if tc in df.columns:
            time_col = tc
            break
    if time_col:
        try:
            ts = pd.to_datetime(df[time_col].iloc[0])
            if hasattr(ts, "to_pydatetime"):
                base_time = ts.to_pydatetime()
            else:
                base_time = ts
        except Exception:  # noqa: BLE001
            pass

    # Pre-build resampled OHLC for each timeframe
    tf_factors = {"M5": 1, "M15": 3, "M30": 6, "H1": 12}
    tf_dfs = {label: _resample_ohlc(df, factor) for label, factor in tf_factors.items()}

    # Pre-built zero fallback for higher timeframes not yet eligible
    _M5_KEYS = [
        "M5_Ret_1",
        "M5_Body_Ratio",
        "M5_ATR_14",
        "M5_RSI_14",
        "M5_MACD",
        "M5_Vol_ZScore",
        "M5_Macro1_Corr",
        "M5_Price_ZScore",
        "M5_OU_Theta",
        "M5_Hurst",
    ]
    _ZERO_FEATS = {k: 0.0 for k in _M5_KEYS}

    # Build timestamp array from CSV
    timestamps: list = []
    n_rows = len(df)
    if time_col and time_col in df.columns:
        try:
            timestamps = pd.to_datetime(df[time_col]).to_list()
        except Exception:  # noqa: BLE001
            timestamps = [base_time + timedelta(minutes=5 * i) for i in range(n_rows)]
    else:
        timestamps = [base_time + timedelta(minutes=5 * i) for i in range(n_rows)]

    records = []
    n = n_rows
    for i in range(MIN_BARS, n, step):
        feats = {}
        for tf_label, factor in tf_factors.items():
            tf_df = tf_dfs[tf_label]
            tf_idx = i // factor
            if tf_idx >= MIN_BARS:
                tf_feats = compute_features_from_ohlc(tf_df.iloc[: tf_idx + 1])
            else:
                tf_feats = _ZERO_FEATS
            for k, v in tf_feats.items():
                feats[k.replace("M5_", f"{tf_label}_")] = v

        if i < len(timestamps):
            event_time = timestamps[i]
            if hasattr(event_time, "to_pydatetime"):
                event_time = event_time.to_pydatetime()
            if hasattr(event_time, "tzinfo") and event_time.tzinfo is not None:
                event_time = event_time.replace(tzinfo=None) - event_time.utcoffset()
        else:
            event_time = base_time + timedelta(minutes=5 * i)
        records.append(
            FeatureRecord(
                schema_name=SCHEMA_NAME,
                schema_version=SCHEMA_VERSION_STORE,
                symbol=symbol,
                timeframe=timeframe,
                event_time=event_time,
                values=feats,
                source="feature_store_warmer",
                ingested_at=datetime.now(UTC).replace(tzinfo=None),
            )
        )

    written = store.write_records(records)

    return {
        "csv_path": str(csv_path),
        "store_dir": str(store_dir),
        "csv_rows": len(df),
        "feature_records_written": written,
        "feature_dim": 40,
        "symbol": symbol,
        "timeframe": timeframe,
        "step": step,
    }


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="feature_store_warmer")
    p.add_argument("--csv", type=Path, required=True, help="CSV price data")
    p.add_argument(
        "--store-dir",
        type=Path,
        default=Path("data/feature_store"),
        help="LocalFeatureStore base dir",
    )
    p.add_argument("--symbol", default="XAUUSDc")
    p.add_argument("--timeframe", default="M5")
    p.add_argument("--max-rows", type=int, default=50000, help="Max rows to process")
    p.add_argument("--step", type=int, default=5, help="Step interval between feature snapshots")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result = warm_store(
        args.csv,
        args.store_dir,
        symbol=args.symbol,
        timeframe=args.timeframe,
        max_rows=args.max_rows,
        step=args.step,
    )
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    if "error" in result:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
