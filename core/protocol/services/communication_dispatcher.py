from datetime import datetime

from core.contracts.domain.dispatch_request import DispatchRequest
from core.contracts.domain.dispatch_result import DispatchResult
from core.contracts.domain_keys import (
    DISPATCH_FAILURE_REASON_LIVE_DISPATCH_DISABLED,
    DISPATCH_FAILURE_REASON_LIVE_READ_ONLY,
    DISPATCH_FAILURE_REASON_SYMBOL_NOT_LIVE_ENABLED,
)
from core.contracts.enums import DispatchStatus
from core.contracts.ids import new_dispatch_id
from core.observability.metric_names import (
    DISPATCH_FAILED,
    DISPATCH_PROTOCOL_VALIDATED,
    DISPATCH_SKIPPED,
    DISPATCH_TRANSPORT_DELIVERED,
)
from core.protocol.schema_versions import SCHEMA_DISPATCH_REQUEST, SCHEMA_DISPATCH_RESULT
from core.protocol.services.communication_adapter_registry import CommunicationAdapterRegistry


class CommunicationDispatcher:
    def __init__(
        self,
        adapter=None,
        adapter_registry: CommunicationAdapterRegistry | None = None,
        clock=datetime.utcnow,
        idempotency_store=None,
        live_read_only: bool = False,
        live_dispatch_enabled: bool = True,
        live_allowed_symbols: tuple[str, ...] = (),
        metrics=None,
        file_wal_adapter=None,  # Phase 3: WAL adapter for dual-write durability
    ):
        self._adapter = adapter
        self._adapter_registry = adapter_registry
        self._clock = clock
        self._idempotency_store = idempotency_store
        self._live_read_only = live_read_only
        self._live_dispatch_enabled = live_dispatch_enabled
        self._live_allowed_symbols = tuple(live_allowed_symbols)
        self._metrics = metrics
        self._file_wal_adapter = file_wal_adapter  # Phase 3

    def dispatch(self, envelope, *, route_policy=None, transport_hints=None, governance=None):
        route_policy = route_policy or {}
        transport_hints = transport_hints or {}
        governance = governance or {}

        request = DispatchRequest(
            schema_version=SCHEMA_DISPATCH_REQUEST,
            dispatch_id=new_dispatch_id(),
            envelope=envelope,
            requested_at=self._clock(),
            route_policy=route_policy,
            transport_hints=transport_hints,
            governance=governance,
        )
        attempts = []

        if self._live_read_only:
            if self._metrics:
                self._metrics.inc(DISPATCH_FAILED)
            return DispatchResult(
                schema_version=SCHEMA_DISPATCH_RESULT,
                dispatch_id=request.dispatch_id,
                message_id=envelope.message_id,
                status=DispatchStatus.FAILED,
                recorded_at=request.requested_at,
                target=envelope.target,
                adapter_name="live_read_only_guard",
                failure_reason=DISPATCH_FAILURE_REASON_LIVE_READ_ONLY,
                attempts=[
                    {
                        "adapter_name": "live_read_only_guard",
                        "status": "failed",
                        "reason": DISPATCH_FAILURE_REASON_LIVE_READ_ONLY,
                    }
                ],
                trace={
                    "live_read_only": True,
                },
            )

        symbol = envelope.payload.get("symbol") if isinstance(envelope.payload, dict) else None
        if not self._live_dispatch_enabled:
            if self._metrics:
                self._metrics.inc(DISPATCH_FAILED)
            return DispatchResult(
                schema_version=SCHEMA_DISPATCH_RESULT,
                dispatch_id=request.dispatch_id,
                message_id=envelope.message_id,
                status=DispatchStatus.FAILED,
                recorded_at=request.requested_at,
                target=envelope.target,
                adapter_name="live_dispatch_gate",
                failure_reason=DISPATCH_FAILURE_REASON_LIVE_DISPATCH_DISABLED,
                attempts=[
                    {
                        "adapter_name": "live_dispatch_gate",
                        "status": "failed",
                        "reason": DISPATCH_FAILURE_REASON_LIVE_DISPATCH_DISABLED,
                    }
                ],
                trace={
                    "live_dispatch_enabled": False,
                    "symbol": symbol,
                },
            )

        if self._live_allowed_symbols and symbol not in self._live_allowed_symbols:
            if self._metrics:
                self._metrics.inc(DISPATCH_FAILED)
            return DispatchResult(
                schema_version=SCHEMA_DISPATCH_RESULT,
                dispatch_id=request.dispatch_id,
                message_id=envelope.message_id,
                status=DispatchStatus.FAILED,
                recorded_at=request.requested_at,
                target=envelope.target,
                adapter_name="live_symbol_gate",
                failure_reason=DISPATCH_FAILURE_REASON_SYMBOL_NOT_LIVE_ENABLED,
                attempts=[
                    {
                        "adapter_name": "live_symbol_gate",
                        "status": "failed",
                        "reason": DISPATCH_FAILURE_REASON_SYMBOL_NOT_LIVE_ENABLED,
                    }
                ],
                trace={
                    "live_allowed_symbols": list(self._live_allowed_symbols),
                    "symbol": symbol,
                },
            )

        if self._idempotency_store is not None and envelope.idempotency_key:
            claim = self._idempotency_store.check_and_claim(
                idempotency_key=envelope.idempotency_key,
                message_id=envelope.message_id,
                date_key=request.requested_at.strftime("%Y-%m-%d"),
            )
            if claim.get("duplicate"):
                if self._metrics:
                    self._metrics.inc(DISPATCH_SKIPPED)
                return DispatchResult(
                    schema_version=SCHEMA_DISPATCH_RESULT,
                    dispatch_id=request.dispatch_id,
                    message_id=envelope.message_id,
                    status=DispatchStatus.FAILED,
                    recorded_at=request.requested_at,
                    target=envelope.target,
                    adapter_name="idempotency_guard",
                    failure_reason=f"duplicate idempotency key: {envelope.idempotency_key}",
                    attempts=[
                        {
                            "adapter_name": "idempotency_guard",
                            "status": "failed",
                            "reason": "duplicate_idempotency_key",
                        }
                    ],
                    trace={
                        "idempotency_key": envelope.idempotency_key,
                        "original_message_id": claim.get("original_message_id"),
                    },
                )

        if envelope.deadline_at is not None and request.requested_at > envelope.deadline_at:
            if self._metrics:
                self._metrics.inc(DISPATCH_FAILED)
            return DispatchResult(
                schema_version=SCHEMA_DISPATCH_RESULT,
                dispatch_id=request.dispatch_id,
                message_id=envelope.message_id,
                status=DispatchStatus.FAILED,
                recorded_at=request.requested_at,
                target=envelope.target,
                adapter_name="deadline_guard",
                failure_reason="dispatch deadline exceeded before attempt",
                attempts=[
                    {
                        "adapter_name": "deadline_guard",
                        "status": "failed",
                        "reason": "deadline_exceeded",
                    }
                ],
                trace={"deadline_at": envelope.deadline_at},
            )

        # ── Phase 3: Write-Ahead Log (WAL) — file-first durability ───────
        # File outbox write MUST succeed before we attempt ZMQ.  If the
        # process crashes between the file write and the ZMQ send, the
        # bridge worker's slow file poll (5s interval) picks up the orphan.
        # This is the "D" in ACID for our dispatch pipeline.
        wal_result: DispatchResult | None = None
        if self._file_wal_adapter is not None:
            try:
                wal_result = self._file_wal_adapter.dispatch(request, envelope)
            except Exception as wal_exc:  # BLE001:FOG (Sev 3, Phase 3b)
                if self._metrics:
                    self._metrics.inc(DISPATCH_FAILED)
                return DispatchResult(
                    schema_version=SCHEMA_DISPATCH_RESULT,
                    dispatch_id=request.dispatch_id,
                    message_id=envelope.message_id,
                    status=DispatchStatus.FAILED,
                    recorded_at=request.requested_at,
                    target=envelope.target,
                    adapter_name="wal_guard",
                    failure_reason=f"WAL write failed: {wal_exc}",
                    attempts=[{
                        "adapter_name": "wal_guard",
                        "status": "failed",
                        "reason": f"WAL write failed: {wal_exc}",
                    }],
                    trace={"wal_failed": True},
                )
        primary_adapter = self._resolve_adapter(
            envelope,
            route_policy=route_policy,
            transport_hints=transport_hints,
            governance=governance,
        )
        primary_adapter_name = getattr(
            primary_adapter, "adapter_name", primary_adapter.__class__.__name__
        )

        try:
            result = primary_adapter.dispatch(request, envelope)
            result.attempts = result.attempts or []
            result.attempts.append(
                {
                    "adapter_name": primary_adapter_name,
                    "status": "succeeded",
                    "reason": None,
                }
            )
            # ── Phase 3: Merge WAL metadata into primary result ──────────
            if wal_result is not None:
                result.transport_metadata = {
                    **(result.transport_metadata or {}),
                    "wal_outbox_path": wal_result.transport_metadata.get("outbox_path", ""),
                    "wal_bytes_written": wal_result.transport_metadata.get("bytes_written", 0),
                    "wal_delivery_channel": wal_result.protocol_metadata.get("delivery_channel", ""),
                }
            if self._metrics:
                self._metrics.inc(DISPATCH_TRANSPORT_DELIVERED)
            return result
        except Exception as exc:  # BLE001:FOG
            attempts.append(
                {
                    "adapter_name": primary_adapter_name,
                    "status": "failed",
                    "reason": str(exc),
                }
            )
            # ── Phase 3: If WAL succeeded, return DEGRADED instead of FAILED ──
            # The file outbox already has the order — bridge slow poll will pick it up.
            # ZMQ was an acceleration, not the durability guarantee.
            if wal_result is not None:
                wal_result.status = DispatchStatus.DEGRADED
                wal_result.degrade_reason = f"primary={exc}; wal_persisted"
                wal_result.fallback_adapter_name = "wal_file_outbox"
                wal_result.attempts = attempts
                wal_result.trace = {
                    **wal_result.trace,
                    "primary_adapter": primary_adapter_name,
                    "primary_error": str(exc),
                    "wal_persisted": True,
                }
                if self._metrics:
                    self._metrics.inc(DISPATCH_PROTOCOL_VALIDATED)
                return wal_result
            fallback_adapter = self._resolve_fallback_adapter(route_policy)
            if fallback_adapter is None:
                if self._metrics:
                    self._metrics.inc(DISPATCH_FAILED)
                return DispatchResult(
                    schema_version=SCHEMA_DISPATCH_RESULT,
                    dispatch_id=request.dispatch_id,
                    message_id=envelope.message_id,
                    status=DispatchStatus.FAILED,
                    recorded_at=request.requested_at,
                    target=envelope.target,
                    adapter_name=primary_adapter_name,
                    failure_reason=str(exc),
                    attempts=attempts,
                    trace={"failed_adapter": primary_adapter_name},
                )

            fallback_adapter_name = getattr(
                fallback_adapter, "adapter_name", fallback_adapter.__class__.__name__
            )
            try:
                fallback_result = fallback_adapter.dispatch(request, envelope)
                attempts.append(
                    {
                        "adapter_name": fallback_adapter_name,
                        "status": "degraded",
                        "reason": "fallback_success",
                    }
                )
                fallback_result.status = DispatchStatus.DEGRADED
                fallback_result.degrade_reason = str(exc)
                fallback_result.fallback_adapter_name = fallback_adapter_name
                fallback_result.attempts = attempts
                fallback_result.trace = {
                    **fallback_result.trace,
                    "failed_adapter": primary_adapter_name,
                    "fallback_adapter": fallback_adapter_name,
                }
                if self._metrics:
                    self._metrics.inc(DISPATCH_PROTOCOL_VALIDATED)
                return fallback_result
            except Exception as fallback_exc:  # BLE001:FOG
                attempts.append(
                    {
                        "adapter_name": fallback_adapter_name,
                        "status": "failed",
                        "reason": str(fallback_exc),
                    }
                )
                if self._metrics:
                    self._metrics.inc(DISPATCH_FAILED)
                return DispatchResult(
                    schema_version=SCHEMA_DISPATCH_RESULT,
                    dispatch_id=request.dispatch_id,
                    message_id=envelope.message_id,
                    status=DispatchStatus.FAILED,
                    recorded_at=request.requested_at,
                    target=envelope.target,
                    adapter_name=primary_adapter_name,
                    failure_reason=f"primary={exc}; fallback={fallback_exc}",
                    fallback_adapter_name=fallback_adapter_name,
                    attempts=attempts,
                    trace={
                        "failed_adapter": primary_adapter_name,
                        "fallback_adapter": fallback_adapter_name,
                    },
                )
    def _resolve_adapter(self, envelope, *, route_policy, transport_hints, governance):
        if self._adapter is not None:
            return self._adapter
        if self._adapter_registry is not None:
            return self._adapter_registry.resolve(
                target=envelope.target,
                message_type=envelope.message_type,
                route_policy=route_policy,
                transport_hints=transport_hints,
                governance=governance,
            )
        raise ValueError("communication dispatcher requires either adapter or adapter_registry")

    def _resolve_fallback_adapter(self, route_policy):
        fallback_adapter_name = route_policy.get("fallback_adapter")
        if not fallback_adapter_name or self._adapter_registry is None:
            return None
        return self._adapter_registry.resolve(
            target="__fallback__",
            message_type="__fallback__",
            route_policy={"adapter": fallback_adapter_name},
            transport_hints={},
            governance={},
        )
