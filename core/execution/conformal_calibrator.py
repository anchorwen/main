"""Conformal prediction calibrator for OU MetaFilterGate (Track 3d).

Maintains a rolling window of (p_win, outcome) pairs from closed trades
and computes an adaptive threshold from the empirical P(win) distribution.

Design constraints (per chief architect review):
  - Simple FIFO deque (maxlen=500) — no EMA-weighted quantiles for MVP.
    Time decay is achieved via oldest-sample eviction.
  - Q10 (10th percentile) is the target quantile — intentionally lenient
    to avoid survivorship-bias drift.  The journal only contains outcomes
    from signals that *passed* a prior threshold, so the observed P(win)
    distribution is left-truncated.  Using a low percentile keeps the
    adaptive threshold close to the base value rather than inflating it.
  - Clamp: [0.35, 0.70] with clamp-hit-rate monitoring.
  - Warmup: first 50 samples return base_threshold (no adaptation).
  - JSON persistence so the calibrator survives process restarts.
"""

from __future__ import annotations

import json
import logging
from collections import deque
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

# ── Constants ──
DEFAULT_WINDOW_SIZE = 500
DEFAULT_WARMUP_SAMPLES = 50
DEFAULT_BASE_THRESHOLD = 0.40
DEFAULT_MIN_THRESHOLD = 0.35
DEFAULT_MAX_THRESHOLD = 0.70
DEFAULT_TARGET_PERCENTILE = 10.0  # Q10 — lenient, anti-survivorship-bias
DEFAULT_STATE_PATH = "data/conformal_calibrator_state.json"


class ConformalCalibrator:
    """Rolling-window conformal threshold for OU signal gating.

    Each call to :meth:`update` appends a (p_win, label) tuple.  Once the
    warmup window is full, :meth:`compute_threshold` returns the Q10 of
    the rolling P(win) distribution, clamped to [min, max].

    The Q10 choice is deliberate: because we only observe outcomes for
    signals that passed a previous threshold, the empirical P(win)
    distribution is left-truncated.  A low percentile keeps the adaptive
    threshold near the base value and avoids the self-inflation death
    spiral that disabled Track 4d conformal (FIX-20260523-003).
    """

    def __init__(
        self,
        window_size: int = DEFAULT_WINDOW_SIZE,
        warmup_samples: int = DEFAULT_WARMUP_SAMPLES,
        base_threshold: float = DEFAULT_BASE_THRESHOLD,
        min_threshold: float = DEFAULT_MIN_THRESHOLD,
        max_threshold: float = DEFAULT_MAX_THRESHOLD,
        target_percentile: float = DEFAULT_TARGET_PERCENTILE,
        state_path: str = DEFAULT_STATE_PATH,
    ) -> None:
        if not 0.0 < target_percentile <= 50.0:
            raise ValueError(f"target_percentile must be in (0, 50], got {target_percentile}")
        if min_threshold >= max_threshold:
            raise ValueError(
                f"min_threshold ({min_threshold}) must be < max_threshold ({max_threshold})"
            )

        self._window_size = window_size
        self._warmup_samples = warmup_samples
        self._base_threshold = base_threshold
        self._min_threshold = min_threshold
        self._max_threshold = max_threshold
        self._target_percentile = target_percentile
        self._state_path = Path(state_path)

        # Rolling history: (p_win: float, label: int, timestamp_utc: str)
        self._history: deque[tuple[float, int, str]] = deque(maxlen=window_size)

        # Clamp hit counters (diagnostic)
        self._clamp_hits_upper: int = 0
        self._clamp_hits_lower: int = 0
        self._total_computations: int = 0
        self._cold_started: bool = False

        # Batched persistence: save every N updates instead of per-update
        self._save_interval: int = 10
        self._updates_since_save: int = 0

        self._load_state()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(self, p_win: float, label: int, *, timestamp_utc: str = "") -> None:
        """Record a closed-trade (p_win, outcome) pair.

        Called by OnlineFeedbackHook when a trade closes.

        Args:
            p_win: The P(breakeven) probability that was predicted when
                   the signal was dispatched.
            label: -1 (loss), 0 (breakeven), or 1 (win).
            timestamp_utc: ISO-format UTC timestamp for traceability.
        """
        if timestamp_utc == "":
            from datetime import UTC, datetime

            timestamp_utc = datetime.now(UTC).replace(tzinfo=None).isoformat()

        self._history.append((float(p_win), int(label), str(timestamp_utc)))
        self._updates_since_save += 1
        if self._updates_since_save >= self._save_interval:
            self._save_state()
            self._updates_since_save = 0

    def compute_threshold(self) -> float:
        """Return the adaptive conformal threshold.

        Before warmup (50 samples) the base_threshold is returned
        unchanged.  After warmup, the threshold is:

            threshold = clip(max(Q10, base_threshold), min, max)

        where Q10 is the 10th percentile of P(win) values in the
        rolling window.

        Returns:
            float — adaptive threshold in [min_threshold, max_threshold].
        """
        self._total_computations += 1

        if len(self._history) < self._warmup_samples:
            return self._base_threshold

        p_wins = [p for p, _, _ in self._history]
        if not p_wins:
            return self._base_threshold

        q = float(np.percentile(p_wins, self._target_percentile))

        # max(q, base) keeps threshold from falling below the original
        # fixed gate.  In practice q ≥ base for the truncated
        # distribution, but this guard handles edge cases.
        threshold = max(q, self._base_threshold)
        threshold = float(np.clip(threshold, self._min_threshold, self._max_threshold))

        # ── clamp-hit monitoring ──
        if threshold >= self._max_threshold:
            self._clamp_hits_upper += 1
            if self._clamp_hits_upper % 10 == 1:
                logger.warning(
                    "ConformalCalibrator: threshold clamped at max %.2f for %d/%d "
                    "computations — base LGB distribution may have degraded",
                    self._max_threshold,
                    self._clamp_hits_upper,
                    self._total_computations,
                )
        elif threshold <= self._min_threshold:
            self._clamp_hits_lower += 1

        return threshold

    @property
    def is_warm(self) -> bool:
        """True when enough samples have accumulated for adaptation."""
        return len(self._history) >= self._warmup_samples

    @property
    def sample_count(self) -> int:
        return len(self._history)

    # ------------------------------------------------------------------
    # Cold-start: seed from journal history
    # ------------------------------------------------------------------

    def cold_start_from_journal(self, journal_path: str) -> int:
        """Seed the rolling window from historical closed trades.

        Reads live_trade_journal.jsonl and extracts (p_win, label)
        pairs for all closed trades.  This lets the calibrator start
        with a meaningful distribution rather than waiting 50+ trades.

        *p_win* is recorded on *accepted* (open) entries while *label*
        is on *closed* entries.  This method JOINs the two via
        ``open_message_id`` → ``message_id`` so closed trades inherit
        the prediction confidence from their open order.

        Only entries with ack_status == "closed" and a valid label
        are included.

        Returns:
            Number of samples loaded.
        """
        if self._cold_started:
            return 0

        jp = Path(journal_path)
        if not jp.exists():
            logger.info("ConformalCalibrator: journal not found at %s", journal_path)
            return 0

        # ── Pass 1: build p_win lookup from accepted entries ──
        p_win_by_msg_id: dict[str, float] = {}
        for line in jp.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("ack_status") != "accepted":
                continue
            _pw = entry.get("p_win")
            if _pw is None:
                continue
            _mid = entry.get("message_id")
            if _mid:
                p_win_by_msg_id[_mid] = float(_pw)

        # ── Pass 2: JOIN closed entries → p_win via open_message_id ──
        loaded = 0
        for line in jp.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            if entry.get("ack_status") != "closed":
                continue

            # JOIN: closed.open_message_id → accepted.message_id
            p_win = entry.get("p_win")
            if p_win is None:
                detail = entry.get("detail", {})
                if isinstance(detail, dict):
                    p_win = detail.get("p_win")
            if p_win is None:
                _open_mid = entry.get("open_message_id")
                if _open_mid:
                    p_win = p_win_by_msg_id.get(_open_mid)
            if p_win is None:
                continue

            label = _journal_entry_label(entry)
            if label is None:
                continue

            ts = str(entry.get("recorded_at", ""))
            self._history.append((float(p_win), int(label), ts))
            loaded += 1

        self._cold_started = True
        if loaded > 0:
            self._save_state()
            logger.info(
                "ConformalCalibrator: cold-started with %d samples from %s",
                loaded,
                journal_path,
            )
        return loaded

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def _save_state(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        serializable: list[dict[str, Any]] = []
        for p_win, label, ts in self._history:
            serializable.append({"p_win": p_win, "label": label, "timestamp": ts})

        # FIX-20260610-007: Merge with existing on-disk state to prevent
        # total_computations from being zeroed out.  Two ConformalCalibrator
        # instances share this state file: the live-cycle instance (which
        # calls compute_threshold() and increments in-memory) and the
        # daily_ops instance (which calls update() but never compute_threshold()).
        # Without merging, daily_ops overwrites the live-cycle counter with 0.
        _total = self._total_computations
        _clamp_up = self._clamp_hits_upper
        _clamp_lo = self._clamp_hits_lower
        if self._state_path.exists():
            try:
                _old = json.loads(self._state_path.read_text(encoding="utf-8"))
                _total = max(_total, int(_old.get("total_computations", 0)))
                _clamp_up = max(_clamp_up, int(_old.get("clamp_hits_upper", 0)))
                _clamp_lo = max(_clamp_lo, int(_old.get("clamp_hits_lower", 0)))
            except (json.JSONDecodeError, OSError, ValueError, TypeError):
                pass  # corrupt state → use in-memory values

        # FIX-20260612-005: Transition cold_started → False when calibrator
        # has accumulated enough history.  Previously cold_started stayed True
        # forever (set by cold_start_from_journal(), never cleared), causing
        # CONFORMAL_COLD_STALLED even with 51+ history entries.  The meaningful
        # signal is history count, not computation count — history is what the
        # Q10 percentile is computed FROM.
        _cold = self._cold_started
        if _cold and len(serializable) >= self._warmup_samples:
            _cold = False

        self._state_path.write_text(
            json.dumps(
                {
                    "history": serializable,
                    "clamp_hits_upper": _clamp_up,
                    "clamp_hits_lower": _clamp_lo,
                    "total_computations": _total,
                    "cold_started": _cold,
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    def _load_state(self) -> None:
        if not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return

        self._clamp_hits_upper = int(data.get("clamp_hits_upper", 0))
        self._clamp_hits_lower = int(data.get("clamp_hits_lower", 0))
        self._total_computations = int(data.get("total_computations", 0))
        self._cold_started = bool(data.get("cold_started", False))

        # FIX-20260612-005: Backfill cold_started transition for state files
        # written before this fix.  If we already have enough history, we're
        # not cold — regardless of what the file says.
        _loaded_history = data.get("history", [])
        if self._cold_started and len(_loaded_history) >= self._warmup_samples:
            self._cold_started = False

        for item in data.get("history", []):
            self._history.append(
                (
                    float(item["p_win"]),
                    int(item["label"]),
                    str(item.get("timestamp", "")),
                )
            )

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def describe(self) -> dict[str, Any]:
        p_wins = [p for p, _, _ in self._history] if self._history else []
        return {
            "sample_count": len(self._history),
            "is_warm": self.is_warm,
            "base_threshold": self._base_threshold,
            "min_threshold": self._min_threshold,
            "max_threshold": self._max_threshold,
            "target_percentile": self._target_percentile,
            "current_threshold": self.compute_threshold(),
            "p_win_median": round(float(np.median(p_wins)), 4) if p_wins else None,
            "p_win_q10": round(float(np.percentile(p_wins, 10)), 4) if p_wins else None,
            "p_win_q25": round(float(np.percentile(p_wins, 25)), 4) if p_wins else None,
            "p_win_min": round(min(p_wins), 4) if p_wins else None,
            "p_win_max": round(max(p_wins), 4) if p_wins else None,
            "clamp_hits_upper": self._clamp_hits_upper,
            "clamp_hits_lower": self._clamp_hits_lower,
            "total_computations": self._total_computations,
            "cold_started": self._cold_started,
        }


# ------------------------------------------------------------------
# Helper: label extraction (mirrors OnlineFeedbackHook._trade_label)
# ------------------------------------------------------------------


def _journal_entry_label(entry: dict[str, Any]) -> int | None:
    """Extract integer label from a closed journal entry.

    Returns 1 (win), -1 (loss), 0 (breakeven), or None (cannot determine).
    """
    label_str = str(entry.get("label", "")).lower()
    if label_str in ("tp_hit_first", "win"):
        return 1
    if label_str in ("sl_hit_first", "loss"):
        return -1
    if label_str in ("breakeven", "timeout", "neutral"):
        return 0

    detail = entry.get("detail", {}) if isinstance(entry.get("detail"), dict) else {}
    detail_label = str(detail.get("label", "")).lower()
    if detail_label in ("tp_hit_first", "win"):
        return 1
    if detail_label in ("sl_hit_first", "loss"):
        return -1

    pnl = entry.get("pnl")
    if pnl is None and isinstance(detail, dict):
        pnl = detail.get("pnl")
    if pnl is not None:
        try:
            pnl_f = float(pnl)
            if pnl_f > 0:
                return 1
            if pnl_f < 0:
                return -1
            return 0
        except (ValueError, TypeError):
            pass
    return None
