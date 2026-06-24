"""DEPRECATED — backward-compatible shim for check_data_health().

FIX-20260610-005: Delegates to core.observability.data_health_service.
All new code should use DataHealthService directly.

Original FIX-20260604-079 created this module as the first dedicated data
health monitor.  The new DataHealthService (FIX-20260610-005) replaces it
with a unified, decorator-based, alert-integrated design.
"""

from __future__ import annotations

from typing import Any


def check_data_health(
    base_dir: str,
    symbol: str,
    alert_hub: Any = None,
    position_manager: Any = None,  # FIX-20260611-002
) -> dict[str, Any]:
    """DEPRECATED — delegates to DataHealthService.run_lightweight().

    Returns a backward-compatible dict matching the original format so
    existing callers in live_intent_loop.py continue to work without
    immediate code changes.
    """
    try:
        from core.observability.data_health_service import DataHealthService

        svc = DataHealthService(
            base_dir=base_dir,
            symbol=symbol,
            mode="light",
            position_manager=position_manager,
        )
        report = svc.run_lightweight()
        svc.save_health_state(report)

        # Feed context through alert system instead of bypassing it
        if alert_hub is not None:
            try:
                ctx = svc.build_alert_context(report)
                if hasattr(alert_hub, "evaluate_and_dispatch"):
                    alert_hub.evaluate_and_dispatch(ctx)
                elif hasattr(alert_hub, "send_warning"):
                    if report.alert_level in ("WARNING", "CRITICAL"):
                        alert_hub.send_warning(
                            "data_health_degraded",
                            {
                                "alert_level": report.alert_level,
                                "primary_codes": report.primary_codes,
                            },
                        )
            except (RuntimeError, ValueError, KeyError, TypeError, OSError):
                pass
        return {
            "time": report.generated_at,
            "symbol": symbol,
            "checks": {s.source: {"status": s.status.value, **s.metrics} for s in report.sources},
            "alerts": [
                s.primary_code
                for s in report.sources
                if s.status.value in ("warn", "fail", "missing")
            ],
        }
    except (RuntimeError, ValueError, KeyError, TypeError, OSError):
        return {"time": "", "symbol": symbol, "checks": {}, "alerts": ["data_health_service_error"]}
