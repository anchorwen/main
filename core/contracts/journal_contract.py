"""Journal entry runtime contract — Pydantic validator for live_trade_journal.

Column 2 (Institutional Data SLA): Every journal entry is validated at the
write boundary.  Dirty entries are rejected before they enter the journal —
no silent data corruption, no "mystery NaN PnL", no ambiguous timezones.

Usage:
    from core.contracts.journal_contract import JournalAccepted, JournalClosed

    # Before writing to journal:
    entry = JournalAccepted(**payload_dict)
    journal_path.write_text(entry.model_dump_json() + "\n")
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field, field_validator

# ── Accepted (open) entry ──────────────────────────────────────────────


class JournalAccepted(BaseModel):
    """An accepted entry in the live trade journal.

    Covers open, modify_sltp, trail, and other accepted-but-not-closed entries.
    The contract focuses on structural integrity (timestamps, NaN rejection)
    rather than per-type field requirements — different entry types have
    different required fields.
    """

    ack_status: Literal["accepted"] = "accepted"
    action: str = Field(default="open")
    symbol: str = Field(default="", min_length=0, max_length=20)
    strategy: str = Field(default="")
    magic: int = Field(default=0)
    side: str = Field(default="")
    volume: float | None = Field(default=None)
    sl: float | None = Field(default=None)
    tp: float | None = Field(default=None)
    message_id: str = Field(default="")
    position_ticket: int | None = Field(default=None)
    recorded_at: str = Field(default="", min_length=0)
    p_win: float | None = Field(default=None)
    confidence: float | None = Field(default=None)
    brain_ids: list[str] | None = Field(default=None)
    brain_votes: list[dict[str, Any]] | None = None
    entry_context: dict[str, Any] | None = None
    kelly_mult: float | None = Field(default=None)
    pnl: float | None = None  # Not yet known at open time
    label: str | None = None  # None for open, 'trail' for modify_sltp
    open_message_id: str | None = None
    schema_version: str = Field(default="live_trade_journal.v2")
    target: str = Field(default="exec_bridge")
    detail: dict[str, Any] = Field(default_factory=dict)

    @field_validator("recorded_at")
    @classmethod
    def must_be_utc_iso(cls, v: str) -> str:
        """Validate timestamp as UTC ISO-8601.

        Accepts both explicit UTC ('2026-06-14T10:00:00Z') and implicit UTC
        ('2026-05-31T05:32:28') formats.  All system timestamps are UTC by
        convention — the runtime never operates in any other timezone.

        Institutional note: implicit-UTC timestamps are accepted for backward
        compatibility but a warning is logged.  New code should emit explicit
        'Z' suffix per the Institutional Data SLA.
        """
        has_explicit_tz = v.endswith("Z") or "+00:00" in v or v.endswith("+0000")
        try:
            iso_str = v.replace("Z", "+00:00") if v.endswith("Z") else v
            if "+00:00" not in iso_str and "+0000" not in iso_str:
                iso_str += "+00:00"  # Assume implicit UTC
            parsed = datetime.fromisoformat(iso_str)
        except (ValueError, TypeError) as e:
            raise ValueError(f"Timestamp is not valid ISO-8601: '{v}' — {e}") from e
        if not has_explicit_tz:
            import logging

            logging.getLogger("JournalContract").info(
                f"Implicit UTC timestamp accepted (no 'Z' suffix): '{v}'. "
                f"Consider updating write path to emit explicit UTC."
            )
        return v

    @field_validator("p_win")
    @classmethod
    def p_win_must_be_valid(cls, v: float | None) -> float | None:
        if v is None:
            return v
        if math.isnan(v) or math.isinf(v):
            raise ValueError(f"p_win cannot be NaN or Inf, got: {v}")
        if not (0.0 <= v <= 1.0):
            raise ValueError(f"p_win must be in [0, 1], got: {v}")
        return v

    @field_validator("entry_context")
    @classmethod
    def entry_context_must_have_vector(cls, v: dict[str, Any] | None) -> dict[str, Any] | None:
        """Reject journal entries missing ``entry_context.vector``.

        DLR-001 (2026-06-17): 34 real BTC opens were permanently lost for
        training because ``vector`` was absent from ``entry_context``.
        This validator blocks that class of data loss at the write boundary —
        no open entry enters the journal without its feature vector.

        .. note::

            ``entry_context=None`` is tolerated for backward compatibility
            (entries written before this validator was deployed), but a
            warning is logged.
        """
        if v is None:
            import logging

            logging.getLogger("JournalContract").warning(
                "entry_context is None — open entry written without feature context. "
                "This entry will be unusable for training. "
                "See DLR-001 in data_loss_register.jsonl."
            )
            return v
        if not isinstance(v, dict):
            raise ValueError(f"entry_context must be a dict, got {type(v).__name__}")
        vector = v.get("vector")
        if vector is None:
            raise ValueError(
                "entry_context.vector is MISSING — this open entry would be "
                "permanently lost for training. Required by Institutional Data "
                "SLA Column 2 (Data Stream Contract: live_trade_journal). "
                "See DLR-001 in data_loss_register.jsonl."
            )
        if isinstance(vector, list) and len(vector) == 0:
            raise ValueError(
                "entry_context.vector is an EMPTY list — no feature data for training. "
                "See DLR-001 in data_loss_register.jsonl."
            )
        return v

    model_config = {"extra": "allow"}  # Forward-compatible: accept unknown fields


# ── Closed entry ───────────────────────────────────────────────────────


class JournalClosed(BaseModel):
    """A closed entry in the live trade journal."""

    ack_status: Literal["closed"] = "closed"
    action: str = Field(default="close")
    symbol: str = Field(default="", min_length=0, max_length=20)
    strategy: str = Field(default="")
    magic: int = Field(default=0)
    side: str = Field(default="")
    volume: float | None = Field(default=None)
    sl: float | None = Field(default=None)
    tp: float | None = Field(default=None)
    message_id: str = Field(default="")
    position_ticket: int | None = Field(default=None)
    recorded_at: str = Field(default="", min_length=0)
    pnl: float | None = None
    _pnl_status: str | None = None
    label: str | None = None
    open_message_id: str | None = None
    brain_ids: list[str] | None = Field(default=None)
    schema_version: str = "live_trade_journal.v2"
    target: str = "exec_bridge"
    detail: dict[str, Any] = Field(default_factory=dict)

    VALID_LABELS: ClassVar[frozenset[str]] = frozenset(
        {
            "win",
            "loss",
            "breakeven",
            "tp_hit_first",
            "sl_hit_first",
            "sl_hit_trailed",
            "auto_orphan_rejected",
            # Legacy aliases
            "close_accepted",
            # PnL integrity labels (PnlGuard — prevents null→0→breakeven cascade)
            "unknown_pnl_pending",
            "unknown_pnl",
            # Synthetic orphan close labels (cleanup_orphan_opens — unverified PnL)
            "auto_orphan_rejected_unverified",
            "auto_orphan_stale_unverified",
            "auto_orphan_no_ticket_unverified",
        }
    )

    @field_validator("recorded_at")
    @classmethod
    def must_be_utc_iso(cls, v: str) -> str:
        has_explicit_tz = v.endswith("Z") or "+00:00" in v or v.endswith("+0000")
        try:
            iso_str = v.replace("Z", "+00:00") if v.endswith("Z") else v
            if "+00:00" not in iso_str and "+0000" not in iso_str:
                iso_str += "+00:00"
            datetime.fromisoformat(iso_str)
        except (ValueError, TypeError) as e:
            raise ValueError(f"Timestamp is not valid ISO-8601: '{v}' — {e}") from e
        return v

    @field_validator("pnl")
    @classmethod
    def pnl_must_be_finite(cls, v: float | None) -> float | None:
        """PnL can be any real number but must be finite.

        NaN PnL = unaccountable profit/loss — the accounting system cannot
        close the books with NaN entries.  Inf PnL = physically impossible.
        """
        if v is None:
            return v
        if math.isnan(v):
            raise ValueError("PnL cannot be NaN — accounting requires finite PnL")
        if math.isinf(v):
            raise ValueError("PnL cannot be Inf — physically impossible")
        return v

    @field_validator("label")
    @classmethod
    def label_must_be_valid(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if v not in cls.VALID_LABELS:
            raise ValueError(f"Unknown label '{v}'. Valid labels: {sorted(cls.VALID_LABELS)}")
        return v

    model_config = {"extra": "allow"}
