from datetime import datetime

from core.contracts.ids import new_snapshot_id


class FeatureService:
    """Unified feature snapshot service.

    Wraps feature adapters and provides a consistent interface for
    the RuntimeLoop. Subclass or inject a feature_adapter for
    different data sources.
    """

    def __init__(self, feature_adapter=None, default_venue: str = "MT5"):
        self._adapter = feature_adapter
        self._default_venue = default_venue

    def build_snapshot(self, trigger: dict):
        from apps.engine.runtime_loop import SimpleFeatureSnapshot
        return SimpleFeatureSnapshot(
            snapshot_id=new_snapshot_id(),
            event_time=datetime.utcnow(),
            symbol=trigger.get("symbol", "UNKNOWN"),
            venue=trigger.get("venue", self._default_venue),
        )


class BrainRegistryService:
    """Manages the set of active brain entries.

    Wraps a list of brain configuration dicts and provides
    ``list_active_entries()`` for BrainRunService.
    """

    def __init__(self, entries: list[dict] | None = None):
        self._entries = list(entries) if entries else []

    def register(self, entry: dict) -> None:
        self._entries.append(entry)

    def remove(self, brain_id: str) -> None:
        self._entries = [e for e in self._entries if e.get("brain_id") != brain_id]

    def list_active_entries(self) -> list[dict]:
        return [e for e in self._entries if e.get("status", "live") not in {"retired", "frozen"}]

    def list_all_entries(self) -> list[dict]:
        return list(self._entries)

    def get_entry(self, brain_id: str) -> dict | None:
        for e in self._entries:
            if e.get("brain_id") == brain_id:
                return e
        return None


class IntentExplainer:
    """Builds reason tags for decision intents."""

    def __init__(self, prefix: str = "v9"):
        self._prefix = prefix

    def build_reason_tags(self, candidate, action: str, side: str) -> list[str]:
        tags = [self._prefix]
        tags.append(action)
        tags.append(side)
        return tags
