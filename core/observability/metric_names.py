"""Canonical names for MetricsCollector metrics used across services."""

ENGINE_CONFIG_RELOAD_TOTAL = "engine_config.reload_total"

# High-level decision pipeline (e.g. diagnostics)
DECISIONS_TOTAL = "decisions.total"

# Decision cycle (DecisionCycleOrchestrator)
CYCLES_TOTAL = "cycles.total"
CYCLES_ALLOWED = "cycles.allowed"
CYCLES_BLOCKED = "cycles.blocked"
CYCLES_ERRORS = "cycles.errors"
CYCLES_THROTTLED = "cycles.throttled"
CYCLES_CIRCUIT_OPEN = "cycles.circuit_open"

VENUE_EVENTS_PREFIX = "venue_events"


def venue_events_metric(event_type: str) -> str:
    """MetricsCollector name for orchestrator-scoped venue event counters."""
    return f"{VENUE_EVENTS_PREFIX}.{event_type}"


# Dispatch (SloService, diagnostics)
DISPATCH_FAILED = "dispatch.failed"
DISPATCH_TRANSPORT_DELIVERED = "dispatch.transport_delivered"
DISPATCH_PROTOCOL_VALIDATED = "dispatch.protocol_validated"
DISPATCH_SKIPPED = "dispatch.skipped"
DISPATCH_REJECTED = "dispatch.rejected"

# Reconciliation
RECONCILIATION_MATCHED = "reconciliation.matched"
RECONCILIATION_BREACHED = "reconciliation.breached"
RECONCILIATION_UNMATCHED = "reconciliation.unmatched"
RECONCILIATION_PARTIAL = "reconciliation.partial"

# ExecutionManager (venue events; dynamic event_type)
EXECUTION_PREFIX = "execution"
EXECUTION_FILL_QUANTITY = "execution.fill_quantity"


def execution_event_metric(event_type: str) -> str:
    """MetricsCollector name for execution-manager venue event counters."""
    return f"{EXECUTION_PREFIX}.{event_type}"


# Paper execution gateway
PAPER_EXECUTION_FILLED = "paper_execution.filled"
PAPER_EXECUTION_FILL_QUANTITY = "paper_execution.fill_quantity"

# Lifecycle
LIFECYCLE_STARTUPS = "lifecycle.startups"

# BatchProcessor
BATCH_TOTAL_TRIGGERS = "batch.total_triggers"
BATCH_ERRORS = "batch.errors"
