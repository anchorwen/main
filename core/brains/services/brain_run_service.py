class BrainRunService:
    def __init__(self, brain_factory, brain_registry_service):
        self._brain_factory = brain_factory
        self._brain_registry_service = brain_registry_service

    def run_active_brains(self, feature_snapshot, control_snapshot, feature_source: dict):
        proposals = []

        for brain_entry in self._brain_registry_service.list_active_entries():
            runner = self._brain_factory.build(brain_entry)
            proposal = runner.run(
                feature_snapshot=feature_snapshot,
                feature_source=feature_source,
            )
            proposals.append(proposal)

        return proposals


