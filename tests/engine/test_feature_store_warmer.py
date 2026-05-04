"""Feature store warmer contract tests."""

from pathlib import Path

import numpy as np
import pandas as pd

from scripts.features.feature_store_warmer import (
    _atr,
    _hurst,
    _macd,
    _ou_theta,
    _returns,
    _rsi,
    _vol_zscore,
    compute_features_from_ohlc,
    warm_store,
)


def _write_test_csv(path: Path, n: int = 100) -> None:
    np.random.seed(42)
    close = 2600.0 + np.cumsum(np.random.randn(n) * 0.5)
    df = pd.DataFrame(
        {
            "time": range(n),
            "open": close + np.random.randn(n) * 0.1,
            "high": close + np.abs(np.random.randn(n)) * 2.0,
            "low": close - np.abs(np.random.randn(n)) * 2.0,
            "close": close,
            "volume": np.random.randint(100, 1000, n),
        }
    )
    df.to_csv(path, index=False)


def test_returns():
    arr = np.array([100.0, 101.0])
    assert abs(_returns(arr) - 1.0) < 0.01


def test_returns_short():
    assert _returns(np.array([100.0])) == 0.0


def test_atr():
    n = 20
    h = np.ones(n) * 2.0
    l = np.ones(n) * 1.0
    c = np.ones(n) * 2.0
    assert _atr(h, l, c) > 0


def test_rsi():
    c = np.array([100.0 + i * 0.1 for i in range(20)])
    rsi = _rsi(c)
    assert 0 <= rsi <= 100


def test_macd():
    c = np.linspace(100, 110, 50)
    macd_val = _macd(c)
    assert isinstance(macd_val, float)


def test_vol_zscore():
    v = np.array([100.0] * 30)
    assert _vol_zscore(v) == 0.0


def test_ou_theta():
    prices = 2600.0 + np.cumsum(np.random.randn(50) * 0.1)
    theta = _ou_theta(prices)
    assert theta >= 0


def test_hurst():
    prices = np.cumsum(np.random.randn(50) * 0.1) + 2600
    h = _hurst(prices)
    assert 0.0 <= h <= 1.0


def test_compute_features_from_ohlc():
    n = 60
    np.random.seed(1)
    close = 2600.0 + np.cumsum(np.random.randn(n) * 0.5)
    df = pd.DataFrame(
        {
            "time": range(n),
            "open": close + np.random.randn(n) * 0.1,
            "high": close + np.abs(np.random.randn(n)) * 2.0,
            "low": close - np.abs(np.random.randn(n)) * 2.0,
            "close": close,
            "volume": np.random.randint(100, 1000, n),
        }
    )
    feats = compute_features_from_ohlc(df)
    assert len(feats) == 10
    assert all(f"M5_{k}" in feats for k in ["Ret_1", "RSI_14", "ATR_14", "MACD", "Hurst"])
    assert all(isinstance(v, int | float) for v in feats.values())


def test_warm_store_with_csv(tmp_path: Path):
    _write_test_csv(tmp_path / "test.csv", n=80)
    store_dir = tmp_path / "feature_store"
    result = warm_store(tmp_path / "test.csv", store_dir, max_rows=100, step=10)
    assert "error" not in result
    assert result["feature_records_written"] > 0


def test_warm_store_csv_not_found(tmp_path: Path):
    result = warm_store(tmp_path / "nonexistent.csv", tmp_path / "store")
    assert "error" in result


def test_cli_help():
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "scripts/features/feature_store_warmer.py", "--help"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert proc.returncode == 0
    assert "csv" in proc.stdout
