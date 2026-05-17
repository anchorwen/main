import logging
from typing import Any

from core.brains.adapters.base_adapter import BaseBrainAdapter
from core.deployment.brain_alert import emit_brain_alert
from core.deployment.brain_config_validator import BrainConfigError

# Schema aliases: all keys resolve to the same canonical feature dict
_SCHEMA_ALIASES: dict[str, str] = {
    "swing_24": "daily_swing_24",
    "v2_microstructure_9": "v4.3_microstructure_9",
    "v4.5_microstructure_9": "v4.3_microstructure_9",
}


def _resolve_schema_key(schema_id: str) -> str:
    """Resolve a schema_id to its canonical blackboard key."""
    return _SCHEMA_ALIASES.get(schema_id, schema_id)


class BrainRunService:
    """Unified brain inference service — the single entry point for all consumers.

    All brain inference (live, shadow, backtest, verification) MUST go through
    this service.  No other code path should call ``adapter.infer()`` or
    ``adapter.run()`` directly.

    Feature routing: each brain's ``feature_schema_id`` is looked up on the
    *feature_blackboard*.  Schema aliases (e.g. ``swing_24`` → ``daily_swing_24``)
    are resolved automatically.
    """

    def __init__(self, brain_factory, brain_registry_service):
        self._brain_factory = brain_factory
        self._brain_registry_service = brain_registry_service
        self._adapters: dict[str, BaseBrainAdapter] = {}
        self._failed_brain_ids: set[str] = set()

    # ── Lifecycle ──────────────────────────────────────────────────────────

    def ensure_loaded(self) -> list[str]:
        """Build and load all active adapters.  Call once on startup or after
        a registry reload.  Returns list of loaded brain_ids."""
        loaded = []
        for entry in self._brain_registry_service.list_active_entries():
            brain_id = entry.get("brain_id", "")
            if brain_id in self._adapters:
                loaded.append(brain_id)
                continue
            if brain_id in self._failed_brain_ids:
                continue
            try:
                adapter = self._brain_factory.build(entry)
                self._adapters[brain_id] = adapter
                loaded.append(brain_id)
                logging.info(
                    "BrainRunService loaded adapter brain_id=%s backend=%s",
                    brain_id,
                    getattr(adapter, "_backend", "unknown"),
                )
            except BrainConfigError:
                self._failed_brain_ids.add(brain_id)
                emit_brain_alert(
                    brain_id,
                    "config_validation_error",
                    {"message": "Brain excluded from inference due to config validation failure"},
                )
                logging.exception(
                    "BrainRunService config validation failed for brain_id=%s — brain excluded",
                    brain_id,
                )
            except Exception:
                logging.exception(
                    "BrainRunService failed to build adapter for brain_id=%s",
                    brain_id,
                )
        return loaded

    def reload_adapters(self) -> list[str]:
        """Reload all adapters from scratch (e.g. after config hot-reload)."""
        self._adapters.clear()
        self._failed_brain_ids.clear()
        return self.ensure_loaded()

    # ── Bulk inference ─────────────────────────────────────────────────────

    def run_active_brains(
        self,
        feature_snapshot,
        control_snapshot,
        feature_blackboard: dict[str, dict] | None = None,
    ) -> list:
        """Run inference for all active brains.

        Routes the correct feature dict to each brain by looking up its
        ``feature_schema_id`` on the *feature_blackboard* (with alias resolution)::

            {
                "v9_institutional_40": {...},
                "daily_swing_24": {...},       # swing_24 alias resolves here
                "v4.3_microstructure_9": {...}, # v2_*/v4.5_* aliases resolve here
            }

        If a schema key is missing from the blackboard the brain receives
        an empty dict — it will produce a neutral prediction.
        """
        proposals = []
        blackboard = feature_blackboard or {}

        for entry in self._brain_registry_service.list_active_entries():
            proposal = self._run_one_entry(entry, feature_snapshot, blackboard)
            if proposal is not None:
                proposals.append(proposal)

        return proposals

    def run_brains_for_contract_group(
        self,
        contract_group: str,
        feature_snapshot,
        feature_blackboard: dict[str, dict] | None = None,
    ) -> list:
        """Run inference for brains matching a contract group (e.g. 'barrier_12bar')."""
        proposals = []
        blackboard = feature_blackboard or {}

        for entry in self._brain_registry_service.list_active_entries():
            if entry.get("contract_group") != contract_group:
                continue
            proposal = self._run_one_entry(entry, feature_snapshot, blackboard)
            if proposal is not None:
                proposals.append(proposal)

        return proposals

    # ── Single-brain inference ─────────────────────────────────────────────

    def run_single_brain(
        self,
        brain_id: str,
        feature_snapshot,
        feature_source: dict | None = None,
    ):
        """Run inference for a single brain by brain_id.

        Used by OU exit evaluation, drift-lock re-evaluation, and other
        special-case paths that need a single brain's opinion.
        """
        entry = self._brain_registry_service.get_entry(brain_id)
        if entry is None:
            logging.warning("BrainRunService.run_single_brain: unknown brain_id=%s", brain_id)
            return None

        adapter = self._adapters.get(brain_id)
        if adapter is None and brain_id not in self._failed_brain_ids:
            try:
                adapter = self._brain_factory.build(entry)
                self._adapters[brain_id] = adapter
            except BrainConfigError:
                self._failed_brain_ids.add(brain_id)
                return None
            except Exception:
                logging.exception(
                    "BrainRunService failed to build adapter for brain_id=%s", brain_id
                )
                return None

        if adapter is None:
            return None

        try:
            return adapter.run(feature_snapshot, feature_source or {})
        except Exception:
            logging.exception("BrainRunService inference failed for brain_id=%s", brain_id)
            return None

    def run_brain_type(
        self,
        brain_type: str,
        feature_snapshot,
        feature_source: dict | None = None,
    ):
        """Run inference for the first active brain matching a brain_type.

        Returns the first successful proposal, or None.
        """
        for entry in self._brain_registry_service.list_active_entries():
            if entry.get("brain_type") != brain_type:
                continue
            return self.run_single_brain(
                entry.get("brain_id", ""),
                feature_snapshot,
                feature_source,
            )
        return None

    # ── Diagnostics ────────────────────────────────────────────────────────

    def get_loaded_count(self) -> int:
        return len(self._adapters)

    def get_failed_count(self) -> int:
        return len(self._failed_brain_ids)

    def get_adapter(self, brain_id: str) -> BaseBrainAdapter | None:
        return self._adapters.get(brain_id)

    def is_brain_loaded(self, brain_id: str) -> bool:
        return brain_id in self._adapters

    def list_loaded_brain_ids(self) -> list[str]:
        return list(self._adapters.keys())

    # ── Internal ───────────────────────────────────────────────────────────

    def _ensure_adapter(self, entry: dict) -> BaseBrainAdapter | None:
        """Get or build an adapter for a brain entry.  Returns None on failure."""
        brain_id = entry.get("brain_id", "")
        adapter = self._adapters.get(brain_id)
        if adapter is not None:
            return adapter
        if brain_id in self._failed_brain_ids:
            return None
        try:
            adapter = self._brain_factory.build(entry)
            self._adapters[brain_id] = adapter
            return adapter
        except BrainConfigError:
            self._failed_brain_ids.add(brain_id)
            emit_brain_alert(
                brain_id,
                "config_validation_error",
                {"message": "Brain excluded from inference due to config validation failure"},
            )
            return None
        except Exception:
            logging.exception(
                "BrainRunService failed to build adapter for brain_id=%s",
                brain_id,
            )
            return None

    def _run_one_entry(
        self,
        entry: dict,
        feature_snapshot,
        blackboard: dict[str, dict],
    ) -> Any | None:
        """Run inference for a single brain registry entry."""
        brain_id = entry.get("brain_id", "")
        adapter = self._ensure_adapter(entry)
        if adapter is None:
            return None

        schema_id = entry.get("feature_schema_id", "v9_institutional_40")
        # Resolve schema alias to canonical blackboard key
        canonical_key = _resolve_schema_key(schema_id)
        brain_feature_source = blackboard.get(canonical_key, {})

        try:
            return adapter.run(feature_snapshot, brain_feature_source)
        except Exception:
            logging.exception(
                "BrainRunService inference failed for brain_id=%s",
                brain_id,
            )
            return None
