"""PnL null-guard and safe label classification.

Prevents the pnl=null → 0 → "breakeven" cascade that has contaminated
downstream metrics (governance WR/PF, calibrator, brain leaderboard).

Core invariant: PnL that has NOT been confirmed by MT5 deal.profit MUST NOT
be written as 0.0 into the journal.  A null PnL means "we don't know yet" —
a 0.0 PnL means "trade was exactly break-even," which is an assertion of
fact that requires MT5 confirmation.

Usage:
    from core.ledger.services.pnl_guard import PnlGuard

    entry = PnlGuard.guard_close_entry(raw_close_dict)
    label = PnlGuard.classify_label(entry)
"""

from __future__ import annotations

from typing import Any


class PnlGuard:
    """Enforces PnL integrity at the journal write boundary."""

    # ── _pnl_status tags ─────────────────────────────────────────────────
    PNL_VERIFIED: str = "verified_from_mt5_deal"
    """PnL sourced directly from MT5 deal.profit — authoritative."""

    PNL_PENDING: str = "pending_mt5_confirmation"
    """PnL is null or estimated — MT5 deal data not yet available."""

    PNL_ESTIMATED: str = "estimated_from_close_price"
    """PnL computed from (close_price - entry_price) * volume — close to actual
    but does NOT account for slippage, commission, or swap."""

    # ── Labels ───────────────────────────────────────────────────────────
    LABEL_UNKNOWN_PENDING: str = "unknown_pnl_pending"
    """Label when PnL is null and awaiting MT5 confirmation."""

    LABEL_UNKNOWN: str = "unknown_pnl"
    """Label when PnL is null and no MT5 confirmation is expected."""

    # ── Public API ───────────────────────────────────────────────────────

    @staticmethod
    def guard_close_entry(entry: dict[str, Any]) -> dict[str, Any]:
        """Validate and tag a close entry before journal write.

        Rules:
        1. If ``detail.profit`` (MT5 deal.profit) is present, use it — it is
           the broker-authoritative PnL.  Tag as ``verified_from_mt5_deal``.
        2. If PnL is None, mark as ``pending_mt5_confirmation`` — do NOT
           silently convert to 0.0.
        3. If PnL is exactly 0.0 or within float epsilon of zero, mark as
           ``pending_mt5_confirmation`` unless verified by rule 1.

        Returns the (possibly mutated) entry dict.
        """
        detail = entry.get("detail", {})
        deal_profit = None

        if isinstance(detail, dict):
            deal_profit = detail.get("profit")

        # Rule 1: MT5 deal.profit is authoritative
        if deal_profit is not None:
            try:
                entry["pnl"] = float(deal_profit)
                entry["_pnl_status"] = PnlGuard.PNL_VERIFIED
                if isinstance(detail, dict):
                    detail["pnl"] = float(deal_profit)
                return entry
            except (ValueError, TypeError):
                pass  # Fall through to PnL check below

        pnl = entry.get("pnl")

        # Rule 2: Null PnL → pending
        if pnl is None:
            entry["_pnl_status"] = PnlGuard.PNL_PENDING
            return entry

        # Rule 3: Zero (or near-zero) PnL → pending, unless already verified
        try:
            if abs(float(pnl)) < 0.0001:
                entry["_pnl_status"] = PnlGuard.PNL_PENDING
                entry["pnl"] = None  # Explicitly null — never write false zero
                return entry
        except (ValueError, TypeError):
            entry["_pnl_status"] = PnlGuard.PNL_PENDING
            return entry

        # Non-zero PnL without deal verification → estimated
        entry["_pnl_status"] = PnlGuard.PNL_ESTIMATED
        return entry

    @staticmethod
    def classify_label(entry: dict[str, Any]) -> str:
        """Safe label classification — never calls null-PnL 'breakeven'.

        Prefer the existing label if already set and valid.
        Only re-classify when PnL is verified.
        """
        pnl = entry.get("pnl")
        status = entry.get("_pnl_status", "")

        # If PnL is null and unverified → unknown, not breakeven
        if pnl is None:
            if status == PnlGuard.PNL_PENDING:
                return PnlGuard.LABEL_UNKNOWN_PENDING
            return PnlGuard.LABEL_UNKNOWN

        # Explicit label already set by a trusted path
        existing_label = entry.get("label")
        if existing_label and existing_label not in (
            "breakeven",  # Most suspect — could be pnl=0 bug
            "close_accepted",  # Legacy pre-FIX-20260612-004 label
        ):
            return existing_label

        # Classify from verified or estimated PnL
        try:
            pnl_float = float(pnl)
        except (ValueError, TypeError):
            return PnlGuard.LABEL_UNKNOWN

        if pnl_float > 0:
            return "win"
        if pnl_float < 0:
            return "loss"

        # Truly zero PnL after verification → breakeven
        if status == PnlGuard.PNL_VERIFIED:
            return "breakeven"

        # Zero PnL without verification → pending, not breakeven
        return PnlGuard.LABEL_UNKNOWN_PENDING
