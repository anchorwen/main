"""Brain Registry Service — loads and manages the active brain registry.

Provides list_active_entries() consumed by BrainRunService and main.py.
"""

import logging
from pathlib import Path
from typing import Any

from core.brains.services.brain_registry_loader import BrainRegistryLoader


class BrainRegistryService:
    """Service that loads and manages the brain registry from live.yaml entries."""

    def __init__(self, registry_entries: list[dict[str, Any]], project_root: Path | None = None):
        self._registry_entries = registry_entries
        self._project_root = project_root or Path.cwd()
        self._loader = BrainRegistryLoader()

    def list_active_entries(self) -> list[dict[str, Any]]:
        """Return the list of loaded brain entry dicts for all enabled entries."""
        active: list[dict[str, Any]] = []

        for entry_ref in self._registry_entries:
            if not entry_ref.get("enabled", True):
                continue

            path_str = entry_ref.get("path", "")
            entry_path = Path(path_str)
            if not entry_path.is_absolute():
                entry_path = (self._project_root / entry_path).resolve()

            if not entry_path.exists():
                continue

            try:
                brain_entry = self._loader.load_json(str(entry_path))
                active.append(brain_entry)
            except Exception:
                logging.exception(
                    "BrainRegistryService failed loading brain entry from %s",
                    entry_path,
                )
                continue

        return active

    def get_entry_by_id(self, brain_id: str) -> dict[str, Any] | None:
        """Look up a single brain entry by its brain_id."""
        for entry in self.list_active_entries():
            if entry.get("brain_id") == brain_id:
                return entry
        return None
