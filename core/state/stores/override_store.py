from datetime import datetime
from typing import List, Optional

from core.contracts.enums import OverrideStatus


class OverrideStore:
    def __init__(self):
        self._overrides = []

    def add(self, override) -> None:
        self._overrides.append(override)

    def list_all(self) -> List[object]:
        return list(self._overrides)

    def list_active(
        self,
        now: Optional[datetime] = None,
        symbol: Optional[str] = None,
        mode: Optional[str] = None,
        regime: Optional[str] = None,
    ) -> List[object]:
        now = now or datetime.utcnow()
        active = []

        for item in self._overrides:
            if getattr(item, "status", None) != OverrideStatus.ACTIVE:
                continue

            start_time = getattr(item, "start_time", None)
            end_time = getattr(item, "end_time", None)

            if start_time and start_time > now:
                continue
            if end_time and end_time < now:
                continue

            scope = getattr(item, "scope", {}) or {}

            if symbol:
                scoped_symbols = scope.get("symbols", [])
                if scoped_symbols and symbol not in scoped_symbols:
                    continue

            if mode:
                scoped_modes = scope.get("system_modes", [])
                if scoped_modes and mode not in scoped_modes:
                    continue

            if regime:
                scoped_regimes = scope.get("regimes", [])
                if scoped_regimes and regime not in scoped_regimes:
                    continue

            active.append(item)

        return active


