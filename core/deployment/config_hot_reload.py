import json
import logging
import threading
from pathlib import Path

from core.contracts.domain_keys import (
    PAYLOAD_KEY_CONFIG_PATH,
    PAYLOAD_KEY_CURRENT_KEYS,
    PAYLOAD_KEY_LAST_MODIFIED,
    PAYLOAD_KEY_LISTENER_COUNT,
    PAYLOAD_KEY_MAX_DRAWDOWN_PCT,
    PAYLOAD_KEY_MAX_NOTIONAL_EXPOSURE,
    PAYLOAD_KEY_MAX_OPEN_POSITIONS,
    PAYLOAD_KEY_NEW,
    PAYLOAD_KEY_OLD,
    PAYLOAD_KEY_OPS_MATURITY_MIN_SCORE,
    PAYLOAD_KEY_RELOAD_COUNT,
    PAYLOAD_KEY_SYSTEM_MODE,
)


class ConfigHotReload:
    """Watches a config file and applies changes at runtime.

    Provides a callback mechanism so services can react to
    configuration changes without restarting.
    """

    def __init__(self, config_path: str | None = None):
        self._path = Path(config_path) if config_path else None
        self._lock = threading.Lock()
        self._listeners: list = []
        self._current: dict = {}
        self._last_modified: float = 0
        self._reload_count = 0

    def register_listener(self, fn) -> None:
        with self._lock:
            self._listeners.append(fn)

    def load(self) -> dict:
        if self._path is None or not self._path.exists():
            return self._current
        with self._lock:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._current = data
            self._last_modified = self._path.stat().st_mtime
            return data

    def check_and_reload(self) -> dict | None:
        if self._path is None or not self._path.exists():
            return None
        mtime = self._path.stat().st_mtime
        if mtime <= self._last_modified:
            return None

        old = dict(self._current)
        new = self.load()
        self._reload_count += 1

        changes = self._diff(old, new)
        if changes:
            for fn in self._listeners:
                try:
                    fn(changes, new)
                except Exception:
                    logging.exception(
                        "ConfigHotReload listener failed for config_path=%s",
                        self._path,
                    )

        return changes

    def apply_overrides(self, container, overrides: dict) -> list[str]:
        """Apply runtime overrides to a ServiceContainer's config."""
        applied = []
        cfg = container.config

        if PAYLOAD_KEY_MAX_OPEN_POSITIONS in overrides:
            cfg.max_open_positions = int(overrides[PAYLOAD_KEY_MAX_OPEN_POSITIONS])
            applied.append(PAYLOAD_KEY_MAX_OPEN_POSITIONS)

        if PAYLOAD_KEY_MAX_DRAWDOWN_PCT in overrides:
            cfg.max_drawdown_pct = float(overrides[PAYLOAD_KEY_MAX_DRAWDOWN_PCT])
            applied.append(PAYLOAD_KEY_MAX_DRAWDOWN_PCT)

        if PAYLOAD_KEY_MAX_NOTIONAL_EXPOSURE in overrides:
            cfg.max_notional_exposure = float(overrides[PAYLOAD_KEY_MAX_NOTIONAL_EXPOSURE])
            applied.append(PAYLOAD_KEY_MAX_NOTIONAL_EXPOSURE)

        if PAYLOAD_KEY_SYSTEM_MODE in overrides:
            cfg.system_mode = overrides[PAYLOAD_KEY_SYSTEM_MODE]
            applied.append(PAYLOAD_KEY_SYSTEM_MODE)

        if PAYLOAD_KEY_OPS_MATURITY_MIN_SCORE in overrides:
            cfg.ops_maturity_min_score = float(overrides[PAYLOAD_KEY_OPS_MATURITY_MIN_SCORE])
            applied.append(PAYLOAD_KEY_OPS_MATURITY_MIN_SCORE)

        return applied

    def get_status(self) -> dict:
        return {
            PAYLOAD_KEY_CONFIG_PATH: str(self._path) if self._path else None,
            PAYLOAD_KEY_RELOAD_COUNT: self._reload_count,
            PAYLOAD_KEY_LAST_MODIFIED: self._last_modified,
            PAYLOAD_KEY_CURRENT_KEYS: list(self._current.keys()),
            PAYLOAD_KEY_LISTENER_COUNT: len(self._listeners),
        }

    def _diff(self, old: dict, new: dict) -> dict:
        changes = {}
        all_keys = set(old) | set(new)
        for k in all_keys:
            if old.get(k) != new.get(k):
                changes[k] = {PAYLOAD_KEY_OLD: old.get(k), PAYLOAD_KEY_NEW: new.get(k)}
        return changes
