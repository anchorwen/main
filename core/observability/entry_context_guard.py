"""Daemon thread for periodic entry_context completeness monitoring.

DLR-001 (2026-06-17): 34 real BTC opens were permanently lost for training
because ``entry_context.vector`` was absent from journal entries.  This
guard runs as an independent daemon thread — it never touches the hot path
(live_cycle.py) or the scheduler (daily_ops_scheduler.py).

Cadence:
    - Every 5 minutes: lightweight heartbeat write (proves the guard is alive)
    - Every hour: full journal scan via DataHealthService.check_entry_context_completeness()

Startup (in main entry point, AFTER infrastructure is ready)::

    from core.observability.entry_context_guard import EntryContextGuard
    guard = EntryContextGuard(base_dir=Path("data_btc"))
    guard.start()

Shutdown is automatic — the thread is ``daemon=True`` and dies with the process.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import traceback
from datetime import UTC, datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# Re-import for guard use
from core.observability.data_loss import append_loss_record
from core.runtime.fault_handler import fail_open_guard

_HEARTBEAT_INTERVAL = 300   # 5 minutes
_FULL_SCAN_INTERVAL = 3600  # 1 hour


class EntryContextGuard:
    """Background monitor for ``entry_context.vector`` completeness."""

    def __init__(
        self,
        base_dir: Path,
        symbol: str = "",
        *,
        heartbeat_interval: float = _HEARTBEAT_INTERVAL,
        scan_interval: float = _FULL_SCAN_INTERVAL,
    ) -> None:
        self._base_dir = Path(base_dir)
        # Derive symbol from directory name if not explicitly given
        self._symbol = symbol or (
            "BTCUSDc" if "btc" in str(base_dir).lower() else "XAUUSDc"
        )
        self._heartbeat_interval = heartbeat_interval
        self._scan_interval = scan_interval
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._heartbeat_path = (
            self._base_dir / "state" / "heartbeats" / "entry_context_guard.json"
        )

    # ── Public API ───────────────────────────────────────────────────────

    def start(self) -> None:
        """Launch the guard as a daemon thread.

        Idempotent — calling ``start()`` on an already-running guard is a no-op.
        """
        if self._thread is not None and self._thread.is_alive():
            logger.info("EntryContextGuard: already running, skipping start()")
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_loop,
            name="entry-context-guard",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            "EntryContextGuard: started (heartbeat=%ss, scan=%ss)",
            self._heartbeat_interval,
            self._scan_interval,
        )

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the guard to stop and wait for it to exit."""
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        logger.info("EntryContextGuard: stopped")

    # ── Internal ─────────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        """Main loop.  Top-level exception handler prevents silent death (M1)."""
        last_scan = 0.0
        while not self._stop_event.is_set():
            try:
                now = time.time()

                # Heartbeat (every cycle — proves the guard process is alive)
                self._write_heartbeat()

                # Full scan (hourly)
                if now - last_scan >= self._scan_interval:
                    self._run_full_scan()
                    last_scan = now

            except Exception:  # BLE001:FOG
                with fail_open_guard("entry_context_guard:_run_loop"):
                    # M1: never let the guard die silently
                    logger.critical(
                        "EntryContextGuard: unhandled exception in main loop:\n%s",
                        traceback.format_exc(),
                    )
                    # Brief sleep before retry to avoid spin on persistent errors
                    time.sleep(10.0)
                    continue
            # Sleep in small increments so we respond to stop() promptly
            for _ in range(int(self._heartbeat_interval / 5)):
                if self._stop_event.is_set():
                    break
                time.sleep(5.0)

    def _write_heartbeat(self) -> None:
        """Write a lightweight heartbeat file so external monitors can
        detect a dead guard process."""
        self._heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "guard": "entry_context_guard",
            "pid": os.getpid(),
            "thread": threading.current_thread().name,
            "last_heartbeat_utc": (
                datetime.now(UTC).replace(tzinfo=None).isoformat() + "Z"
            ),
            "base_dir": str(self._base_dir),
        }
        try:
            self._heartbeat_path.write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            logger.warning("EntryContextGuard: failed to write heartbeat", exc_info=True)

    def _run_full_scan(self) -> None:
        """Execute the DataHealthService entry_context check and act on findings."""
        try:
            # Late import to avoid circular dependency at module level
            from core.observability.data_health_service import (
                DataHealthService,
            )

            service = DataHealthService(base_dir=str(self._base_dir), symbol=self._symbol)
            result = service.check_entry_context_completeness()
        except Exception:  # BLE001:FOG
            with fail_open_guard("entry_context_guard:_run_full_scan"):
                logger.error(
                    "EntryContextGuard: DataHealthService scan failed:\n%s",
                    traceback.format_exc(),
                )
                return
        metrics = result.metrics
        if not metrics:
            return

        total_missing = (
            metrics.get("missing_ctx", 0)
            + metrics.get("missing_vector", 0)
            + metrics.get("empty_vector", 0)
        )

        if result.status.value == "FAIL" and total_missing > 0:
            logger.warning(
                "EntryContextGuard: DETECTED %d opens with missing entry_context.vector",
                total_missing,
            )
            append_loss_record(
                base_dir=self._base_dir,
                detector="L2",
                data_type="missing_entry_context.vector",
                affected_count=total_missing,
                sample_ids=metrics.get("sample_tickets", []),
                extra={
                    "completeness": metrics.get("completeness"),
                    "total_opens": metrics.get("total_opens"),
                },
            )


# ── Convenience: module-level start for single-symbol deployments ──

_guard_instance: EntryContextGuard | None = None


def start_entry_context_guard(base_dir: Path, symbol: str = "") -> EntryContextGuard:
    """Start the singleton guard for *base_dir* (e.g. ``data_btc``).

    Call this from the main entry point AFTER all infrastructure (logging,
    state directories, DataHealthService) is fully initialized.
    """
    global _guard_instance
    if _guard_instance is not None and _guard_instance._thread is not None:
        return _guard_instance  # already started
    _guard_instance = EntryContextGuard(base_dir=base_dir, symbol=symbol)
    _guard_instance.start()
    return _guard_instance
