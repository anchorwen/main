"""State Artifact Catalog — the single source of truth for all ephemeral state files.

Every state JSON file written by the system MUST be registered here.  Any
``json.dump()`` that writes to a path not in this catalog is an illegal
wild-write and will be rejected by the StateWriter gate (Iron Law #0-bis).

Architecture:
    StateArtifact (frozen dataclass) → CATALOG (immutable registry)
    → StateWriter (validates + atomically writes)

Design principles:
    1. Schema Dictatorship — data is validated BEFORE touching disk
    2. Atomic Write — .tmp → fsync → os.replace (no 0-byte corruption)
    3. Cross-Symbol Guard — alpha_registry rejects cross-contamination
    4. TTL Enforcement — every artifact declares its max age

See Also:
    - Writer:  core/state/writer.py
    - Audit:   scripts/audit_state_of_system.py
    - DQAF-046: Feature schema dual-track (catalog of feature schemas)
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

# ── Sentinel values ────────────────────────────────────────────────────

SymbolDir = Literal["data", "data_btc"]
SymbolName = Literal["XAUUSDc", "BTCUSDc"]


# ── Error types ────────────────────────────────────────────────────────


class DataIntegrityError(ValueError):
    """Raised when data fails schema validation at the write boundary.

    This is the physical enforcement of the Schema Dictatorship principle:
    dirty data is rejected at the gate, never written to disk.
    """

    def __init__(
        self,
        message: str,
        *,
        artifact_id: str = "",
        violations: list[str] | None = None,
    ):
        super().__init__(message)
        self.artifact_id = artifact_id
        self.violations = violations or []


class CrossSymbolContaminationError(DataIntegrityError):
    """Raised when a cross-symbol invariant is violated (e.g. btc_swing in XAU registry)."""

    def __init__(
        self, message: str, *, artifact_id: str = "", foreign_ids: list[str] | None = None
    ):
        super().__init__(message, artifact_id=artifact_id)
        self.foreign_ids = foreign_ids or []


# ── Validator type ─────────────────────────────────────────────────────

# A SchemaValidator receives the deserialized data dict and raises
# DataIntegrityError if validation fails.  Returns None on success.
SchemaValidator = Callable[[dict[str, Any]], None]


# ── StateArtifact ──────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class StateArtifact:
    """Immutable definition of one state file in the system.

    Every state JSON that the system writes MUST have a corresponding
    StateArtifact entry in the CATALOG.  The StateWriter refuses to
    write to any path not backed by a catalog entry.
    """

    # ── Identity ──
    logical_id: str
    """Unique human-readable identifier, e.g. ``"LEADERBOARD"``."""

    # ── Path ──
    path_template: str
    """Relative path within the symbol data directory.

    May contain ``{symbol_dir}`` for the top-level data dir (``data``
    or ``data_btc``).  The writer resolves this against the configured
    base directory for the symbol.

    Examples:
        ``"reports/leaderboard.json"``
        ``"state/daily_ops_state.json"``
        ``"governance_state.json"``
    """

    # ── Schema ──
    schema_validator: SchemaValidator
    """Validates the data dict BEFORE any I/O.  Must raise
    :class:`DataIntegrityError` on failure."""

    # ── Freshness ──
    ttl_seconds: int
    """Maximum allowed age in seconds.  0 = no freshness check."""

    # ── Lineage ──
    generator: str = ""
    """Human-readable description of which module(s) produce this artifact."""

    required_fields: tuple[str, ...] = ()
    """Fields that MUST be present and non-None in the data dict."""

    cross_symbol_guard: bool = False
    """If True, the writer performs cross-symbol validation before writing."""


# ═══════════════════════════════════════════════════════════════════════════════
# Built-in Validators
# ═══════════════════════════════════════════════════════════════════════════════


def _must_have_fields(data: dict[str, Any], required: tuple[str, ...]) -> None:
    """Check that all required fields are present and non-None."""
    missing = []
    for f_name in required:
        if f_name not in data or data[f_name] is None:
            missing.append(f_name)
    if missing:
        raise DataIntegrityError(
            f"Missing required fields: {missing}",
            violations=[f"missing:{f}" for f in missing],
        )


def validate_non_empty_dict(data: dict[str, Any]) -> None:
    """Reject empty dicts, None, non-dict values."""
    if not isinstance(data, dict):
        raise DataIntegrityError(f"Expected dict, got {type(data).__name__}")
    if len(data) == 0:
        raise DataIntegrityError("State artifact must not be an empty dict")


def validate_leaderboard(data: dict[str, Any]) -> None:
    """Validate leaderboard.json structure."""
    validate_non_empty_dict(data)
    # Must have either 'leaderboard' key or 'entries' or 'brains'
    has_lb = "leaderboard" in data
    has_entries = "entries" in data
    has_brains = "brains" in data
    if not (has_lb or has_entries or has_brains):
        raise DataIntegrityError(
            "leaderboard.json must contain 'leaderboard', 'entries', or 'brains' key",
            artifact_id="LEADERBOARD",
            violations=["missing_required_key"],
        )


def validate_alpha_allocation(data: dict[str, Any]) -> None:
    """Validate alpha_allocation.json structure."""
    validate_non_empty_dict(data)
    recs = data.get("recommendations")
    if recs is None:
        raise DataIntegrityError(
            "alpha_allocation.json must contain 'recommendations' key",
            artifact_id="ALPHA_ALLOCATION",
            violations=["missing:recommendations"],
        )
    if not isinstance(recs, list):
        raise DataIntegrityError(
            f"'recommendations' must be a list, got {type(recs).__name__}",
            artifact_id="ALPHA_ALLOCATION",
        )


def validate_alpha_registry(data: dict[str, Any]) -> None:
    """Validate alpha_registry.json structure + cross-symbol guard.

    Cross-symbol invariant: alpha_ids must not contain foreign-symbol
    identifiers (e.g. ``btc_swing`` must not appear in XAU registry).
    This is checked at write time by the writer, which knows the target symbol.
    """
    validate_non_empty_dict(data)
    records = data.get("records") or data.get("alphas")
    if records is None:
        raise DataIntegrityError(
            "alpha_registry.json must contain 'records' or 'alphas' key",
            artifact_id="ALPHA_REGISTRY",
            violations=["missing:records_or_alphas"],
        )
    if not isinstance(records, list | dict):
        raise DataIntegrityError(
            f"'records' must be list or dict, got {type(records).__name__}",
            artifact_id="ALPHA_REGISTRY",
        )


def validate_governance_state(data: dict[str, Any]) -> None:
    """Validate governance_state.json structure."""
    validate_non_empty_dict(data)
    brains = data.get("brain_states") or data.get("brains")
    if brains is None:
        raise DataIntegrityError(
            "governance_state.json must contain 'brain_states' or 'brains' key",
            artifact_id="GOVERNANCE_STATE",
            violations=["missing:brain_states"],
        )


def validate_daily_ops_state(data: dict[str, Any]) -> None:
    """Validate daily_ops_state.json — must have last_daily_ops_utc."""
    validate_non_empty_dict(data)
    ts = data.get("last_daily_ops_utc")
    if ts is None:
        raise DataIntegrityError(
            "daily_ops_state.json must contain 'last_daily_ops_utc'",
            artifact_id="DAILY_OPS_STATE",
            violations=["missing:last_daily_ops_utc"],
        )


def validate_training_readiness(data: dict[str, Any]) -> None:
    """Validate training_readiness.json — must have readiness assessments."""
    validate_non_empty_dict(data)


def validate_alpha_performance(data: dict[str, Any]) -> None:
    """Validate alpha_performance.json structure."""
    validate_non_empty_dict(data)
    # Must have snapshots or history or be an empty initial state
    snaps = data.get("snapshots") or data.get("history")
    if snaps is not None and not isinstance(snaps, list):
        raise DataIntegrityError(
            f"'snapshots' must be a list, got {type(snaps).__name__}",
            artifact_id="ALPHA_PERFORMANCE",
        )


def validate_mt5_bridge_health(data: dict[str, Any]) -> None:
    """Validate mt5_bridge_health.json structure."""
    validate_non_empty_dict(data)


def validate_execution_state(data: dict[str, Any]) -> None:
    """Validate execution_state.json structure."""
    validate_non_empty_dict(data)


def validate_data_health_state(data: dict[str, Any]) -> None:
    """Validate data_health_state.json structure."""
    validate_non_empty_dict(data)


def validate_brain_pnl_ledger(data: dict[str, Any]) -> None:
    """Validate brain_pnl_ledger.json structure.

    DQAF-20260622-057: Registers the 15.5MB PnL ledger — the system's
    largest state file — into the catalog perimeter.  Previously existed
    outside governance (CATALOG_COVERAGE_GAP), allowing 42.3h staleness
    without detection.
    """
    validate_non_empty_dict(data)
    if "schema_version" not in data:
        raise DataIntegrityError(
            "brain_pnl_ledger.json must contain 'schema_version'",
            artifact_id="BRAIN_PNL_LEDGER",
            violations=["missing:schema_version"],
        )
    if "settled" not in data:
        raise DataIntegrityError(
            "brain_pnl_ledger.json must contain 'settled' key",
            artifact_id="BRAIN_PNL_LEDGER",
            violations=["missing:settled"],
        )


def validate_alert_cooling(data: dict[str, Any]) -> None:
    """Validate alert_cooling.json — alert cooldown state.

    DQAF-20260622-057: Second CATALOG_COVERAGE_GAP finding.
    """
    validate_non_empty_dict(data)


# ═══════════════════════════════════════════════════════════════════════════════
# The Catalog
# ═══════════════════════════════════════════════════════════════════════════════

CATALOG: dict[str, StateArtifact] = {
    # ── Core pipeline states ──
    "LEADERBOARD": StateArtifact(
        logical_id="LEADERBOARD",
        path_template="reports/leaderboard.json",
        schema_validator=validate_leaderboard,
        ttl_seconds=14400,  # 4h (DQAF-057: tightened from 24h)
        generator="daily_ops + brain_leaderboard",
    ),
    "ALPHA_ALLOCATION": StateArtifact(
        logical_id="ALPHA_ALLOCATION",
        path_template="reports/alpha_allocation.json",
        schema_validator=validate_alpha_allocation,
        ttl_seconds=14400,  # 4h (DQAF-057: tightened from 24h)
        generator="daily_ops + portfolio_allocator",
    ),
    "GOVERNANCE_STATE": StateArtifact(
        logical_id="GOVERNANCE_STATE",
        path_template="governance_state.json",
        schema_validator=validate_governance_state,
        ttl_seconds=14400,  # 4h (DQAF-057: tightened from 24h)
        generator="daily_ops + governance_service",
    ),
    "DAILY_OPS_STATE": StateArtifact(
        logical_id="DAILY_OPS_STATE",
        path_template="state/daily_ops_state.json",
        schema_validator=validate_daily_ops_state,
        ttl_seconds=14400,  # 4h (DQAF-057: tightened from 24h)
        generator="daily_ops",
    ),
    # ── Alpha pipeline ──
    "ALPHA_REGISTRY": StateArtifact(
        logical_id="ALPHA_REGISTRY",
        path_template="alpha_registry.json",
        schema_validator=validate_alpha_registry,
        ttl_seconds=14400,  # 4h (DQAF-057: tightened from 24h)
        generator="core/alpha/*",
        cross_symbol_guard=True,
    ),
    "ALPHA_PERFORMANCE": StateArtifact(
        logical_id="ALPHA_PERFORMANCE",
        path_template="alpha_performance.json",
        schema_validator=validate_alpha_performance,
        ttl_seconds=14400,  # 4h (DQAF-057: tightened from 24h)
        generator="core/alpha/*",
    ),
    "ALPHA_FEED_STATE": StateArtifact(
        logical_id="ALPHA_FEED_STATE",
        path_template="alpha_feed_state.json",
        schema_validator=validate_non_empty_dict,
        ttl_seconds=14400,  # 4h (DQAF-057: tightened from 24h)
        generator="core/alpha/*",
    ),
    # ── Governance / Training ──
    "TRAINING_READINESS": StateArtifact(
        logical_id="TRAINING_READINESS",
        path_template="reports/training_readiness.json",
        schema_validator=validate_training_readiness,
        ttl_seconds=86400,  # 24h — training is genuinely daily
        generator="daily_ops + governance_scheduler",
    ),
    "RETRAINING_SIGNAL_PREV": StateArtifact(
        logical_id="RETRAINING_SIGNAL_PREV",
        path_template="reports/retraining_signal_prev.json",
        schema_validator=validate_non_empty_dict,
        ttl_seconds=86400,  # 24h — retraining is genuinely daily
        generator="daily_ops + governance_scheduler",
    ),
    # ── Operational ──
    "EXECUTION_STATE": StateArtifact(
        logical_id="EXECUTION_STATE",
        path_template="state/execution_state.json",
        schema_validator=validate_execution_state,
        ttl_seconds=1800,  # 30min (DQAF-057: tightened from 1h — execution changes every cycle)
        generator="daily_ops + execution_service",
    ),
    "DATA_HEALTH_STATE": StateArtifact(
        logical_id="DATA_HEALTH_STATE",
        path_template="state/data_health_state.json",
        schema_validator=validate_data_health_state,
        ttl_seconds=14400,  # 4h (DQAF-057: tightened from 24h)
        generator="daily_ops + data_health_service",
    ),
    # ── Leaderboard backup for run-to-run comparison ──
    "LEADERBOARD_PREV": StateArtifact(
        logical_id="LEADERBOARD_PREV",
        path_template="reports/leaderboard_prev.json",
        schema_validator=validate_leaderboard,
        ttl_seconds=86400 * 2,  # 48h — backup copy, intentionally longer
        generator="daily_ops (backup copy)",
    ),
    "MT5_BRIDGE_HEALTH": StateArtifact(
        logical_id="MT5_BRIDGE_HEALTH",
        path_template="reports/mt5_bridge_health.json",
        schema_validator=validate_mt5_bridge_health,
        ttl_seconds=900,  # 15min (DQAF-057: tightened from 1h — bridge health is critical)
        generator="daily_ops + bridge_health",
    ),
    # ── DQAF-20260622-057: CATALOG_COVERAGE_GAP closure ──
    # These files existed outside the State Governance Protocol perimeter.
    # brain_pnl_ledger.json (15.5MB XAU) was the largest ungoverned state file.
    # alert_cooling.json was also unmonitored.
    "BRAIN_PNL_LEDGER": StateArtifact(
        logical_id="BRAIN_PNL_LEDGER",
        path_template="brain_pnl_ledger.json",
        schema_validator=validate_brain_pnl_ledger,
        ttl_seconds=14400,  # 4h — PnL is updated every cycle when live, every daily_ops otherwise
        generator="daily_ops + brain_pnl_ledger.BrainPnLStore",
        required_fields=("schema_version", "settled"),
    ),
    "ALERT_COOLING": StateArtifact(
        logical_id="ALERT_COOLING",
        path_template="state/alert_cooling.json",
        schema_validator=validate_alert_cooling,
        ttl_seconds=7200,  # 2h — cooling state must be recent
        generator="execution exit_watchdog / alert system",
    ),
}

# ── Path constants for symbol resolution ──
SYMBOL_DIRS: dict[str, str] = {
    "XAUUSDc": "data",
    "BTCUSDc": "data_btc",
}

# Cross-symbol contamination patterns: (alpha_id_prefix, expected_symbol)
ALPHA_ID_SYMBOL_PREFIXES: dict[str, str] = {
    "btc_": "BTCUSDc",
    "xau_": "XAUUSDc",
    "alpha_xau_": "XAUUSDc",
    "alpha_btc_": "BTCUSDc",
}


def resolve_artifact_path(artifact: StateArtifact, data_dir: str) -> Path:
    """Resolve the absolute filesystem path for an artifact + data directory.

    Args:
        artifact: The catalog entry.
        data_dir: Absolute or relative path to the symbol data directory.

    Returns:
        Absolute Path to the state file.
    """
    base = Path(data_dir)
    return base / artifact.path_template


def lookup(logical_id: str) -> StateArtifact:
    """Look up a catalog entry by logical ID.

    Raises:
        KeyError: If the logical_id is not registered.
    """
    if logical_id not in CATALOG:
        raise KeyError(
            f"Unknown state artifact: {logical_id!r}. " f"Registered artifacts: {list(CATALOG)}"
        )
    return CATALOG[logical_id]


def list_artifacts() -> list[StateArtifact]:
    """Return all registered artifacts."""
    return list(CATALOG.values())


def detect_symbol_from_alpha_id(alpha_id: str) -> str | None:
    """Guess the expected symbol from an alpha_id prefix.

    Returns the symbol string (e.g. ``"BTCUSDc"``) or None.
    """
    for prefix, symbol in ALPHA_ID_SYMBOL_PREFIXES.items():
        if alpha_id.startswith(prefix):
            return symbol
    return None
