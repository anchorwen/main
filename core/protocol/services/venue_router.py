from datetime import datetime

from core.contracts.domain.dispatch_result import DispatchResult
from core.contracts.enums import DispatchStatus
from core.protocol.schema_versions import SCHEMA_DISPATCH_RESULT


class VenueAdapter:
    """Base class for venue-specific dispatch adapters."""

    def __init__(self, venue_id: str):
        self.venue_id = venue_id

    def dispatch(self, request, envelope) -> DispatchResult:
        raise NotImplementedError

    def health(self) -> dict:
        return {"venue_id": self.venue_id, "status": "unknown"}


class StubVenueAdapter(VenueAdapter):
    """Test adapter that records dispatches."""

    def __init__(self, venue_id: str):
        super().__init__(venue_id)
        self._dispatches: list[dict] = []

    def dispatch(self, request, envelope) -> DispatchResult:
        self._dispatches.append({
            "dispatch_id": request.dispatch_id,
            "message_id": envelope.message_id,
            "venue": self.venue_id,
        })
        return DispatchResult(
            schema_version=SCHEMA_DISPATCH_RESULT,
            dispatch_id=request.dispatch_id,
            message_id=envelope.message_id,
            status=DispatchStatus.PROTOCOL_VALIDATED,
            recorded_at=datetime.utcnow(),
            target=envelope.target,
            adapter_name=f"stub_{self.venue_id}",
        )

    def get_dispatches(self) -> list[dict]:
        return list(self._dispatches)

    def health(self) -> dict:
        return {"venue_id": self.venue_id, "status": "healthy",
                "dispatch_count": len(self._dispatches)}


class VenueRouter:
    """Routes dispatch requests to venue-specific adapters.

    Selects the adapter based on the envelope's target field
    or the intent's venue field. Falls back to a default adapter
    if no specific one matches.
    """

    def __init__(self, default_adapter=None):
        self._adapters: dict[str, VenueAdapter] = {}
        self._default = default_adapter
        self._route_log: list[dict] = []

    def register(self, venue_id: str, adapter: VenueAdapter) -> None:
        self._adapters[venue_id] = adapter

    def unregister(self, venue_id: str) -> None:
        self._adapters.pop(venue_id, None)

    def route(self, request, envelope) -> DispatchResult:
        venue = self._resolve_venue(envelope)
        adapter = self._adapters.get(venue, self._default)

        if adapter is None:
            return DispatchResult(
                schema_version=SCHEMA_DISPATCH_RESULT,
                dispatch_id=request.dispatch_id,
                message_id=envelope.message_id,
                status=DispatchStatus.FAILED,
                recorded_at=datetime.utcnow(),
                target=envelope.target,
                adapter_name="venue_router",
                failure_reason=f"no adapter for venue: {venue}",
                trace={"error": f"no adapter for venue: {venue}"},
            )

        self._route_log.append({
            "venue": venue,
            "adapter": adapter.venue_id,
            "message_id": envelope.message_id,
            "routed_at": datetime.utcnow().isoformat(),
        })
        return adapter.dispatch(request, envelope)

    def _resolve_venue(self, envelope) -> str:
        venue = envelope.payload.get("venue", "") if envelope.payload else ""
        if not venue:
            venue = getattr(envelope, "target", "default")
        return venue

    def list_venues(self) -> list[dict]:
        result = []
        for vid, adapter in self._adapters.items():
            result.append(adapter.health())
        return result

    def get_route_log(self, limit: int = 50) -> list[dict]:
        return list(self._route_log[-limit:])
