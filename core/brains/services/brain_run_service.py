import logging

import numpy as np

from core.brains.adapters.base_adapter import BaseBrainAdapter


class BrainRunService:
    """Runs inference across all active brain adapters.

    Accepts a feature_vector (np.ndarray) that was pre-computed by FeatureService,
    and passes it to each adapter's inference() method.

    Each adapter receives the same feature_vector; adapters that maintain
    internal state (e.g. ParamsBrainAdapter with rolling price buffer) will
    ignore feature_vector and use their own internal state.
    """

    def __init__(self, brain_factory, brain_registry_service):
        self._brain_factory = brain_factory
        self._brain_registry_service = brain_registry_service
        self._adapters: dict[str, BaseBrainAdapter] = {}

    def ensure_loaded(self) -> list[str]:
        """Build and load all active adapters.  Call once on startup or after
        a registry reload.  Returns list of loaded brain_ids."""
        loaded = []
        for entry in self._brain_registry_service.list_active_entries():
            brain_id = entry.get("brain_id", "")
            if brain_id in self._adapters:
                loaded.append(brain_id)
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
            except Exception:
                logging.exception(
                    "BrainRunService failed to build adapter for brain_id=%s",
                    brain_id,
                )
        return loaded

    def run_active_brains(
        self,
        feature_snapshot,
        control_snapshot,
        feature_vector: np.ndarray | None = None,
        feature_source: dict | None = None,
    ) -> list:
        proposals = []

        for entry in self._brain_registry_service.list_active_entries():
            brain_id = entry.get("brain_id", "")
            adapter = self._adapters.get(brain_id)
            if adapter is None:
                try:
                    adapter = self._brain_factory.build(entry)
                    self._adapters[brain_id] = adapter
                except Exception:
                    logging.exception(
                        "BrainRunService failed to build adapter for brain_id=%s",
                        brain_id,
                    )
                    continue

            try:
                proposal = adapter.run(feature_snapshot, feature_source)
                proposals.append(proposal)
            except Exception:
                logging.exception(
                    "BrainRunService inference failed for brain_id=%s",
                    brain_id,
                )
                continue

        return proposals
