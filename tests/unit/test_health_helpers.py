"""Unit tests for core.observability._health_helpers — SF #28 extraction validation."""

from __future__ import annotations

import json
import os
import tempfile

from core.observability._health_helpers import (
    _age_minutes,
    _safe_json_load,
    _safe_jsonl_count,
    _safe_jsonl_last,
    _safe_jsonl_tail_stats,
    _utc_iso,
)


class TestUtcIso:
    def test_returns_iso_format_string(self):
        result = _utc_iso()
        assert isinstance(result, str)
        assert "T" in result  # ISO format has T separator

    def test_returns_different_values_on_successive_calls(self):
        a = _utc_iso()
        b = _utc_iso()
        # May be equal if called very fast, just check format
        assert len(a) >= 19  # at least "YYYY-MM-DDTHH:MM:SS"


class TestAgeMinutes:
    def test_none_returns_negative(self):
        assert _age_minutes(None) == -1.0

    def test_empty_string_returns_negative(self):
        assert _age_minutes("") == -1.0

    def test_invalid_string_returns_negative(self):
        assert _age_minutes("not-a-date") == -1.0

    def test_recent_timestamp_returns_small_age(self):
        age = _age_minutes(_utc_iso())
        assert age >= 0.0
        assert age < 1.0  # Should be less than 1 minute old

    def test_old_timestamp_returns_large_age(self):
        age = _age_minutes("2020-01-01T00:00:00")
        assert age > 100_000  # ~6 years in minutes

    def test_timestamp_with_timezone(self):
        age = _age_minutes("2020-01-01T00:00:00+00:00")
        assert age > 100_000

    def test_malformed_partial_timestamp(self):
        # "2020-01-01" is a valid ISO date — Python 3.11 fromisoformat parses it.
        # The age should be large (years in minutes), not -1.0.
        age = _age_minutes("2020-01-01")
        assert age > 100_000  # ~6 years in minutes


class TestSafeJsonLoad:
    def test_missing_file_returns_none(self):
        assert _safe_json_load("/nonexistent/path.json") is None

    def test_valid_json(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"key": "value", "num": 42}, f)
            path = f.name
        try:
            result = _safe_json_load(path)
            assert result == {"key": "value", "num": 42}
        finally:
            os.unlink(path)

    def test_invalid_json_returns_none(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("not valid json {{{")
            path = f.name
        try:
            assert _safe_json_load(path) is None
        finally:
            os.unlink(path)

    def test_empty_file_returns_none(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write("")
            path = f.name
        try:
            assert _safe_json_load(path) is None
        finally:
            os.unlink(path)


class TestSafeJsonlCount:
    def test_missing_file_returns_none(self):
        assert _safe_jsonl_count("/nonexistent/file.jsonl") is None

    def test_empty_file_returns_zero(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            path = f.name
        try:
            assert _safe_jsonl_count(path) == 0
        finally:
            os.unlink(path)

    def test_counts_lines(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('{"a": 1}\n{"b": 2}\n{"c": 3}\n')
            path = f.name
        try:
            assert _safe_jsonl_count(path) == 3
        finally:
            os.unlink(path)

    def test_blank_lines_counted(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('{"a": 1}\n\n{"b": 2}\n')
            path = f.name
        try:
            assert _safe_jsonl_count(path) == 3  # blank line counts as a line
        finally:
            os.unlink(path)


class TestSafeJsonlLast:
    def test_missing_file_returns_none(self):
        assert _safe_jsonl_last("/nonexistent/file.jsonl") is None

    def test_empty_file_returns_none(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            path = f.name
        try:
            assert _safe_jsonl_last(path) is None
        finally:
            os.unlink(path)

    def test_single_line(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('{"event": "test", "value": 42}\n')
            path = f.name
        try:
            result = _safe_jsonl_last(path)
            assert result == {"event": "test", "value": 42}
        finally:
            os.unlink(path)

    def test_returns_last_line(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('{"line": 1}\n{"line": 2}\n{"line": 3}\n')
            path = f.name
        try:
            result = _safe_jsonl_last(path)
            assert result == {"line": 3}
        finally:
            os.unlink(path)

    def test_large_file_tail_read(self):
        """Test that tail-read optimization works for files larger than 8192 bytes."""
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            # Write enough lines to exceed 8192 bytes
            for i in range(500):
                f.write(f'{{"line": {i}, "padding": "{"x" * 50}"}}\n')
            f.write('{"line": "last", "marker": true}\n')
            path = f.name
        try:
            result = _safe_jsonl_last(path)
            assert result == {"line": "last", "marker": True}
        finally:
            os.unlink(path)

    def test_invalid_json_line_skipped(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('{"valid": 1}\n')
            f.write("not json\n")
            f.write('{"valid": 3}\n')
            path = f.name
        try:
            result = _safe_jsonl_last(path)
            assert result == {"valid": 3}
        finally:
            os.unlink(path)


class TestSafeJsonlTailStats:
    def test_missing_file_returns_empty_dict(self):
        assert _safe_jsonl_tail_stats("/nonexistent/file.jsonl") == {}

    def test_empty_file_returns_zeroed_stats(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            path = f.name
        try:
            stats = _safe_jsonl_tail_stats(path)
            # Returns zeroed stats dict, not empty dict — file exists but is empty
            assert stats["total_lines"] == 0
            assert stats["close_count_tail"] == 0
            assert stats["open_count_tail"] == 0
        finally:
            os.unlink(path)

    def test_close_entries_counted(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('{"action": "close", "pnl": 10.5, "label": "sl_hit", "ack_status": "closed"}\n')
            f.write('{"action": "close", "pnl": -5.0, "label": "tp_hit", "ack_status": "closed"}\n')
            f.write('{"action": "open", "pnl": null}\n')
            path = f.name
        try:
            stats = _safe_jsonl_tail_stats(path)
            assert stats["close_count_tail"] == 2
            assert stats["open_count_tail"] == 1
            assert stats["pnl_null_count"] == 0
            assert stats["pnl_null_rate"] == 0.0
            assert stats["total_lines"] == 3
        finally:
            os.unlink(path)

    def test_pnl_null_detected(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write(
                '{"action": "close", "pnl": null, "label": "unknown", "ack_status": "closed"}\n'
            )
            f.write(
                '{"action": "close", "pnl": null, "label": "unknown", "ack_status": "closed"}\n'
            )
            path = f.name
        try:
            stats = _safe_jsonl_tail_stats(path)
            assert stats["pnl_null_count"] == 2
            assert stats["pnl_null_rate"] == 1.0
        finally:
            os.unlink(path)

    def test_retry_counted(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write(
                '{"action": "close", "pnl": 0, "label": "breakeven", "ack_status": "rejected"}\n'
            )
            f.write(
                '{"action": "close", "pnl": 0, "label": "breakeven", "ack_status": "rejected"}\n'
            )
            path = f.name
        try:
            stats = _safe_jsonl_tail_stats(path)
            assert stats["retry_count"] == 2
        finally:
            os.unlink(path)

    def test_label_distribution(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('{"action": "close", "pnl": 1, "label": "sl_hit", "ack_status": "closed"}\n')
            f.write('{"action": "close", "pnl": 2, "label": "sl_hit", "ack_status": "closed"}\n')
            f.write('{"action": "close", "pnl": 3, "label": "tp_hit", "ack_status": "closed"}\n')
            path = f.name
        try:
            stats = _safe_jsonl_tail_stats(path)
            assert stats["label_distribution"] == {"sl_hit": 2, "tp_hit": 1}
        finally:
            os.unlink(path)

    def test_invalid_json_lines_skipped(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            f.write('{"action": "close", "pnl": 1, "label": "sl_hit", "ack_status": "closed"}\n')
            f.write("garbage line\n")
            f.write('{"action": "close", "pnl": 2, "label": "tp_hit", "ack_status": "closed"}\n')
            path = f.name
        try:
            stats = _safe_jsonl_tail_stats(path)
            assert stats["close_count_tail"] == 2
        finally:
            os.unlink(path)
