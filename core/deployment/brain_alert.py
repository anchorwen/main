"""Structured brain alerts — high-visibility JSON events for any fallback/degradation.

Every silent failure path in the brain inference pipeline must emit a brain_alert
so operators can detect problems without grepping logs.

Alert types:
  - feature_dimension_mismatch: Feature vector dimension != model expected dim
  - model_load_failed: Model artifact could not be loaded
  - feature_missing: Feature key missing, zero-filled
  - normalization_unavailable: Normalization config missing or failed
  - inference_returned_zero: Inference produced all-zero / neutral output
  - brain_stub_mode: Brain running in stub/fallback mode
  - config_validation_error: Brain config failed validation
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime


def _utc_iso() -> str:
    return datetime.now(UTC).isoformat()


def emit_brain_alert(brain_id: str, alert_type: str, detail: dict | None = None) -> None:
    """Emit a structured brain alert to stderr.

    Printed as a single JSON line to stderr so it never corrupts stdout JSON
    output (e.g. shadow CLI).  Operators can filter::

        grep '"event":"brain_alert"' journal.log | jq .
    """
    payload = {
        "event": "brain_alert",
        "time": _utc_iso(),
        "brain_id": brain_id,
        "alert_type": alert_type,
        "detail": detail or {},
    }
    print(json.dumps(payload, ensure_ascii=False), file=sys.stderr, flush=True)
