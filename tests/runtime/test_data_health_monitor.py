"""Tests for core.runtime.data_health_monitor — backward-compatible shim.

FIX-20260619-028: Tier 1 zero-coverage breakout.  Tests both the happy
delegation path and the exception-fallback safety net.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from core.runtime.data_health_monitor import check_data_health


class TestCheckDataHealth:
    """Unit tests for check_data_health() — the deprecated shim."""

    def test_returns_error_dict_when_service_unavailable(self) -> None:
        """When DataHealthService import fails, return a safe error dict.

        The Iron Law #1 safety net must never crash the main loop.
        """
        with patch(
            "core.observability.data_health_service.DataHealthService",
            side_effect=RuntimeError("no module"),
        ):
            result = check_data_health("/fake/path", "XAUUSDc")

        assert result["symbol"] == "XAUUSDc"
        assert result["checks"] == {}
        assert "data_health_service_error" in result["alerts"]

    def test_delegates_to_service_and_returns_compatible_dict(self) -> None:
        """Happy path: delegates to DataHealthService, returns old-format dict."""
        mock_report = MagicMock()
        mock_report.generated_at = "2026-06-19T12:00:00Z"
        mock_report.alert_level = "OK"
        mock_report.primary_codes = []
        mock_report.sources = []

        mock_svc = MagicMock()
        mock_svc.run_lightweight.return_value = mock_report

        with patch(
            "core.observability.data_health_service.DataHealthService",
            return_value=mock_svc,
        ):
            result = check_data_health("/fake/path", "XAUUSDc")

        assert result["symbol"] == "XAUUSDc"
        assert result["time"] == "2026-06-19T12:00:00Z"
        assert isinstance(result["checks"], dict)
        assert isinstance(result["alerts"], list)
        mock_svc.save_health_state.assert_called_once_with(mock_report)

    def test_includes_warn_and_fail_sources_in_alerts(self) -> None:
        """Sources with warn/fail/missing status appear in the alerts list."""
        mock_source_warn = MagicMock()
        mock_source_warn.source = "test_warn"
        mock_source_warn.status = MagicMock(value="warn")
        mock_source_warn.metrics = {}
        mock_source_warn.primary_code = "TEST_WARN"

        mock_source_fail = MagicMock()
        mock_source_fail.source = "test_fail"
        mock_source_fail.status = MagicMock(value="fail")
        mock_source_fail.metrics = {}
        mock_source_fail.primary_code = "TEST_FAIL"

        mock_source_ok = MagicMock()
        mock_source_ok.source = "test_ok"
        mock_source_ok.status = MagicMock(value="pass")
        mock_source_ok.metrics = {}
        mock_source_ok.primary_code = "TEST_OK"

        mock_report = MagicMock()
        mock_report.generated_at = "2026-06-19T12:00:00Z"
        mock_report.alert_level = "WARNING"
        mock_report.primary_codes = ["TEST_WARN", "TEST_FAIL"]
        mock_report.sources = [mock_source_warn, mock_source_fail, mock_source_ok]

        mock_svc = MagicMock()
        mock_svc.run_lightweight.return_value = mock_report
        mock_svc.build_alert_context.return_value = {}

        with patch(
            "core.observability.data_health_service.DataHealthService",
            return_value=mock_svc,
        ):
            result = check_data_health("/fake/path", "XAUUSDc")

        assert result["alerts"] == ["TEST_WARN", "TEST_FAIL"]

    def test_dispatches_alert_when_hub_has_evaluate_and_dispatch(self) -> None:
        """When alert_hub supports evaluate_and_dispatch, use the new path."""
        mock_report = MagicMock()
        mock_report.generated_at = "2026-06-19T12:00:00Z"
        mock_report.alert_level = "WARNING"
        mock_report.primary_codes = ["TEST_WARN"]
        mock_report.sources = []

        mock_svc = MagicMock()
        mock_svc.run_lightweight.return_value = mock_report
        mock_svc.build_alert_context.return_value = {"key": "value"}

        mock_hub = MagicMock()
        mock_hub.evaluate_and_dispatch = MagicMock()

        with patch(
            "core.observability.data_health_service.DataHealthService",
            return_value=mock_svc,
        ):
            check_data_health("/fake/path", "XAUUSDc", alert_hub=mock_hub)

        mock_hub.evaluate_and_dispatch.assert_called_once_with({"key": "value"})

    def test_falls_back_to_send_warning_for_old_alert_hub(self) -> None:
        """Old alert_hub without evaluate_and_dispatch uses send_warning."""
        mock_report = MagicMock()
        mock_report.generated_at = "2026-06-19T12:00:00Z"
        mock_report.alert_level = "CRITICAL"
        mock_report.primary_codes = ["TEST_CRIT"]
        mock_report.sources = []

        mock_svc = MagicMock()
        mock_svc.run_lightweight.return_value = mock_report
        mock_svc.build_alert_context.return_value = {}

        mock_hub = MagicMock()
        # Old hub: has send_warning but NOT evaluate_and_dispatch
        del mock_hub.evaluate_and_dispatch
        mock_hub.send_warning = MagicMock()

        with patch(
            "core.observability.data_health_service.DataHealthService",
            return_value=mock_svc,
        ):
            check_data_health("/fake/path", "XAUUSDc", alert_hub=mock_hub)

        mock_hub.send_warning.assert_called_once()
        call_args = mock_hub.send_warning.call_args[0]
        assert call_args[0] == "data_health_degraded"

    def test_old_hub_skips_when_alert_level_ok(self) -> None:
        """Old alert_hub only sends warning for WARNING or CRITICAL."""
        mock_report = MagicMock()
        mock_report.generated_at = "2026-06-19T12:00:00Z"
        mock_report.alert_level = "OK"
        mock_report.primary_codes = []
        mock_report.sources = []

        mock_svc = MagicMock()
        mock_svc.run_lightweight.return_value = mock_report
        mock_svc.build_alert_context.return_value = {}

        mock_hub = MagicMock()
        del mock_hub.evaluate_and_dispatch
        mock_hub.send_warning = MagicMock()

        with patch(
            "core.observability.data_health_service.DataHealthService",
            return_value=mock_svc,
        ):
            check_data_health("/fake/path", "XAUUSDc", alert_hub=mock_hub)

        mock_hub.send_warning.assert_not_called()
