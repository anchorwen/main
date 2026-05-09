"""Tests for MiFID II compliance trade report generation."""

from __future__ import annotations

import json

import pytest

from core.deployment.compliance_export import (
    OPTIONAL_TRADE_FIELDS,
    REQUIRED_TRADE_FIELDS,
    STANDARD_INSTRUMENTS,
    ComplianceReport,
    TradeRecord,
    _build_summary,
    generate_order_audit_trail,
    generate_trade_report,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def sample_trade_dict():
    return {
        "trade_id": "T-001",
        "order_id": "O-001",
        "symbol": "XAUUSDc",
        "side": "buy",
        "quantity": 0.01,
        "price": 2000.50,
        "venue": "MT5",
        "timestamp": "2026-01-15T10:30:00+00:00",
        "execution_timestamp": "2026-01-15T10:30:01+00:00",
        "decision_price": 2000.00,
        "arrival_price": 2000.25,
        "commission": 0.50,
        "spread_bps": 1.2,
        "slippage_bps": 2.5,
        "latency_ms": 150.0,
        "fill_rate": 1.0,
        "reject_reason": "",
        "strategy_id": "barrier_12bar",
        "brain_id": "v9_institutional_01",
        "regime": "trending",
        "pnl": 15.0,
    }


@pytest.fixture
def sample_trades(sample_trade_dict):
    buy = dict(sample_trade_dict)
    sell = dict(sample_trade_dict)
    sell["trade_id"] = "T-002"
    sell["order_id"] = "O-002"
    sell["side"] = "sell"
    sell["price"] = 2010.00
    sell["pnl"] = 10.0
    rejected = dict(sample_trade_dict)
    rejected["trade_id"] = "T-003"
    rejected["order_id"] = "O-003"
    rejected["reject_reason"] = "regime_gate_blocked"
    rejected["fill_rate"] = 0.0
    return [buy, sell, rejected]


# ── TradeRecord ───────────────────────────────────────────────────────────────


class TestTradeRecord:
    def test_from_trade_dict_basic(self, sample_trade_dict):
        rec = TradeRecord.from_trade_dict(sample_trade_dict)
        assert rec.trade_id == "T-001"
        assert rec.order_id == "O-001"
        assert rec.symbol == "XAUUSDc"
        assert rec.side == "buy"
        assert rec.quantity == 0.01
        assert rec.price == 2000.50
        assert rec.venue == "MT5"
        assert rec.decision_price == 2000.00
        assert rec.arrival_price == 2000.25
        assert rec.spread_bps == 1.2
        assert rec.slippage_bps == 2.5
        assert rec.latency_ms == 150.0
        assert rec.fill_rate == 1.0
        assert rec.strategy_id == "barrier_12bar"
        assert rec.brain_id == "v9_institutional_01"
        assert rec.regime == "trending"
        assert rec.pnl == 15.0
        assert rec.reject_reason == ""

    def test_from_trade_dict_defaults(self):
        rec = TradeRecord.from_trade_dict({})
        assert rec.trade_id == ""
        assert rec.symbol == "XAUUSDc"
        assert rec.quantity == 0.0
        assert rec.price == 0.0
        assert rec.venue == "MT5"
        assert rec.decision_price is None
        assert rec.slippage_bps is None
        assert rec.fill_rate == 1.0

    def test_from_trade_dict_aliases(self):
        d = {
            "ticket": 12345,
            "volume": 0.05,
            "entry_price": 1999.0,
            "time": "2026-03-01T08:00:00Z",
            "exit_time": "2026-03-01T09:00:00Z",
            "fill_ratio": 0.85,
        }
        rec = TradeRecord.from_trade_dict(d)
        assert rec.trade_id == "12345"
        assert rec.quantity == 0.05
        assert rec.price == 1999.0
        assert rec.timestamp == "2026-03-01T08:00:00Z"
        assert rec.execution_timestamp == "2026-03-01T09:00:00Z"
        assert rec.fill_rate == 0.85

    def test_from_trade_dict_with_isin_cfi(self):
        d = {"trade_id": "T-100", "symbol": "EURUSD"}
        rec = TradeRecord.from_trade_dict(d, isin="EU0009652759", cfi="MRCXXX")
        assert rec.isin == "EU0009652759"
        assert rec.cfi == "MRCXXX"

    def test_to_dict_uses_standard_instruments_lookup(self):
        rec = TradeRecord.from_trade_dict(
            {"trade_id": "T-200", "symbol": "XAUUSDc"}, isin="", cfi=""
        )
        d = rec.to_dict()
        assert d["isin"] == "XC0009655157"
        assert d["cfi"] == "MRCXXX"

    def test_to_dict_falls_back_to_isin_field(self):
        rec = TradeRecord.from_trade_dict(
            {"trade_id": "T-300", "symbol": "CUSTOM"}, isin="CUST1234", cfi="CFIXYZ"
        )
        d = rec.to_dict()
        assert d["isin"] == "CUST1234"
        assert d["cfi"] == "CFIXYZ"

    def test_to_dict_handles_none_optional_fields(self):
        rec = TradeRecord.from_trade_dict({"trade_id": "T-400"})
        d = rec.to_dict()
        assert d["decision_price"] is None
        assert d["arrival_price"] is None
        assert d["spread_bps"] is None
        assert d["slippage_bps"] is None
        assert d["pnl"] is None
        assert d["reject_reason"] == ""

    def test_to_dict_contains_all_required_keys(self, sample_trade_dict):
        rec = TradeRecord.from_trade_dict(sample_trade_dict)
        d = rec.to_dict()
        for key in REQUIRED_TRADE_FIELDS:
            assert key in d, f"Missing required field: {key}"

    def test_to_dict_contains_optional_strategy_keys(self, sample_trade_dict):
        rec = TradeRecord.from_trade_dict(sample_trade_dict)
        d = rec.to_dict()
        assert d["strategy_id"] == "barrier_12bar"
        assert d["brain_id"] == "v9_institutional_01"
        assert d["regime"] == "trending"


# ── STANDARD_INSTRUMENTS ──────────────────────────────────────────────────────


class TestStandardInstruments:
    def test_xauusd_has_isin(self):
        info = STANDARD_INSTRUMENTS["XAUUSDc"]
        assert info["isin"] == "XC0009655157"
        assert info["cfi"] == "MRCXXX"

    def test_xauusd_alias(self):
        info = STANDARD_INSTRUMENTS["XAUUSD"]
        assert info["isin"] == "XC0009655157"

    def test_eurusd(self):
        info = STANDARD_INSTRUMENTS["EURUSD"]
        assert info["isin"] == "EU0009652759"


# ── ComplianceReport ──────────────────────────────────────────────────────────


class TestComplianceReport:
    def test_to_dict_structure(self):
        report = ComplianceReport(
            report_type="rts27",
            generated_at="2026-01-15T12:00:00+00:00",
            firm_name="Test Fund",
            firm_lei="TEST1234567890ABCDE",
            reporting_period_start="2026-01-01T00:00:00Z",
            reporting_period_end="2026-01-15T00:00:00Z",
            venue="MT5",
            trades=[{"trade_id": "T-1"}],
            summary={"total_orders": 1},
        )
        d = report.to_dict()
        assert d["report_type"] == "rts27"
        assert d["firm_name"] == "Test Fund"
        assert d["firm_lei"] == "TEST1234567890ABCDE"
        assert d["reporting_period"]["start"] == "2026-01-01T00:00:00Z"
        assert d["reporting_period"]["end"] == "2026-01-15T00:00:00Z"
        assert d["venue"] == "MT5"
        assert d["trades"] == [{"trade_id": "T-1"}]
        assert d["summary"] == {"total_orders": 1}

    def test_to_json_produces_valid_json(self):
        report = ComplianceReport(
            report_type="rts28",
            generated_at="2026-01-15T12:00:00+00:00",
            firm_name="Test Fund",
            firm_lei="",
            reporting_period_start="2026-01-01",
            reporting_period_end="2026-01-15",
            venue="MT5",
        )
        js = report.to_json()
        parsed = json.loads(js)
        assert parsed["report_type"] == "rts28"
        assert parsed["trades"] == []

    def test_to_json_handles_unicode(self):
        report = ComplianceReport(
            report_type="rts27",
            generated_at="2026-01-15",
            firm_name="Quant Fund — Zürich",
            firm_lei="",
            reporting_period_start="",
            reporting_period_end="",
            venue="MT5",
        )
        js = report.to_json()
        assert "Zürich" in js


# ── generate_trade_report ─────────────────────────────────────────────────────


class TestGenerateTradeReport:
    def test_rts27_includes_trades(self, sample_trades):
        report = generate_trade_report(
            trades=sample_trades,
            firm_name="Test Fund",
            report_type="rts27",
        )
        assert report.report_type == "rts27"
        assert len(report.trades) == 3
        assert report.reporting_period_start != ""
        assert report.reporting_period_end != ""

    def test_rts28_excludes_trades(self, sample_trades):
        report = generate_trade_report(
            trades=sample_trades,
            report_type="rts28",
        )
        assert report.report_type == "rts28"
        assert report.trades == []
        assert report.summary["total_orders"] == 3

    def test_uses_provided_period(self, sample_trades):
        report = generate_trade_report(
            trades=sample_trades,
            period_start="2026-01-01",
            period_end="2026-01-31",
        )
        assert report.reporting_period_start == "2026-01-01"
        assert report.reporting_period_end == "2026-01-31"

    def test_deduces_period_from_trade_timestamps(self, sample_trades):
        report = generate_trade_report(trades=sample_trades)
        assert "2026-01-15" in report.reporting_period_start
        assert "2026-01-15" in report.reporting_period_end

    def test_uses_now_for_empty_trades(self):
        report = generate_trade_report(trades=[])
        assert report.reporting_period_start == report.reporting_period_end
        assert "2026" in report.reporting_period_start

    def test_default_firm_and_venue(self, sample_trades):
        report = generate_trade_report(trades=sample_trades)
        assert report.firm_name == "Quant Fund Ltd"
        assert report.venue == "MT5"

    def test_custom_lei(self, sample_trades):
        report = generate_trade_report(
            trades=sample_trades,
            firm_lei="1234567890ABCDEFGHIJ",
        )
        assert report.firm_lei == "1234567890ABCDEFGHIJ"

    def test_summary_contains_required_keys(self, sample_trades):
        report = generate_trade_report(trades=sample_trades, report_type="rts28")
        s = report.summary
        assert "total_orders" in s
        assert "filled_orders" in s
        assert "rejected_orders" in s
        assert "average_slippage_bps" in s
        assert "by_symbol" in s

    def test_summary_by_symbol(self, sample_trades):
        report = generate_trade_report(trades=sample_trades, report_type="rts28")
        by_sym = report.summary["by_symbol"]
        assert "XAUUSDc" in by_sym
        assert by_sym["XAUUSDc"]["trade_count"] == 2  # buy + sell (rejected excluded)


# ── _build_summary ────────────────────────────────────────────────────────────


class TestBuildSummary:
    def test_empty_records(self):
        s = _build_summary([], "MT5")
        assert s["venue"] == "MT5"
        assert s["total_orders"] == 0
        assert s["reject_rate"] == 0.0

    def test_rejected_separated_from_filled(self, sample_trades):
        records = [TradeRecord.from_trade_dict(t).to_dict() for t in sample_trades]
        s = _build_summary(records, "MT5")
        assert s["filled_orders"] == 2
        assert s["rejected_orders"] == 1
        assert s["reject_rate"] == pytest.approx(1 / 3, abs=0.01)

    def test_buy_sell_counts(self, sample_trades):
        records = [TradeRecord.from_trade_dict(t).to_dict() for t in sample_trades]
        s = _build_summary(records, "MT5")
        assert s["buy_trades"] == 1
        assert s["sell_trades"] == 1

    def test_averages(self, sample_trades):
        records = [TradeRecord.from_trade_dict(t).to_dict() for t in sample_trades]
        s = _build_summary(records, "MT5")
        assert s["average_slippage_bps"] == pytest.approx(2.5, abs=0.1)
        assert s["average_latency_ms"] == pytest.approx(150.0, abs=0.1)
        assert s["total_commission"] == pytest.approx(1.0, abs=0.01)
        assert s["total_volume"] == pytest.approx(0.02, abs=0.001)

    def test_none_values_excluded_from_avg(self):
        records = [
            TradeRecord.from_trade_dict(
                {"trade_id": "1", "symbol": "XAUUSDc", "slippage_bps": None}
            ).to_dict(),
            TradeRecord.from_trade_dict(
                {"trade_id": "2", "symbol": "XAUUSDc", "slippage_bps": 5.0}
            ).to_dict(),
        ]
        s = _build_summary(records, "MT5")
        assert s["average_slippage_bps"] == 5.0  # only Trade 2 counts

    def test_by_symbol_separation(self):
        records = [
            TradeRecord.from_trade_dict(
                {"trade_id": "1", "symbol": "XAUUSDc", "price": 2000, "quantity": 0.01}
            ).to_dict(),
            TradeRecord.from_trade_dict(
                {"trade_id": "2", "symbol": "EURUSD", "price": 1.05, "quantity": 0.10}
            ).to_dict(),
        ]
        s = _build_summary(records, "MT5")
        assert len(s["by_symbol"]) == 2
        assert s["by_symbol"]["XAUUSDc"]["total_volume"] == pytest.approx(0.01)
        assert s["by_symbol"]["EURUSD"]["total_volume"] == pytest.approx(0.10)


# ── generate_order_audit_trail ────────────────────────────────────────────────


class TestOrderAuditTrail:
    def test_creates_state_transitions_per_trade(self, sample_trades):
        trail = generate_order_audit_trail(sample_trades)
        # 3 trades × 2 states (CREATED + FILLED/REJECTED) = 6 records
        assert len(trail) == 6
        states = [r["state"] for r in trail]
        assert states.count("CREATED") == 3
        assert states.count("FILLED") == 2
        assert states.count("REJECTED") == 1

    def test_sequence_numbers_are_sequential(self, sample_trades):
        trail = generate_order_audit_trail(sample_trades)
        seqs = [r["sequence"] for r in trail]
        assert seqs == [1, 2, 3, 4, 5, 6]

    def test_rejected_trade_has_reason(self, sample_trades):
        trail = generate_order_audit_trail(sample_trades)
        rejected = [r for r in trail if r["state"] == "REJECTED"]
        assert len(rejected) == 1
        assert rejected[0]["reason"] == "regime_gate_blocked"

    def test_filled_trade_has_fill_price(self, sample_trades):
        trail = generate_order_audit_trail(sample_trades)
        filled = [r for r in trail if r["state"] == "FILLED"]
        assert all("fill_price" in r for r in filled)

    def test_empty_trades_returns_empty_list(self):
        trail = generate_order_audit_trail([])
        assert trail == []

    def test_writes_jsonl_to_path(self, sample_trades, tmp_path):
        out = tmp_path / "audit" / "trail.jsonl"
        generate_order_audit_trail(sample_trades, output_path=str(out))
        assert out.exists()
        lines = out.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 6
        # Each line is valid JSON
        for line in lines:
            record = json.loads(line)
            assert "sequence" in record

    def test_uses_ticket_as_fallback_id(self):
        trades = [{"ticket": 99999, "symbol": "XAUUSDc", "price": 2000}]
        trail = generate_order_audit_trail(trades)
        assert trail[0]["trade_id"] == 99999
        assert trail[0]["order_id"] == 99999

    def test_uses_trade_id_as_order_id_fallback(self):
        trades = [{"trade_id": "T-500", "symbol": "XAUUSDc", "price": 2000}]
        trail = generate_order_audit_trail(trades)
        assert trail[0]["order_id"] == "T-500"

    def test_order_id_takes_priority(self):
        trades = [
            {
                "trade_id": "T-600",
                "order_id": "O-600",
                "symbol": "XAUUSDc",
                "price": 2000,
            }
        ]
        trail = generate_order_audit_trail(trades)
        assert trail[0]["order_id"] == "O-600"


# ── Constants ─────────────────────────────────────────────────────────────────


class TestFieldLists:
    def test_required_fields_are_comprehensive(self):
        essential = {
            "trade_id",
            "order_id",
            "symbol",
            "isin",
            "side",
            "quantity",
            "price",
            "venue",
            "timestamp",
        }
        field_set = set(REQUIRED_TRADE_FIELDS)
        for f in essential:
            assert f in field_set

    def test_optional_fields_include_strategy_context(self):
        assert "strategy_id" in OPTIONAL_TRADE_FIELDS
        assert "brain_id" in OPTIONAL_TRADE_FIELDS
        assert "regime" in OPTIONAL_TRADE_FIELDS
        assert "pnl" in OPTIONAL_TRADE_FIELDS
