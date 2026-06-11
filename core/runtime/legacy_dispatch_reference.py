"""LEGACY Phase 9-12 dispatch reference — extracted from live_cycle.py.

FIX-20260611-018: Dead code retained as rollback reference ONLY.
These ~567 lines were the legacy contract-group consensus + shadow verification
+ risk evaluation + dispatch pipeline (Phase 9-12 in the old routing fork).

**Why removed**: FIX-20260610-010 inserted a Phase 10 gate at L5688-L5698 that
sets ``direction = "neutral"`` when ``multi_strategy_enabled=True`` (default).
This causes an early return at L5721-L5750, making L5752-L6317 unreachable.
With default config, dispatch is handled exclusively by the ExecutionQueue
path (Phase 7-8e, L3543-L5319).

**When to reference**: Only needed for rollback to ``multi_strategy_enabled=False``
or when auditing the historical dispatch pathway.

**Original location**: live_cycle.py L5752-L6317 (inside the
``if config.multi_brain and config.multi_strategy_enabled:`` block).

**Architecture note**: The active dispatch pipeline (ExecutionQueue flush at
L5078) already covers all dispatch scenarios.  This legacy code was retained
"just in case" but has been dead since 2026-06-10.
"""

# ══════════════════════════════════════════════════════════════════════════════
# LEGACY DISPATCH REFERENCE — DO NOT IMPORT IN PRODUCTION CODE
# ══════════════════════════════════════════════════════════════════════════════
#
# The code below was the original ``execute_live_cycle()`` Phase 9-12 tail.
# It is preserved verbatim for rollback reference and historical audit.
# It is NOT importable as a function — the code references local variables
# from the enclosing ``execute_live_cycle()`` scope and cannot run standalone.
#
# For the ACTIVE dispatch code, see:
#   - core/runtime/live_cycle.py L4963-5319 (ExecutionQueue flush + dispatch)
#   - core/runtime/position_registration.py (Strangler Fig #10)
#   - core/runtime/trail_dispatch.py (Strangler Fig #11)
#   - core/runtime/position_close_adapter.py (Strangler Fig #11-13)
#
# ══════════════════════════════════════════════════════════════════════════════

# NOTE: The following is NOT executable Python — it's a reference transcript.
# It references local variables (side, direction, confidence, mid, bid, ask,
# proposal, raw_proposals, config, state, brains, broker, mt5_worker, etc.)
# from the enclosing execute_live_cycle() function scope.
#
# BEGIN REFERENCE TRANSCRIPT:

#     side = direction  # "long" or "short"
#
#     # ── Stage shadow verification for next cycle's settlement ──
#     if direction != "neutral" and mid_price is not None and mid_price > 0:
#         all_supporting_v: list[str] = []
#         all_opposing_v: list[str] = []
#         if config.multi_brain and consensus_extra:
#             all_supporting_v = consensus_extra.get("supporting_brains", [])
#             all_opposing_v = consensus_extra.get("opposing_brains", [])
#         state.shadow_verification_pending = {
#             "direction": direction,
#             "entry_price": mid_price,
#             "consensus_score": confidence,
#             "supporting_brains": all_supporting_v,
#             "opposing_brains": all_opposing_v,
#         }
#
#     # ── Risk evaluation ──
#     if control_snapshot is None:
#         control_snapshot = _build_minimal_control_snapshot()
#     risk_context = (
#         _build_risk_context_from_broker(broker, config.symbol)
#         if broker is not None
#         else _build_risk_context(mt5_worker, config.symbol)
#     )
#     risk_verdict = _evaluate_risk(
#         risk_service, control_snapshot, risk_context,
#         config.symbol, direction, confidence,
#     )
#     ...
#     # (full dispatch pipeline: SL/TP compute → dispatch_live_open_order →
#     #  position registration → journal enrichment → known_open_tickets tracking)
#
# END REFERENCE TRANSCRIPT.
#
# For the complete original code, see git history:
#   git show HEAD~:core/runtime/live_cycle.py | sed -n '5752,6317p'
