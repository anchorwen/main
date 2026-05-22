"""Online feedback hook — bridges trade outcomes to OnlineLearnerAdapter.partial_fit().

Monitors the live trade journal for newly closed trades, extracts the
corresponding feature vectors from the LocalFeatureStore, and triggers
incremental weight updates on the OnlineLearnerAdapter.

Intended to be called from the daily ops pipeline or as a lightweight
post-trade callback in the live intent loop.
"""

from __future__ import annotations

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
    max_time_delta_seconds.  If a match is found, call adapter.partial_fit().
    """

    def __init__(
        self,
        adapter,  # OnlineLearnerAdapter instance
        journal_path: str = "data/live_trade_journal.jsonl",
        feature_store_dir: str = "data/feature_store/records",
        max_time_delta_seconds: int = 300,
        last_processed_path: str = "data/online_feedback_state.json",
    ):
        self._adapter = adapter
        self._journal_path = Path(journal_path)
        self._feature_store_dir = Path(feature_store_dir)
        self._max_delta = max_time_delta_seconds
        self._last_processed_path = Path(last_processed_path)
        self._last_processed_at: str | None = None
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

    def _find_feature_vector(
        self, close_time_iso: str, symbol: str = "XAUUSDc"
    ) -> dict[str, float] | None:
        """Load feature records for the given date and find the nearest in time."""
        try:
            dt = datetime.fromisoformat(close_time_iso.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            return None

        feat_dir = self._feature_store_dir / f"symbol={symbol}" / "timeframe=M5"
        feat_file = feat_dir / "features.jsonl"

        if not feat_file.exists():
            logger.debug("OnlineFeedbackHook: feature file not found: %s", feat_file)
            return None

        best_row = None
        best_delta = float("inf")
        target_ts = dt.timestamp()

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
            delta = abs(et.timestamp() - target_ts)
            if delta < best_delta and delta <= self._max_delta:
                best_delta = delta
                best_row = rec.get("values")
        return best_row

    # ------------------------------------------------------------------
    # Main processing loop
    # ------------------------------------------------------------------

    def process_new_trades(self, *, save_weights: bool = True) -> dict[str, Any]:
        """Scan journal for new closed trades since last_processed_at and
        update the adapter for each one with a matchable feature vector.

        Returns a summary dict with counts.
        """
        if not self._journal_path.exists():
            return {"status": "no_journal", "updated": 0, "skipped": 0, "errors": 0}

        entries: list[dict[str, Any]] = []
        for line in self._journal_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError:
                continue

        updated = 0
        skipped = 0
        errors = 0
        latest_processed = self._last_processed_at

        for entry in entries:
            recorded_at = str(entry.get("recorded_at", ""))
            if self._last_processed_at and recorded_at <= self._last_processed_at:
                continue

            ack_status = str(entry.get("ack_status", ""))
            if ack_status != "closed":
                skipped += 1
                continue

            label = self._trade_label(entry)
            if label is None:
                skipped += 1
                continue

            close_time = str(
                entry.get("close_recorded_at")
                or entry.get("close_time")
                or entry.get("recorded_at", "")
            )
            symbol = str(entry.get("symbol", "XAUUSDc"))
            features = self._find_feature_vector(close_time, symbol)

            if features is None:
                skipped += 1
                logger.debug(
                    "OnlineFeedbackHook: no feature match for trade %s at %s",
                    entry.get("message_id", "?"),
                    close_time,
                )
                continue

            try:
                import numpy as np

                feat_arr = np.array(list(features.values()), dtype=np.float64)
                success = self._adapter.partial_fit(feat_arr, label)
                if success:
                    updated += 1
                    logger.info(
                        "OnlineFeedbackHook: updated adapter with label=%d (trade=%s)",
                        label,
                        entry.get("message_id", "?"),
                    )
                else:
                    errors += 1
            except Exception:
                errors += 1
                logger.exception(
                    "OnlineFeedbackHook: update failed for trade %s", entry.get("message_id", "?")
                )

            if recorded_at > (latest_processed or ""):
                latest_processed = recorded_at

        self._last_processed_at = latest_processed
        self._save_state()

        if save_weights and updated > 0:
            self._adapter.save_weights()

        summary = {"status": "ok", "updated": updated, "skipped": skipped, "errors": errors}
        logger.info("OnlineFeedbackHook: %s", summary)
        return summary


def run_online_feedback(
    adapter,
    base_dir: str = "data",
    *,
    save_weights: bool = True,
) -> dict[str, Any]:
    """Convenience entry point — create a hook and process new trades."""
    hook = OnlineFeedbackHook(
        adapter=adapter,
        journal_path=f"{base_dir}/live_trade_journal.jsonl",
        feature_store_dir=f"{base_dir}/feature_store/records",
    )
    return hook.process_new_trades(save_weights=save_weights)
