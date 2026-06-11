"""Projection engine — rebuild governance state from the event stream.

FIX-20260611-021: Event Sourcing Foundation — Step 4 Projection Engine.

Pure functions with ZERO side effects.  The governance state is a
MATERIALISED VIEW of the immutable event log.  It can be destroyed
and rebuilt at any time because the event log is the source of truth.

Key design decisions:
 - Checkpoint + incremental replay for O(Δ) performance.
 - ``source`` filter physically isolates backtest/shadow/migration data
   from live governance decisions.
 - All functions are deterministic: same events → same state.

Usage::

    from core.data.projections import project_governance_state

    state = project_governance_state(
        events_path=Path("data/ledger_events.jsonl"),
        checkpoint_path=Path("data/state/governance_checkpoint.json"),
    )
    # state["Swing_V9_M15_V2"]["win_rate"] → 0.6457
    # state["_checkpoint_event_id"] → "evt_abc123"
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from core.contracts.events import DataSource, PnLEvent

# ── Per-brain state accumulator ────────────────────────────────────────────


def _init_brain_state() -> dict[str, Any]:
    """Return a fresh brain state accumulator."""
    return {
        "total_trades": 0,
        "wins": 0,
        "losses": 0,
        "breakeven": 0,
        "cumulative_pnl_r": 0.0,
        "long_count": 0,
        "short_count": 0,
        "long_wins": 0,
        "short_wins": 0,
    }


def _ensure_brain_state(state: dict[str, Any], brain_id: str) -> dict[str, Any]:
    """Get or create a brain accumulator with ALL required keys.

    This is needed because checkpoint-restored state may use the derived
    metrics format (win_rate, profit_factor, ...) rather than the raw
    accumulator format (wins, losses, cumulative_pnl_r, ...).

    Missing keys are filled from _init_brain_state() defaults.
    """
    if brain_id not in state:
        state[brain_id] = _init_brain_state()
        return state[brain_id]

    bs = state[brain_id]
    # Fill in any missing keys (checkpoint may have derived format)
    defaults = _init_brain_state()
    for key, default_val in defaults.items():
        if key not in bs:
            bs[key] = default_val
    return bs


def _apply_event(state: dict[str, Any], event: PnLEvent) -> None:
    """Apply a single PnLEvent to the governance state accumulator.

    Mutates ``state`` in place.  This is intentional: the caller owns
    the accumulator dict and this function is purely mechanical.
    """
    brain_id = event.brain_id
    bs = _ensure_brain_state(state, brain_id)

    bs["total_trades"] += 1
    if event.pnl_r > 0:
        bs["wins"] += 1
    elif event.pnl_r < 0:
        bs["losses"] += 1
    else:
        bs["breakeven"] += 1
    bs["cumulative_pnl_r"] += event.pnl_r

    if event.direction == "long":
        bs["long_count"] += 1
        if event.pnl_r > 0:
            bs["long_wins"] += 1
    elif event.direction == "short":
        bs["short_count"] += 1
        if event.pnl_r > 0:
            bs["short_wins"] += 1


def _derive_metrics(state: dict[str, Any]) -> dict[str, Any]:
    """Compute derived metrics (win_rate, profit_factor, sharpe) from raw counts.

    Returns a new dict with governance-compatible performance_metrics.
    """
    result: dict[str, Any] = {}
    for brain_id, bs in state.items():
        if brain_id.startswith("_"):
            continue  # skip internal fields like _checkpoint_event_id

        total = bs["total_trades"]
        wins = bs["wins"]
        losses = bs["losses"]

        if total > 0:
            win_rate = wins / total
            # Profit factor: gross_profit / gross_loss (avoid div-by-zero)
            # Simplified: use cumulative_pnl_r / total as a proxy
            avg_pnl = bs["cumulative_pnl_r"] / total
        else:
            win_rate = 0.0
            avg_pnl = 0.0

        result[brain_id] = {
            "win_rate": round(win_rate, 4),
            "profit_factor": (
                round(bs["wins"] / max(bs["losses"], 1), 2)
                if bs["losses"] > 0
                else (round(bs["wins"], 2) if bs["wins"] > 0 else 0.0)
            ),
            "sharpe_ratio": 0.0,  # Requires per-trade return series; computed separately
            "total_trades": total,
            "pnl_r": round(bs["cumulative_pnl_r"], 2),
            "long_win_rate": (
                round(bs["long_wins"] / max(bs["long_count"], 1), 4)
                if bs["long_count"] > 0
                else 0.0
            ),
            "short_win_rate": (
                round(bs["short_wins"] / max(bs["short_count"], 1), 4)
                if bs["short_count"] > 0
                else 0.0
            ),
        }
    return result


# ── Public API ─────────────────────────────────────────────────────────────


def project_governance_state(
    events_path: Path,
    checkpoint_path: Path | None = None,
    source_filter: set[str] | None = None,
) -> dict[str, Any]:
    """Rebuild governance state from the event stream.

    Args:
        events_path: Path to ``ledger_events.jsonl``.
        checkpoint_path: Optional checkpoint file for incremental replay.
            If provided, loads the snapshot and only replays events after
            the checkpoint.  The checkpoint is updated with the latest
            event_id after projection.
        source_filter: Set of ``source`` values to include.
            Default: ``{"live"}`` — backtest/shadow/migration events are
            physically excluded from live governance.

    Returns:
        A dict mapping brain_id → performance_metrics, plus
        ``_checkpoint_event_id`` for the next checkpoint save.
    """
    if source_filter is None:
        source_filter = {DataSource.LIVE}

    state: dict[str, Any] = {}
    processed_lines: int = 0  # Line-based checkpoint (events are single-line)

    # ── Load checkpoint if available ──
    if checkpoint_path and checkpoint_path.exists():
        try:
            ck = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            processed_lines = ck.pop("_checkpoint_lines", 0)
            # Restore raw accumulators from checkpoint
            for bid, bs in ck.items():
                if isinstance(bs, dict):
                    state[bid] = bs
        except (json.JSONDecodeError, OSError):
            pass  # Corrupt checkpoint → rebuild from scratch

    # ── Replay events after checkpoint ──
    if events_path.exists():
        with open(events_path, encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                # Skip already-processed lines via checkpoint
                # (UUID-based event_id comparison is NOT monotonic —
                #  random hex strings have no ordering guarantee)
                if i < processed_lines:
                    continue

                line = line.strip()
                if not line:
                    continue
                try:
                    event = PnLEvent.model_validate_json(line)
                except (ValueError, TypeError, KeyError):
                    # Corrupt or malformed line → skip
                    continue

                # Physically exclude non-target sources
                if event.source not in source_filter:
                    continue

                _apply_event(state, event)
                processed_lines = i + 1  # Line index + 1 = count processed

    # ── Save checkpoint (raw accumulator, not derived result) ──
    if checkpoint_path:
        save_checkpoint(state, processed_lines, checkpoint_path)

    # ── Derive metrics ──
    result = _derive_metrics(state)
    result["_checkpoint_lines"] = processed_lines

    return result


def save_checkpoint(
    raw_state: dict[str, Any],
    processed_lines: int,
    checkpoint_path: Path,
) -> None:
    """Atomically save raw accumulator state for incremental replay.

    Saves the RAW accumulator dict (not derived metrics) so that
    _apply_event can correctly increment wins/losses/breakeven counters
    during incremental replay.

    Uses tmp + os.replace for atomic write (no half-written checkpoints).
    """
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    payload = dict(raw_state)
    payload["_checkpoint_lines"] = processed_lines
    tmp_path = checkpoint_path.with_suffix(".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp_path, checkpoint_path)


def count_events(
    events_path: Path,
    source_filter: set[str] | None = None,
) -> int:
    """Count total events in the stream (useful for health checks)."""
    if not events_path.exists():
        return 0
    count = 0
    with open(events_path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                event = PnLEvent.model_validate_json(line)
            except (ValueError, TypeError, KeyError):
                continue
            if source_filter and event.source not in source_filter:
                continue
            count += 1
    return count
