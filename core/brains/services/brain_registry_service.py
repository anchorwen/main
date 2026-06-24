"""Brain Registry Service — loads and manages the active brain registry.

Provides list_active_entries() consumed by BrainRunService and main.py.

Single Source of Truth: brain config JSON files in ``configs/brains/``.
When ``registry_entries`` in live.yaml is empty or absent, the service
auto-discovers all ``brain_registry_entry.v1`` configs from disk via
BrainRegistry — no manual YAML registration needed.

The ``registry_entries`` list, when present, acts as an optional allowlist
filter: only brains whose config path appears in the list are active.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.brains.services.brain_registry_loader import BrainRegistryLoader


class BrainRegistryService:
    """Service that loads and manages the brain registry.

    Primary source: auto-discovery from ``configs/brains/`` via BrainRegistry.
    Optional allowlist: ``registry_entries`` in live.yaml filters auto-discovered
    brains by config path when present.
    """

    def __init__(
        self,
        registry_entries: list[dict[str, Any]] | None = None,
        project_root: Path | None = None,
    ):
        self._registry_entries = registry_entries or []
        self._project_root = project_root or Path.cwd()
        self._loader = BrainRegistryLoader()
        self._auto_discovered: list[dict[str, Any]] | None = None

    # ── auto-discovery ───────────────────────────────────────────────────

    def _discover_from_disk(self) -> list[dict[str, Any]]:
        """Auto-discover all brain_registry_entry.v1 configs from configs/brains/."""
        if self._auto_discovered is not None:
            return self._auto_discovered

        from core.brains.brain_registry import BrainRegistry

        registry = BrainRegistry.instance()
        entries: list[dict[str, Any]] = []
        for brain_entry in registry.list_all():
            if not brain_entry.is_active:
                continue
            entries.append(brain_entry.raw)
        self._auto_discovered = entries
        return entries

    def _has_explicit_entries(self) -> bool:
        """Return True when live.yaml provides an explicit registry_entries list."""
        return bool(self._registry_entries)

    # ── public API ───────────────────────────────────────────────────────

    # DQAF-20260624-058: frozen/retired brain IDs excluded at the water source.
    _GOVERNANCE_EXCLUDED_STATUSES: frozenset[str] = frozenset({"frozen", "retired"})

    def list_active_entries(
        self,
        gov_state_filter: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """Return all active brain entry dicts.

        When ``registry_entries`` is explicitly configured in live.yaml, it acts
        as an allowlist: only brains whose config path matches an enabled entry
        are returned.  When ``registry_entries`` is empty or absent, ALL active
        brains auto-discovered from ``configs/brains/`` are returned.

        Args:
            gov_state_filter: Optional ``{brain_id: governance_status}`` dict
                injected by the caller (dependency inversion — this module does
                NOT import GovernanceService).  Entries whose status is
                ``frozen`` or ``retired`` are excluded from the result.
                When ``None`` (default), no governance filtering is applied
                (backward-compatible with tests, shadow, and diagnostics).
        """
        entries = (
            self._filtered_by_allowlist()
            if self._has_explicit_entries()
            else self._discover_from_disk()
        )
        if gov_state_filter is None:
            return entries
        return [
            e
            for e in entries
            if gov_state_filter.get(e.get("brain_id", ""), "probation")
            not in self._GOVERNANCE_EXCLUDED_STATUSES
        ]

    def _filtered_by_allowlist(self) -> list[dict[str, Any]]:
        """Return auto-discovered entries filtered by the allowlist."""
        discovered = self._discover_from_disk()
        if not discovered:
            return []

        allowed_paths: set[str] = set()
        for entry_ref in self._registry_entries:
            if not entry_ref.get("enabled", True):
                continue
            path_str = entry_ref.get("path", "")
            entry_path = Path(path_str)
            if not entry_path.is_absolute():
                entry_path = (self._project_root / entry_path).resolve()
            allowed_paths.add(str(entry_path))

        filtered: list[dict[str, Any]] = []
        for brain_cfg in discovered:
            cfg_brain_id = brain_cfg.get("brain_id", "")
            cfg_path = self._project_root / "configs" / "brains" / f"{cfg_brain_id}.json"
            if str(cfg_path.resolve()) in allowed_paths:
                filtered.append(brain_cfg)
            else:
                # Also check relative path matches
                rel = f"configs/brains/{cfg_brain_id}.json"
                if rel in allowed_paths:
                    filtered.append(brain_cfg)
        return filtered

    def get_entry_by_id(self, brain_id: str) -> dict[str, Any] | None:
        """Look up a single brain entry by its brain_id."""
        for entry in self.list_active_entries():
            if entry.get("brain_id") == brain_id:
                return entry
        return None

    def reload(self) -> None:
        """Clear caches so next call re-discovers from disk."""
        self._auto_discovered = None
        from core.brains.brain_registry import BrainRegistry

        BrainRegistry.reset()
