"""DCI auditor calendar-aware staleness (FIX-20260821-001, TECH_DEBT-011).

Weekend reproduction lock: on Saturday, a data chain frozen at Friday close must
NOT trip the hardcoded-age faults (previously a 12h false positive); the same
data on Monday morning (market reopened) MUST still trip them.  All 7 staleness
sites in audit_data_chain_integrity.py converge on _is_stale → staleness_anchor
(the single calendar clock, core/market/calendar.py).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.audit_data_chain_integrity import (
    _is_stale,
    _market_type_for_data_dir,
    _market_type_for_symbol,
    s1_event_ingress,
)

# Reference dates (all UTC): 2026-08-14=Friday, 2026-08-15=Saturday,
# 2026-08-16=Sunday, 2026-08-17=Monday.
FRI_CLOSE = datetime(2026, 8, 14, 21, 0, tzinfo=UTC)  # data frozen at Fri close
SAT = datetime(2026, 8, 15, 10, 0, tzinfo=UTC)
MON = datetime(2026, 8, 17, 8, 0, tzinfo=UTC)


class TestIsStale:
    def test_forex_weekend_freeze_not_stale(self):
        # Sat, XAU closed → anchor = Fri 22:00 - 12h = Fri 10:00; Fri 21:00 > anchor → fresh
        assert _is_stale(FRI_CLOSE, SAT, "forex_24_5", base_threshold_min=12 * 60) is False

    def test_forex_monday_reopened_stale(self):
        # Mon, XAU open → anchor = Mon 08:00 - 12h = Sun 20:00; Fri 21:00 < anchor → stale
        assert _is_stale(FRI_CLOSE, MON, "forex_24_5", base_threshold_min=12 * 60) is True

    def test_crypto_never_relaxes(self):
        # BTC 24/7 → Sat still open → anchor = Sat 10:00 - 12h = Fri 22:00; Fri 21:00 < → stale
        assert _is_stale(FRI_CLOSE, SAT, "crypto_24_7", base_threshold_min=12 * 60) is True

    def test_none_ts_not_stale(self):
        assert _is_stale(None, SAT, "forex_24_5", base_threshold_min=12 * 60) is False


class TestMarketTypeDerivation:
    def test_data_dir(self):
        assert _market_type_for_data_dir(Path("data_btc")) == "crypto_24_7"
        assert _market_type_for_data_dir(Path("data")) == "forex_24_5"
        assert _market_type_for_data_dir(Path("data_xau")) == "forex_24_5"

    def test_symbol(self):
        assert _market_type_for_symbol("XAUUSDc") == "forex_24_5"
        assert _market_type_for_symbol("BTCUSDC") == "crypto_24_7"
        assert _market_type_for_symbol("") == "crypto_24_7"


class TestS1WeekendRepro:
    """Synthetic data dir: XAU feature frozen at Fri close → Sat NO S1_FEATURE_STALE
    (was a false positive at the hardcoded 12h); Mon (reopened) → S1_FEATURE_STALE."""

    @pytest.fixture
    def data_dir(self, tmp_path: Path) -> Path:
        sym = tmp_path / "feature_store" / "records" / "symbol=XAUUSDc" / "timeframe=M5"
        sym.mkdir(parents=True)
        (tmp_path / "feature_store" / "schemas.json").write_text("{}", encoding="utf-8")
        row = {
            "schema_name": "technical_v1",
            "schema_version": "1.0",
            "symbol": "XAUUSDc",
            "timeframe": "M5",
            "event_time": "2026-08-14T21:00:00Z",
            "values": {"ema_bias": 1.0, "adx_slope": 0.1},
            "source": "test",
        }
        with (sym / "features.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(row) + "\n")
        return tmp_path

    def test_saturday_no_false_positive(self, data_dir: Path):
        faults = s1_event_ingress(data_dir, SAT)
        assert not any(f["code"] == "S1_FEATURE_STALE" for f in faults)

    def test_monday_still_catches(self, data_dir: Path):
        faults = s1_event_ingress(data_dir, MON)
        assert any(f["code"] == "S1_FEATURE_STALE" for f in faults)
