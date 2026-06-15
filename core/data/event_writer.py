"""Single-process, thread-safe, append-only event writer.

FIX-20260611-021: Event Sourcing Foundation — Step 2 Unified Writer.

The EventWriter is the SOLE mechanism for writing to the event stream.
No other code path may open ``ledger_events.jsonl`` for writing.

Design constraints:
 - Windows 10 host: NO assumption of POSIX O_APPEND atomicity.
   Single writer per process + threading.Lock guarantees line-level atomicity.
 - Line-buffered (buffering=1): each write is flushed to OS buffer immediately.
 - Pydantic validation happens BEFORE calling write() — the caller is
   responsible for constructing valid events.  The writer does NOT re-validate
   (zero overhead on the hot path).

Usage::

    from core.data.event_writer import get_event_writer
    from core.contracts.events import PnLEvent, DataSource

    writer = get_event_writer("data")
    event = PnLEvent(
        timestamp=datetime.now(UTC),
        source=DataSource.LIVE,
        event_type="SignalSettled",
        brain_id="Swing_V9_M15_V2",
        symbol="XAUUSDc",
        pnl_r=5.2,
        generated_by="live_intent_loop.v2",
    )
    writer.write(event)
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pydantic import BaseModel


class EventWriter:
    """Thread-safe, append-only writer for the event stream.

    Exactly ONE instance per process.  Created via ``get_event_writer()``.
    """

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path)
        # Create parent directory if needed
        self._path.parent.mkdir(parents=True, exist_ok=True)
        # Open in append mode with line buffering
        self._fh = open(path, "a", encoding="utf-8", buffering=1)
        self._lock = threading.Lock()
        self._line_count: int = 0

    def write(self, event: BaseModel) -> str:
        """Append one event as a JSON line.  Returns the event_id.

        Thread-safe: multiple threads in the same process can call write()
        concurrently.  The lock guarantees line-level atomicity.

        Raises:
            OSError: If the underlying write fails (disk full, etc.).
        """
        line = event.model_dump_json() + "\n"
        with self._lock:
            self._fh.write(line)
            self._fh.flush()
            self._line_count += 1
        # event_id is guaranteed by Pydantic model to exist
        return getattr(event, "event_id", "")

    @property
    def line_count(self) -> int:
        """Number of lines written by this writer instance (since startup)."""
        with self._lock:
            return self._line_count

    @property
    def path(self) -> Path:
        return self._path

    def close(self) -> None:
        """Close the underlying file handle.  Safe to call multiple times."""
        with self._lock:
            if self._fh and not self._fh.closed:
                self._fh.close()

    def __enter__(self) -> EventWriter:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


# ── Module-level singleton ────────────────────────────────────────────────

_writer: EventWriter | None = None
_writer_lock = threading.Lock()


def get_event_writer(base_dir: str) -> EventWriter:
    """Get or create the process-global EventWriter singleton.

    Args:
        base_dir: Base data directory.  The event stream is written to
                  ``{base_dir}/ledger_events.jsonl``.

    Returns:
        The singleton EventWriter instance for this process.
    """
    global _writer
    if _writer is None:
        with _writer_lock:
            if _writer is None:
                _writer = EventWriter(Path(base_dir) / "ledger_events.jsonl")
    return _writer


def reset_event_writer() -> None:
    """Close and reset the global writer.  For testing ONLY."""
    global _writer
    if _writer is not None:
        _writer.close()
        _writer = None
