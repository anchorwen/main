"""Golden Master recorder — capture (inputs, outputs) pairs for regression testing.

Architecture:  Non-invasive.  Records every live cycle to
``data/golden_master.jsonl`` by default.  Set ``GOLDEN_MASTER_RECORD=0``
to disable.  Each record contains decision-relevant inputs and per-strategy
outputs for one cycle (~2KB).

Replay mode (``GOLDEN_MASTER_REPLAY=1``) compares live outputs against
recorded expectations and logs mismatches — it does NOT block trading.

Default: RECORDING ON.  Opt-out: GOLDEN_MASTER_RECORD=0.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_ENV_DISABLE = "GOLDEN_MASTER_RECORD"  # set to "0" to disable
_DEFAULT_PATH = "data/golden_master.jsonl"


def _is_recording() -> bool:
    # Default ON — only disable when explicitly set to "0"
    return os.environ.get(_ENV_DISABLE) != "0"


def _is_replaying() -> bool:
    return os.environ.get(_ENV_REPLAY) == "1"


def _now_utc() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat()


# ── Recording ───────────────────────────────────────────────────────────────


def record_cycle_inputs(
    *,
    cycle_count: int,
    mid_price: float | None,
    bid: float | None,
    ask: float | None,
    current_atr: float,
    regime_info: dict[str, Any] | None,
    trend_direction: str,
    trend_strength: float,
    macro_regime: str,
    risk_budget_usd: float,
    session_volume_mult: float,
    health_volume_mult: float,
    feature_vector_sample: list[float] | None = None,
    data_dir: str = "data",
) -> dict[str, Any] | None:
    """Capture decision-relevant inputs at the start of strategy evaluation.

    Returns the captured dict (for downstream recording) or None if not recording.
    Caller should pass the return value to :func:`record_cycle_outputs`.
    """
    if not _is_recording():
        return None

    now_ts = time.time()
    fv_sample = (
        [round(float(x), 6) for x in (list(feature_vector_sample)[:8] if hasattr(feature_vector_sample, '__iter__') else [])]
        if feature_vector_sample is not None
        else []
    )
    regime = regime_info.get("regime", "normal") if regime_info else "normal"
    detected_regime = regime_info.get("detected_regime", regime) if regime_info else regime

    return {
        "cycle": cycle_count,
        "timestamp_utc": _now_utc(),
        "now_unix": now_ts,
        "inputs": {
            "mid_price": round(mid_price, 2) if mid_price else None,
            "bid": round(bid, 2) if bid else None,
            "ask": round(ask, 2) if ask else None,
            "spread": round(ask - bid, 4) if (bid and ask and ask > bid) else 0.0,
            "current_atr": round(current_atr, 4),
            "regime": regime,
            "detected_regime": detected_regime,
            "trend_direction": trend_direction,
            "trend_strength": round(trend_strength, 4),
            "macro_regime": macro_regime,
            "risk_budget_usd": round(risk_budget_usd, 2),
            "session_volume_mult": round(session_volume_mult, 4),
            "health_volume_mult": round(health_volume_mult, 4),
            "feature_vector_head8": fv_sample,
        },
    }


def record_cycle_outputs(
    capture: dict[str, Any] | None,
    *,
    strategy_results: dict[str, Any],
    decisions_map: dict[str, Any],
    trade_decisions: int,
    queued: int,
    data_dir: str = "data",
) -> None:
    """Write per-strategy decision outputs and append the full record to disk."""
    if capture is None:
        return

    outputs: dict[str, Any] = {}
    for name, result in (strategy_results or {}).items():
        outputs[name] = {
            "direction": result.get("direction", "neutral"),
            "confidence": round(float(result.get("confidence", 0)), 4),
            "should_trade": bool(result.get("should_trade", False)),
            "reason": str(result.get("reason", "")),
            "volume": round(float(result.get("volume", 0)), 4),
            "sl": round(float(result.get("sl", 0)), 2) if result.get("sl") else 0.0,
            "tp": round(float(result.get("tp", 0)), 2) if result.get("tp") else 0.0,
        }

    capture["outputs"] = outputs
    capture["summary"] = {
        "trade_decisions": trade_decisions,
        "queued": queued,
        "active_strategies": list((strategy_results or {}).keys()),
    }

    _path = Path(data_dir) / "golden_master.jsonl"
    try:
        _path.parent.mkdir(parents=True, exist_ok=True)
        with open(_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(capture, ensure_ascii=False) + "\n")
    except OSError:
        pass  # Disk I/O failure is non-fatal for golden master recording


# ── Replay / Verification ───────────────────────────────────────────────────


def load_records(data_dir: str = "data") -> list[dict[str, Any]]:
    """Load all recorded golden master cycles."""
    _path = Path(data_dir) / "golden_master.jsonl"
    if not _path.exists():
        return []
    records: list[dict[str, Any]] = []
    for line in _path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


# ── Tolerance helpers ───────────────────────────────────────────────────────


def _fuzzy_equal(a: Any, b: Any, *, rel_tol: float = 1e-4, abs_tol: float = 1e-6) -> bool:
    """Compare two values with tolerance for floats."""
    if isinstance(a, float) and isinstance(b, float):
        if a == 0.0 and b == 0.0:
            return True
        return abs(a - b) <= max(rel_tol * max(abs(a), abs(b)), abs_tol)
    if isinstance(a, bool) and isinstance(b, bool):
        return a == b
    if isinstance(a, str) and isinstance(b, str):
        return a == b
    if a == b:
        return True
    # Last resort: stringify
    return str(a) == str(b)


def replay_check_cycle(
    cycle: dict[str, Any],
    live_outputs: dict[str, Any],
    *,
    strict: bool = False,
) -> list[str]:
    """Compare recorded outputs against live outputs for one cycle.

    Returns a list of mismatch descriptions (empty = all good).
    """
    mismatches: list[str] = []
    recorded = cycle.get("outputs", {})
    for strategy, expected in recorded.items():
        actual = live_outputs.get(strategy)
        if actual is None:
            mismatches.append(f"{strategy}: missing in live output")
            continue
        for field in ("direction", "should_trade", "reason"):
            exp_val = expected.get(field)
            act_val = actual.get(field)
            if not _fuzzy_equal(exp_val, act_val):
                mismatches.append(
                    f"{strategy}.{field}: expected={exp_val!r}, got={act_val!r}"
                )
        # Numeric fields with tolerance
        for field in ("confidence", "volume"):
            exp_val = expected.get(field, 0.0)
            act_val = actual.get(field, 0.0)
            if not _fuzzy_equal(float(exp_val), float(act_val), rel_tol=0.01, abs_tol=0.001):
                mismatches.append(
                    f"{strategy}.{field}: expected={exp_val}, got={act_val}"
                )
    # Check for unexpected new strategies
    for strategy in live_outputs:
        if strategy not in recorded:
            mismatches.append(f"{strategy}: new strategy not in golden master")
    return mismatches
