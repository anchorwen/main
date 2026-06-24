"""Brain registry — centralized, read-only access to brain configuration metadata.

This is the SINGLE SOURCE OF TRUTH for brain metadata.  All code that needs
to know which contract group a brain belongs to, what its training horizon is,
or what feature schema it requires must go through this registry.

Adding a new brain only requires a JSON config file in configs/brains/ —
no code changes needed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class BrainEntry:
    """Immutable snapshot of a brain registry entry loaded from JSON."""

    brain_id: str
    brain_type: str
    brain_role: str
    contract_group: str
    training_horizon: int
    feature_schema: str
    vote_weight: float
    magic: int
    status: str
    artifact_path: str
    hmre_layer: str = ""
    training_params: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def is_active(self) -> bool:
        """A brain is active if it is not retired or frozen."""
        return self.status not in ("retired", "frozen")


class BrainRegistry:
    """Central read-only brain metadata registry.

    Loads all ``brain_registry_entry.v1`` JSON files from ``configs/brains/``
    and indexes them by brain_id, brain_type, and contract_group.

    Singleton access via ``BrainRegistry.instance()``.
    """

    def __init__(self, config_dir: str = "configs/brains"):
        self._entries: dict[str, BrainEntry] = {}
        self._by_type: dict[str, list[BrainEntry]] = {}
        self._by_group: dict[str, list[BrainEntry]] = {}
        self._load_all(Path(config_dir))

    # ── Loading ──────────────────────────────────────────────────────────

    def _load_all(self, config_dir: Path) -> None:
        if not config_dir.is_dir():
            return
        for path in sorted(config_dir.glob("*.json")):
            if ".normalization." in path.name:
                continue
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (RuntimeError, ValueError, KeyError, TypeError, OSError):  # BLE001:FOG
                continue
            if raw.get("schema_version") != "brain_registry_entry.v1":
                continue

            entry = BrainEntry(
                brain_id=raw.get("brain_id", ""),
                brain_type=raw.get("brain_type", ""),
                brain_role=raw.get("brain_role", ""),
                contract_group=raw.get("contract_group", "barrier_12bar"),
                training_horizon=raw.get("training_horizon", 12),
                feature_schema=raw.get("feature_schema", "v9_40dim"),
                vote_weight=raw.get("vote_weight", 1.0),
                magic=raw.get("magic", 0),
                status=raw.get("status", "shadow"),
                artifact_path=raw.get("artifact_path", ""),
                hmre_layer=raw.get("hmre_layer", ""),
                training_params=raw.get("training_params", {}),
                raw=raw,
            )
            self._entries[entry.brain_id] = entry
            if entry.brain_type:
                self._by_type.setdefault(entry.brain_type, []).append(entry)
            if entry.contract_group:
                self._by_group.setdefault(entry.contract_group, []).append(entry)

    def reload(self, config_dir: str = "configs/brains") -> None:
        """Clear and re-load all entries (hot-reload support)."""
        self._entries.clear()
        self._by_type.clear()
        self._by_group.clear()
        self._load_all(Path(config_dir))

    # ── Lookup ───────────────────────────────────────────────────────────

    def get(self, brain_id: str) -> BrainEntry | None:
        """Look up a brain entry by brain_id."""
        return self._entries.get(brain_id)

    def get_by_type(self, brain_type: str) -> list[BrainEntry]:
        """List all brain entries with the given brain_type (may be empty)."""
        return self._by_type.get(brain_type, [])

    def get_first_by_type(self, brain_type: str) -> BrainEntry | None:
        """Return the first (usually only) brain entry for a brain_type.

        Convenience method for factory dispatch where brain_type is expected
        to be unique (e.g., 'onnx', 'xgboost_json', 'lightgbm_txt').
        """
        entries = self._by_type.get(brain_type, [])
        return entries[0] if entries else None

    def get_contract_group(self, brain_id: str) -> str:
        """Return the contract_group for a brain_id (default: 'barrier_12bar')."""
        entry = self.get(brain_id)
        return entry.contract_group if entry else "barrier_12bar"

    def get_training_horizon(self, brain_id: str) -> int:
        """Return the training_horizon for a brain_id (default: 12)."""
        entry = self.get(brain_id)
        return entry.training_horizon if entry else 12

    def get_feature_schema(self, brain_id: str) -> str:
        """Return the feature_schema for a brain_id (default: 'v9_40dim')."""
        entry = self.get(brain_id)
        return entry.feature_schema if entry else "v9_40dim"

    def list_by_group(self, contract_group: str) -> list[BrainEntry]:
        """List all brain entries belonging to a contract_group."""
        return self._by_group.get(contract_group, [])

    def list_all(self) -> list[BrainEntry]:
        """List all registered brain entries."""
        return list(self._entries.values())

    @property
    def all_groups(self) -> list[str]:
        """List all distinct contract groups."""
        return sorted(self._by_group.keys())

    def resolve_ids_to_group(self, brain_ids: list[str]) -> str:
        """Map a list of brain_ids to a single contract_group.

        Returns the contract_group of the first recognised brain_id.
        Falls back to ``"barrier_12bar"`` when none are found.
        """
        for bid in brain_ids:
            entry = self.get(bid)
            if entry:
                return entry.contract_group
        return "unknown"

    # ── Singleton ────────────────────────────────────────────────────────

    _instance: BrainRegistry | None = None

    @classmethod
    def instance(cls, config_dir: str = "configs/brains") -> BrainRegistry:
        """Return the singleton BrainRegistry, creating it on first call."""
        if cls._instance is None:
            cls._instance = cls(config_dir=config_dir)
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset the singleton (useful in tests)."""
        cls._instance = None
