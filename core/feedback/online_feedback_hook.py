"""Online feedback hook — bridges trade outcomes to OnlineLearnerAdapter.partial_fit().

Monitors the live trade journal for newly closed trades, extracts the
corresponding feature vectors from the LocalFeatureStore, and triggers
incremental weight updates on the OnlineLearnerAdapter.

Intended to be called from the daily ops pipeline or as a lightweight
post-trade callback in the live intent loop.
"""

from __future__ import annotations

import bisect
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class OnlineFeedbackHook:
    """Reads closed trades from live_trade_journal.jsonl and updates an
    OnlineLearnerAdapter via partial_fit.

    Matching strategy: for each closed trade with a `close_recorded_at`
    timestamp, find the nearest feature record in LocalFeatureStore within
    max_time_delta_seconds.  If a match is found, add to experience replay
    buffer or call adapter.partial_fit() directly.

    When an ExperienceReplayBuffer is provided and reaches buffer_size,
    samples are expanded by R-multiple weight, shuffled, and fed sequentially
    to avoid catastrophic forgetting from consecutive duplicate gradients.
    """

    def __init__(
        self,
        adapter,  # OnlineLearnerAdapter instance
        journal_path: str = "data/live_trade_journal.jsonl",
        feature_store_dir: str = "data/feature_store/records",
        max_time_delta_seconds: int = 300,
        last_processed_path: str = "data/online_feedback_state.json",
        replay_buffer=None,  # ExperienceReplayBuffer | None
        calibrator=None,  # ConformalCalibrator | None
    ):
        self._adapter = adapter
        self._journal_path = Path(journal_path)
        self._feature_store_dir = Path(feature_store_dir)
        self._max_delta = max_time_delta_seconds
        self._last_processed_path = Path(last_processed_path)
        self._last_processed_at: str | None = None
        self._replay = replay_buffer
        self._calibrator = calibrator
        # In-memory feature index: symbol → sorted list of (unix_ts, values_dict)
        self._feature_cache: dict[str, list[tuple[float, dict[str, float]]]] = {}
        self._load_state()

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _load_state(self) -> None:
        if self._last_processed_path.exists():
            try:
                state = json.loads(self._last_processed_path.read_text(encoding="utf-8"))
                self._last_processed_at = state.get("last_processed_at")
            except Exception:
                self._last_processed_at = None

    def _save_state(self) -> None:
        self._last_processed_path.parent.mkdir(parents=True, exist_ok=True)
        self._last_processed_path.write_text(
            json.dumps(
                {
                    "last_processed_at": self._last_processed_at,
                    "updated_at": datetime.now(UTC).replace(tzinfo=None).isoformat(),
                },
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    # ------------------------------------------------------------------
    # Label extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _trade_label(entry: dict[str, Any]) -> int | None:
        """Extract an integer label from a journal entry.

        Returns:
            1  — tp_hit_first / win (positive outcome)
            -1 — sl_hit_first / loss (negative outcome)
            0  — breakeven / timeout (neutral)
            None — cannot determine (skip)
        """
        # Check top-level label field
        label_str = str(entry.get("label", "")).lower()
        if label_str in ("tp_hit_first", "win"):
            return 1
        if label_str in ("sl_hit_first", "loss"):
            return -1
        if label_str in ("breakeven", "timeout", "neutral"):
            return 0

        # Check detail.label (nested journal structure)
        detail = entry.get("detail", {}) if isinstance(entry.get("detail"), dict) else {}
        detail_label = str(detail.get("label", "")).lower()
        if detail_label in ("tp_hit_first", "win"):
            return 1
        if detail_label in ("sl_hit_first", "loss"):
            return -1

        # Fallback: use PnL sign (top-level or detail.pnl)
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

    # ------------------------------------------------------------------
    # Feature lookup
    # ------------------------------------------------------------------

    def _build_feature_index(self, symbol: str) -> None:
        """Load and index features.jsonl for a symbol (cached in-memory)."""
        feat_dir = self._feature_store_dir / f"symbol={symbol}" / "timeframe=M5"
        feat_file = feat_dir / "features.jsonl"
        if not feat_file.exists():
            self._feature_cache[symbol] = []
            return
        rows: list[tuple[float, dict[str, float]]] = []
        for line in feat_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            event_time = rec.get("event_time", "")
            if not event_time or str(rec.get("symbol", "")) != symbol:
                continue
            try:
                et = datetime.fromisoformat(str(event_time).replace("Z", "+00:00"))
            except (ValueError, TypeError):
                continue
            values = rec.get("values")
            if isinstance(values, dict):
                rows.append((et.timestamp(), values))
        rows.sort(key=lambda x: x[0])
        self._feature_cache[symbol] = rows

    def _find_feature_vector(
        self, close_time_iso: str, symbol: str = "XAUUSDc"
    ) -> dict[str, float] | None:
        """Find the nearest feature record using bisect on in-memory index."""
        try:
            dt = datetime.fromisoformat(close_time_iso.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None

        if symbol not in self._feature_cache:
            self._build_feature_index(symbol)

        rows = self._feature_cache.get(symbol, [])
        if not rows:
            return None

        target_ts = dt.timestamp()
        timestamps = [r[0] for r in rows]
        idx = bisect.bisect_left(timestamps, target_ts)

        best_row = None
        best_delta = float("inf")

        # Check the insertion point and the one before it
        candidates = []
        if idx < len(rows):
            candidates.append(rows[idx])
        if idx > 0:
            candidates.append(rows[idx - 1])

        for ts, values in candidates:
            delta = abs(ts - target_ts)
            if delta < best_delta:
                best_delta = delta
                best_row = values

        return best_row if best_delta <= self._max_delta else None

    # ------------------------------------------------------------------
    # R-multiple extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_pnl_volume(entry: dict[str, Any]) -> tuple[float, float]:
        """Extract (pnl, volume) from a closed-trade journal entry.

        PnL is the realised profit/loss in account currency.  Volume is the
        position size in lots.  Used to compute approximate R-multiple for
        sample weighting.
        """
        pnl = entry.get("pnl")
        if pnl is None and isinstance(entry.get("detail"), dict):
            pnl = entry["detail"].get("pnl")
        try:
            pnl_f = float(pnl) if pnl is not None else 0.0
        except (ValueError, TypeError):
            pnl_f = 0.0

        try:
            volume = float(entry.get("volume", 0.01))
        except (ValueError, TypeError):
            volume = 0.01

        return pnl_f, max(volume, 0.01)

    # ------------------------------------------------------------------
    # Main processing loop
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_dt(ts: str) -> datetime | None:
        """Parse an ISO timestamp string to a UTC datetime, returning None on failure."""
        try:
            return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None

    def process_new_trades(self, *, save_weights: bool = True) -> dict[str, Any]:
        """Scan journal for new closed trades since last_processed_at.

        With a replay buffer: collects trades into buffer, flushes a shuffled
        mini-batch when buffer_size is reached, then calls partial_fit
        sequentially (shuffle prevents catastrophic forgetting).

        Without a replay buffer: calls adapter.partial_fit() directly for each
        trade (legacy single-sample behaviour).

        Returns a summary dict with counts.
        """
        if not self._journal_path.exists():
            return {"status": "no_journal", "updated": 0, "skipped": 0, "errors": 0}

        # ── Parse all entries ──────────────────────────────────────────
        entries: list[dict[str, Any]] = []
        for line in self._journal_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

        # ── Build open-order timestamp index (message_id → recorded_at) ─
        open_order_times: dict[str, str] = {}
        for entry in entries:
            if str(entry.get("ack_status", "")) != "closed":
                msg_id = str(entry.get("message_id", ""))
                if msg_id:
                    open_order_times[msg_id] = str(entry.get("recorded_at", ""))

        # ── Parse last_processed_at for datetime comparison (C4 fix) ───
        last_processed_dt: datetime | None = None
        if self._last_processed_at:
            last_processed_dt = self._parse_dt(self._last_processed_at)

        collected = 0
        matched = 0
        skipped = 0
        errors = 0
        latest_processed = self._last_processed_at
        latest_processed_dt = last_processed_dt

        for entry in entries:
            recorded_at = str(entry.get("recorded_at", ""))
            # ── C4: datetime comparison, not string comparison ──────────
            entry_dt = self._parse_dt(recorded_at)
            if last_processed_dt is not None and entry_dt is not None:
                if entry_dt <= last_processed_dt:
                    continue
            elif self._last_processed_at and recorded_at and recorded_at <= self._last_processed_at:
                continue  # fallback for non-standard timestamps

            ack_status = str(entry.get("ack_status", ""))
            if ack_status != "closed":
                skipped += 1
                continue

            label = self._trade_label(entry)
            if label is None:
                skipped += 1
                continue

            # ── C1: use entry_time (open order recorded_at) not close_time ──
            open_msg_id = str(entry.get("open_message_id", ""))
            entry_time = open_order_times.get(open_msg_id, "")
            if not entry_time:
                # Fallback: use close recorded_at (suboptimal but best available)
                entry_time = str(
                    entry.get("close_recorded_at")
                    or entry.get("close_time")
                    or entry.get("recorded_at", "")
                )
            symbol = str(entry.get("symbol", "XAUUSDc"))
            features = self._find_feature_vector(entry_time, symbol)

            if features is None:
                skipped += 1
                logger.debug(
                    "OnlineFeedbackHook: no feature match for trade %s at %s",
                    entry.get("message_id", "?"),
                    entry_time,
                )
                continue

            matched += 1
            trade_id = str(entry.get("message_id", ""))

            # ── Update conformal calibrator (Track 3d) ──
            if self._calibrator is not None:
                p_win = entry.get("p_win")
                if p_win is None and isinstance(entry.get("detail"), dict):
                    p_win = entry["detail"].get("p_win")
                if p_win is not None:
                    try:
                        self._calibrator.update(float(p_win), label)
                    except Exception:
                        pass  # non-critical — calibrator update failure must not block feedback

            if self._replay is not None:
                # ── Replay buffer path ──
                try:
                    import numpy as np

                    feat_arr = np.array(list(features.values()), dtype=np.float64)
                    pnl, volume = self._extract_pnl_volume(entry)
                    self._replay.add(feat_arr, label, pnl, volume, trade_id=trade_id)
                    collected += 1
                except Exception:
                    errors += 1
                    logger.exception("OnlineFeedbackHook: buffer add failed for trade %s", trade_id)
            else:
                # ── Legacy direct partial_fit path ──
                try:
                    import numpy as np

                    feat_arr = np.array(list(features.values()), dtype=np.float64)
                    success = self._adapter.partial_fit(feat_arr, label)
                    if success:
                        collected += 1
                        logger.info(
                            "OnlineFeedbackHook: updated adapter with label=%d (trade=%s)",
                            label,
                            trade_id,
                        )
                    else:
                        errors += 1
                except Exception:
                    errors += 1
                    logger.exception("OnlineFeedbackHook: update failed for trade %s", trade_id)

            # ── C4: datetime comparison for latest_processed tracking ──
            if entry_dt is not None and (
                latest_processed_dt is None or entry_dt > latest_processed_dt
            ):
                latest_processed = recorded_at
                latest_processed_dt = entry_dt
            elif entry_dt is None and recorded_at and recorded_at > (latest_processed or ""):
                latest_processed = recorded_at  # fallback for non-standard timestamps

        # ── Flush replay buffer if ready ──
        flushed_count = 0
        if self._replay is not None and self._replay.is_ready():
            try:
                batch = self._replay.flush()
                for feat, lbl in batch:
                    self._adapter.partial_fit(feat, lbl)
                flushed_count = len(batch)
                logger.info(
                    "OnlineFeedbackHook: flushed mini-batch — %d samples from %d trades",
                    flushed_count,
                    len(batch),
                )
            except Exception:
                logger.exception("OnlineFeedbackHook: mini-batch flush failed")

        self._last_processed_at = latest_processed
        self._save_state()

        updated = flushed_count if self._replay is not None else collected
        if save_weights and updated > 0:
            self._adapter.save_weights()

        summary = {
            "status": "ok",
            "collected": collected,
            "matched": matched,
            "skipped": skipped,
            "errors": errors,
            "flushed": flushed_count,
            "updated": updated,
            "buffer_size": self._replay.size if self._replay else 0,
        }
        logger.info("OnlineFeedbackHook: %s", summary)
        return summary


def run_online_feedback(
    adapter,
    base_dir: str = "data",
    *,
    save_weights: bool = True,
    replay_buffer=None,
    calibrator=None,
) -> dict[str, Any]:
    """Convenience entry point — create a hook and process new trades."""
    hook = OnlineFeedbackHook(
        adapter=adapter,
        journal_path=f"{base_dir}/live_trade_journal.jsonl",
        feature_store_dir=f"{base_dir}/feature_store/records",
        replay_buffer=replay_buffer,
        calibrator=calibrator,
    )
    return hook.process_new_trades(save_weights=save_weights)
