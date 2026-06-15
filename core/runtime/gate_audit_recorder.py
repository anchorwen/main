"""Gate audit recorder — structured per-cycle gate decision logging.

Writes one JSONL line per blocked trade so operators can answer
"which gate is killing my signals?" without grepping print() output.

Output: ``data/gate_audit/YYYY-MM-DD.jsonl``

Usage::

    from core.runtime.gate_audit_recorder import record_gate_block

    record_gate_block(
        strategy_name="statarb_dynamic",
        direction="long",
        reason="score_0.1100_lt_threshold_0.3500",
        gate_diag={"z_score": -0.42, "composite_score": 0.11, "threshold": 0.35},
    )
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def record_gate_block(
    *,
    strategy_name: str,
    direction: str,
    reason: str,
    base_dir: str,  # FIX-20260615-006/C8: required — no default
    gate_diag: dict[str, Any] | None = None,
    symbol: str = "XAUUSDc",
) -> None:
    """Record one gate-block event to the daily audit file.

    Thread-safe via append mode.  Skips silently on any error
    so gate audit never breaks the hot path.
    """
    try:
        now = datetime.now(UTC)
        date_str = now.strftime("%Y-%m-%d")
        audit_dir = Path(base_dir) / "gate_audit"
        audit_dir.mkdir(parents=True, exist_ok=True)

        record: dict[str, Any] = {
            "ts": now.isoformat(),
            "strategy": strategy_name,
            "direction": direction,
            "reason": reason,
        }
        if gate_diag:
            record["diag"] = gate_diag

        audit_path = audit_dir / f"{date_str}.jsonl"
        with open(audit_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:  # noqa: BLE001
        pass  # audit recording is best-effort, not critical path
