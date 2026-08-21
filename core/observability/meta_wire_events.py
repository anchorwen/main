"""SSOT for ``meta_pipeline_wired`` events (P7 / TECH_DEBT-018).

The ``meta_pipeline_wired`` event records a successful MetaFilter stage-2
wiring at boot. It is printed to the intent process stdout AND (P7) appended
to a persistent per-asset JSONL file decoupled from the intent log file
lifecycle.

Why the SSOT file exists
------------------------
The health check ``check_meta_filter_state`` reads the wired event to assert
the running intent process actually wired MetaFilter. Before P7 it located the
event by head-reading ``intent_*.log`` files — but the event's home in the
process-stdout tree depends on how the launcher captures the intent subprocess:

* normal path → ``logs/intent_<boot_ts>.log`` (launcher tees stdout there)
* crash-loop era (8/11→8/13) → intent stdout was captured into
  ``live_launcher_*.log`` ``[intent]`` lines and **no** fresh ``intent_*.log``
  was rotated → the head-read only ever saw a stale event → daily
  ``META_FILTER_WIRED_STALE`` false WARN.

The SSOT file survives any stdout-routing change: every successful wire is
appended here by the producer itself (``scripts/live_intent_loop.py``),
regardless of where stdout is redirected.

Format: one JSON object per line, always carrying a ``time`` ISO-8601 UTC
field (defaulted at write time when absent). The file is small (one line per
boot) and append-only.

Pure stdlib, no cross-module imports — safe for the standalone intent script.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_WIRED_EVENTS_FILENAME = "meta_pipeline_wired.jsonl"

_INTENT_LOG_PREFIX = "intent_"
_INTENT_LOG_SUFFIX = ".log"


def wired_events_path(base_dir: str | os.PathLike[str]) -> Path:
    """Absolute path of the per-asset wired-event SSOT file.

    Scoped to the asset data dir so XAU (``data``) and BTC (``data_btc``)
    each keep an independent record — matching how ``check_meta_filter_state``
    globs ``{base_dir}/logs/intent_*.log``.
    """
    return Path(base_dir) / "state" / _WIRED_EVENTS_FILENAME


def record_wired_event(base_dir: str | os.PathLike[str], event: dict[str, Any]) -> bool:
    """Append a ``meta_pipeline_wired`` event to the SSOT file (non-fatal).

    ``event["time"]`` is written through when present; a missing timestamp is
    defaulted to the current UTC time so the record is always ageable. Returns
    ``True`` on success. Any write failure is swallowed — a durable append
    must never take down the live intent loop (Iron Law #1: best-effort I/O).
    """
    payload = dict(event)
    if not payload.get("time"):
        payload["time"] = datetime.now(UTC).replace(tzinfo=None).isoformat()
    path = wired_events_path(base_dir)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
            f.flush()
        return True
    except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG — non-fatal
        return False


def read_last_wired_event(base_dir: str | os.PathLike[str]) -> dict[str, Any] | None:
    """Tail-read the most recent wired event from the SSOT file (last line).

    Returns ``None`` when the file is missing, empty, or holds no parseable
    dict line. A corrupt trailing line is skipped in favour of the previous
    valid one (append may have raced a crash).
    """
    path = wired_events_path(base_dir)
    if not path.exists():
        return None
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size == 0:
                return None
            # Events are one short line per boot; 64KB back-read covers the
            # last record even on pathological files.
            f.seek(max(0, size - 65536))
            data = f.read().decode("utf-8", errors="replace")
        for line in reversed(data.splitlines()):
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                return parsed
    except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
        return None
    return None


def parse_intent_boot_ts(log_name: str) -> datetime | None:
    """Parse the boot timestamp from an ``intent_YYYYMMDDTHHMMSSZ.log`` name.

    The launcher names intent logs after the subprocess start instant
    (``_utc_compact()``), so the filename encodes the boot time. Lexicographic
    sort of the names equals chronological order (fixed-width zero-padded).
    """
    if not (log_name.startswith(_INTENT_LOG_PREFIX) and log_name.endswith(_INTENT_LOG_SUFFIX)):
        return None
    ts_str = log_name[len(_INTENT_LOG_PREFIX) : -len(_INTENT_LOG_SUFFIX)]
    if not ts_str.endswith("Z"):
        return None
    try:
        return datetime.strptime(ts_str, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None
