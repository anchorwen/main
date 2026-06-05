"""Data health monitor — proactive training data quality checks + alerts.

FIX-20260604-079: Monitors feature store freshness, journal growth,
and training prerequisite conditions.  Alerts via LiveAlertHub when
data quality degrades or when pending-task conditions are met.

Runs every 60 cycles (~5 hours) in the main loop.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


def _utc_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).replace(tzinfo=None).isoformat()


def check_data_health(
    base_dir: str,
    symbol: str,
    alert_hub: Any = None,
) -> dict[str, Any]:
    """Run all data health checks.  Returns a summary dict.

    Checked:
      - Feature store freshness (last record age)
      - Journal growth (new trades since last check)
      - Training prerequisites (trade count for MetaFilter, retraining)
    """
    report: dict[str, Any] = {
        "time": _utc_iso(),
        "symbol": symbol,
        "checks": {},
    }

    # ── 1. Feature store freshness ──
    _fs_path = os.path.join(base_dir, "feature_store", "records",
                            f"symbol={symbol}", "timeframe=M5", "features.jsonl")
    if os.path.exists(_fs_path):
        try:
            lines = open(_fs_path).readlines()
            if lines:
                last = json.loads(lines[-1])
                ts = last.get("event_time", "")
                if ts:
                    from datetime import datetime, timezone

                    dt = datetime.fromisoformat(str(ts)[:19])
                    age_min = (datetime.now(timezone.utc).replace(tzinfo=None)
                               - dt.replace(tzinfo=None)).total_seconds() / 60
                    report["checks"]["feature_store"] = {
                        "status": "ok" if age_min < 15 else "stale",
                        "age_minutes": round(age_min, 1),
                        "total_records": len(lines),
                    }
        except Exception:
            report["checks"]["feature_store"] = {"status": "error", "age_minutes": -1}
    else:
        report["checks"]["feature_store"] = {"status": "missing"}

    # ── 2. Journal growth ──
    _jl_path = os.path.join(base_dir, "live_trade_journal.jsonl")
    if os.path.exists(_jl_path):
        try:
            lines = open(_jl_path).readlines()
            closes = 0
            for l in lines:
                if '"action": "close"' in l:
                    closes += 1
            report["checks"]["journal"] = {
                "total_lines": len(lines),
                "close_entries": closes,
                "status": "ok",
            }
            # Read persisted last-check count
            _state_path = os.path.join(base_dir, "state", "data_health_state.json")
            if os.path.exists(_state_path):
                prev = json.loads(open(_state_path).read())
                prev_closes = prev.get("last_close_count", 0)
                new_closes = closes - prev_closes
                report["checks"]["journal"]["new_closes_since_last"] = new_closes
                if new_closes >= 50:
                    report["checks"]["training_ready"] = {
                        "metafilter": True,
                        "message": f"{new_closes} new settled trades — MetaFilter training viable",
                    }
            # Persist current count
            try:
                os.makedirs(os.path.dirname(_state_path), exist_ok=True)
                with open(_state_path, "w") as f:
                    json.dump({"last_close_count": closes, "checked_at": _utc_iso()}, f)
            except OSError:
                pass
        except Exception:
            report["checks"]["journal"] = {"status": "error"}
    else:
        report["checks"]["journal"] = {"status": "missing"}

    # ── 3. Alert if issues found ──
    _alerts = []
    for check_name, result in report.get("checks", {}).items():
        if isinstance(result, dict) and result.get("status") in ("stale", "error", "missing"):
            _alerts.append(f"{check_name}: {result.get('status')}")
    if _alerts:
        report["alerts"] = _alerts
        if alert_hub is not None:
            try:
                alert_hub.send_warning(
                    "data_health_degraded",
                    {"checks": report["checks"], "alerts": _alerts},
                )
            except Exception:
                pass

    # ── 4. Training ready notification ──
    _tr = report.get("checks", {}).get("training_ready", {})
    if _tr.get("metafilter"):
        if alert_hub is not None:
            try:
                alert_hub.send_info(
                    "training_condition_met",
                    {"message": _tr.get("message", "")},
                )
            except Exception:
                pass

    return report
