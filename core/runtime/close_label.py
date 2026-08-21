"""resolve_close_label — SSOT for close-label attribution (TECH_DEBT-007).

P6 (DQAF-20260821-001, IC Approved): the close-label decision was scattered
across five producers — position_close_adapter, reconciliation, mia_close,
settlement_queue, and the bridge worker — each with its own divergent logic:

  * three different watchdog short-codes (2-part vs 3-part),
  * a fabricated ``broker:client_close`` for None deal reasons,
  * a third ``broker:reason_N`` fallback format,
  * settlement_queue hardcoding ``sl_hit_first`` with no trail awareness —
    the DQAF-20260806-001 trail-blindspot bug resurrected, and worse: its
    journal entry carries ``_source="mt5_reconciliation"`` so it *supersedes*
    a correct bridge label in the dedup chain.

This module is the ONE place that maps
    ``(deal_reason, deal_comment, trail_active) → canonical close label``.
Every deal-informed producer consumes it, so no fourth path can diverge.

Same extraction pattern as ``deal_selection.py`` (DQAF-20260708-003) and
``calendar.py`` staleness_anchor (FIX-20260821-001): pull the authority out,
force every consumer through it, and let the divergence die at the leaf.
"""

from __future__ import annotations

from typing import Any

# ── MT5 DEAL reason taxonomy (single canonical copy) ─────────────────────
# Full taxonomy lives here; the old per-module copies (adapter:150-159,
# reconciliation:182-191, mia_close:135) are deleted.
DEAL_REASON_CLIENT = 0
DEAL_REASON_MOBILE = 1
DEAL_REASON_WEB = 2
DEAL_REASON_SIGNAL = 3
DEAL_REASON_SL = 4
DEAL_REASON_TP = 5
DEAL_REASON_STOP_OUT = 6
DEAL_REASON_RISK_OUT = 7

DEAL_REASON_MAP: dict[int, str] = {
    DEAL_REASON_CLIENT: "client_close",
    DEAL_REASON_MOBILE: "mobile_close",
    DEAL_REASON_WEB: "web_close",
    DEAL_REASON_SIGNAL: "signal_close",
    DEAL_REASON_SL: "sl_hit",
    DEAL_REASON_TP: "tp_hit",
    DEAL_REASON_STOP_OUT: "stop_out",
    DEAL_REASON_RISK_OUT: "risk_out",
}

WATCHDOG_PREFIX = "exit_watchdog:"
MANAGED_COMMENT_LIMIT = 80


def watchdog_shortcode(comment: str) -> str:
    """Canonical watchdog short-code: ``exit_watchdog:hesitation_18c_no_breakeven`` → ``watchdog:hesitation_18c``.

    DQAF-064 §1 / FIX-20260612-003: first two underscore-segments after the
    prefix.  The bridge worker previously extracted three segments — converged
    here so every producer emits the same code.
    """
    _reason = comment.split(":", 1)[1] if ":" in comment else comment
    _parts = _reason.split("_", 2)
    _short = "_".join(_parts[:2]) if len(_parts) >= 2 else _reason[:30]
    return f"watchdog:{_short}"


def trail_advances_of(contribution: Any) -> int:
    """Extract the trail_advances int from a ``trail_contribution`` dict.

    Defensive: ``None``/non-dict/absent ⇒ 0.  Used by settlement enqueue call
    sites so the trail_advances extraction lives in one place.
    """
    if isinstance(contribution, dict):
        return _as_positive_int(contribution.get("trail_advances", 0))
    return 0


def trail_active_from_sources(
    pm_trail_advances: Any,
    trail_contribution: Any,
) -> bool:
    """Unified trail-active predicate (DQAF-20260806-001 contract).

    ORs the two independent trail-telemetry sources:

      * ``position_manager`` trail_advances (position may still be tracked),
      * the ``trail_contribution`` dict captured at detection time (ghost
        path — the position may already be gone from position_manager).

    Either source proving a trail advance ⇒ the SL exit is ``sl_hit_trailed``.
    Pre-P6 each producer consulted at most one source, so the same deal could
    be labeled ``sl_hit_first`` on one path and ``sl_hit_trailed`` on another.
    """
    if _as_positive_int(pm_trail_advances) > 0:
        return True
    if isinstance(trail_contribution, dict):
        if _as_positive_int(trail_contribution.get("trail_advances", 0)) > 0:
            return True
    return False


def resolve_close_reason_str(deal_reason: int | None) -> str:
    """Canonical MT5 deal-reason string (the ``detail.reason``/``exit_reason`` field).

    Converges the three pre-P6 formats (``unknown_{n}``,
    ``mt5_deal_reason_{n}`` + ``unknown_close``, and the ``{4,5}-only`` map).
    ``None`` ⇒ the honest ``unknown_close`` — never fabricate a broker
    attribution for an unknown reason.
    """
    if deal_reason is not None:
        if deal_reason in DEAL_REASON_MAP:
            return DEAL_REASON_MAP[deal_reason]
        return f"unknown_{deal_reason}"
    return "unknown_close"


def resolve_close_label(
    deal_reason: int | None,
    deal_comment: str,
    trail_active: bool = False,
) -> str:
    """Canonical close label from MT5 deal attribution — the SSOT mouth.

    Priority order (P6 / DQAF-20260821-001 mapping matrix):

      P0  ``exit_watchdog:`` comment        → ``watchdog:<short>``
      P1  deal_reason 4 (SL) + trail_active → ``sl_hit_trailed``
      P2  deal_reason 4 (SL) no trail       → ``sl_hit_first``
      P3  deal_reason 5 (TP)                → ``tp_hit_first``
      P4  non-empty comment                 → ``managed:<comment[:80]>``
      P5  broker reason 0-3, 6, 7           → ``broker:<canonical>``
      P6  None / unknown reason             → ``unknown_close`` (honest)

    Totally-defined over deal-informed inputs; ``None`` never fabricates.
    """
    _comment = str(deal_comment or "")
    if _comment.startswith(WATCHDOG_PREFIX):
        return watchdog_shortcode(_comment)
    if deal_reason == DEAL_REASON_SL:
        return "sl_hit_trailed" if trail_active else "sl_hit_first"
    if deal_reason == DEAL_REASON_TP:
        return "tp_hit_first"
    if _comment:
        return f"managed:{_comment[:MANAGED_COMMENT_LIMIT]}"
    if deal_reason is not None and deal_reason in DEAL_REASON_MAP:
        return f"broker:{DEAL_REASON_MAP[deal_reason]}"
    if deal_reason is not None:
        return f"broker:unattributed_{deal_reason}"
    return "unknown_close"


def _as_positive_int(value: Any) -> int:
    try:
        _v = int(float(value or 0))
    except (TypeError, ValueError):
        return 0
    return _v if _v > 0 else 0
