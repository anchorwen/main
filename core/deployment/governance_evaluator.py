"""SSOT governance evaluation — the single automated lifecycle writer.

FIX-20260801-010 / DQAF-20260801-010: Extracted from
``scheduler_service.governance_eval``'s Auditor→Executor pipeline so that BOTH
the containerized deployment path (scheduler_service.py / apps.engine) and the
bare-metal launcher path (scripts/live_launcher.py) execute the IDENTICAL
``brain_performance.json``-based governance evaluation.

Background: ``BTC_Swing_V4`` was oscillating live↔probation because the runtime
path (scripts/live_intent_loop.py) evaluated governance on BrainPnLStore
(brain_pnl_ledger.json, last-20 window) via the DEPRECATED
``apply_promotion_decisions()`` direct write, while the container path used
brain_performance.json (window-100) + GovernanceRuleEngine.execute_transitions.
Iron Law #14 (No Siloed Reconciliation) mandates a single reconciliation path;
this module is that path.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def resolve_brains_dir(base_dir: str | Path) -> Path:
    """Infer the brains config dir from the data dir name.

    Mirrors the SSOT pattern in ``GovernanceService.load()``:
    ``data_btc`` → ``configs/brains_btc``, ``data`` → ``configs/brains``.
    Falls back to ``configs/brains`` for unknown data dir names.
    """
    base = Path(base_dir)
    if base.name == "data_btc":
        return Path("configs/brains_btc")
    if base.name == "data":
        return Path("configs/brains")
    return Path("configs/brains")


def load_observation_holds(brains_dir: str | Path) -> dict[str, datetime]:
    """Read ``observation_hold_until`` from brain config ``governance`` blocks.

    Returns a map ``brain_id → naive-UTC expiry datetime``.  Configs without
    the field, unparseable dates, and missing dirs are skipped — observation
    holds are a non-critical policy guard and must never block governance
    evaluation (fail-open).

    Field contract (FIX-20260801-012): the IC's strategic observation window
    has explicit priority over automated demotion.  ``GovernanceRuleEngine``
    refuses any demotion for a brain whose hold is still active.
    """
    holds: dict[str, datetime] = {}
    _dir = Path(brains_dir)
    if not _dir.is_dir():
        return holds
    for _cfg_path in sorted(_dir.glob("*.json")):
        if "normalization" in _cfg_path.name.lower():
            continue
        try:
            _raw = json.loads(_cfg_path.read_text(encoding="utf-8"))
            _bid = _raw.get("brain_id", "")
            _hold_raw = (_raw.get("governance") or {}).get("observation_hold_until")
            if not _bid or not _hold_raw:
                continue
            _dt = datetime.fromisoformat(_hold_raw.replace("Z", "+00:00"))
            if _dt.tzinfo is not None:
                _dt = _dt.astimezone(UTC).replace(tzinfo=None)
            holds[_bid] = _dt
        except (ValueError, TypeError, KeyError, OSError, json.JSONDecodeError):
            continue
    return holds


def evaluate_governance_state(
    governance_service: Any,
    base_dir: str | Path,
    *,
    manual_mode: bool = False,
    rule_engine: Any = None,
    brains_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Run the brain_performance SSOT governance evaluation once.

    Reads ``brain_performance.json`` (window-100 live execution outcomes),
    injects metrics into governance state, purges backtest-contaminated
    metrics, then evaluates promotion decisions and applies transitions via
    GovernanceRuleEngine.execute_transitions (the sole writer).

    Args:
        governance_service: Loaded GovernanceService (owning the state).
        base_dir: Data directory holding brain_performance.json and
            governance_state.json.
        manual_mode: If True, log decisions without executing transitions.
        rule_engine: GovernanceRuleEngine to apply transitions with.  When
            None, a fresh ``with_default_rules()`` engine is built for the
            given governance_service.
        brains_dir: Brains config directory (L1 SSOT) holding the brain JSON
            files with ``governance.observation_hold_until``.  Defaults to
            ``resolve_brains_dir(base_dir)``.

    Returns:
        A summary dict: ``brains_with_live_data``, ``decisions``, ``changes``,
        ``manual_mode``, ``updated_at``.

    Raises:
        The evaluation raises on failure; callers wrap with BLE001:FOG as
        appropriate (do not swallow the diagnostics here).
    """
    from core.brains.services.brain_promotion import BrainPromotionEvaluator
    from core.deployment.brain_alert import emit_brain_alert
    from core.governance.governance_rule_engine import GovernanceRuleEngine

    base_dir = Path(base_dir)
    _bp_path = base_dir / "brain_performance.json"

    perf: dict[str, dict] = {}
    if _bp_path.exists():
        _bp_data = json.loads(_bp_path.read_text(encoding="utf-8"))
        _bp_records = _bp_data.get("records", {})
        for _bid, _records in _bp_records.items():
            if not isinstance(_records, list):
                continue
            _wins = sum(
                1 for r in _records if isinstance(r, dict) and r.get("execution_outcome") == "win"
            )
            _losses = sum(
                1 for r in _records if isinstance(r, dict) and r.get("execution_outcome") == "loss"
            )
            _total = _wins + _losses
            if _total == 0:
                continue
            _wr = _wins / _total
            perf[_bid] = {
                "win_rate": round(_wr, 4),
                "profit_factor": (
                    round(_wins / max(_losses, 1), 2)
                    if _losses > 0
                    else round(_wins, 2)
                    if _wins > 0
                    else 0.0
                ),
                "signal_count": _total,
                "consecutive_losses": 0,  # not in brain_performance
                "recent_win_rate": round(_wr, 4),
            }
            # Auto-register brain if not in governance yet
            _existing = governance_service.get_brain_state(_bid)
            if _existing is None:
                governance_service.register_brain(_bid, "candidate")

            # Inject LIVE execution metrics into governance
            governance_service.set_performance_metrics(
                _bid,
                {
                    "win_rate": round(_wr, 4),
                    "total_trades": _total,
                    "wins": _wins,
                    "losses": _losses,
                    "source": "brain_performance",
                    "updated_at": datetime.now(UTC).isoformat(),
                },
            )
    else:
        logger.warning("[GOV_LIVE] brain_performance.json not found at %s", _bp_path)

    # ── Purge backtest-contaminated metrics for brains NOT in brain_performance.
    #    DQAF-061: daily_ops-injected metrics carry _data_source
    #    ("live_journal" or "pnl_store") — these are trusted event-stream or
    #    journal-derived metrics, NOT backtest artifacts.  Only purge metrics
    #    that lack BOTH source markers.
    _all_states = governance_service.get_all_states()
    for _bid, _state in _all_states.items():
        _pm = _state.get("performance_metrics") or {}
        _src = _pm.get("source", "")
        _alt_src = _pm.get("_data_source", "")
        # brain_performance marker → trusted
        if _src == "brain_performance":
            continue
        # DQAF-061: daily_ops-injected metrics → trusted
        if _alt_src in ("live_journal", "pnl_store"):
            continue
        if _pm.get("total_trades", 0) > 0:
            # Replace stale backtest metrics with empty sentinel
            governance_service.set_performance_metrics(
                _bid,
                {
                    "win_rate": 0.0,
                    "total_trades": 0,
                    "source": "cleared_backtest",
                    "updated_at": datetime.now(UTC).isoformat(),
                },
            )
            logger.info(
                "[GOV_CLEAN] Purged backtest metrics for brain=%s " "(had %d backtest trades)",
                _bid,
                _pm.get("total_trades", 0),
            )

    logger.info(
        "[GOV_LIVE] Injected brain_performance metrics: " "%d brains with live trade data",
        len(perf),
    )

    # ── Auditor→Executor: evaluate decisions, apply via rule engine ──
    brain_states = governance_service.get_all_states()
    evaluator = BrainPromotionEvaluator()
    decisions = evaluator.evaluate_all(brain_states, perf)

    if rule_engine is None:
        rule_engine = GovernanceRuleEngine.with_default_rules(governance_service)

    # ── FIX-20260801-012: Observation hold (grace period) attachment ──
    # Read observation_hold_until from brain config governance blocks (L1
    # human SSOT) and attach to the engine.  During an active hold the
    # Executor (execute_transitions) blocks any demotion (e.g. throttle
    # live→probation), giving the IC's strategic observation window explicit
    # priority over automated demotion.  Applies to BOTH deployment paths
    # (container: caller's engine; launcher: fresh engine built above).
    _brains_dir = Path(brains_dir) if brains_dir is not None else resolve_brains_dir(base_dir)
    _observation_holds = load_observation_holds(_brains_dir)
    rule_engine.set_observation_holds(_observation_holds)
    if _observation_holds:
        logger.info(
            "[GOV_HOLD] observation holds attached for brain(s): %s",
            ", ".join(sorted(_observation_holds)),
        )

    if manual_mode:
        for d in decisions:
            if d.action != "hold":
                logger.warning(
                    "[GOV_MANUAL] Would %s brain=%s (%s→%s) "
                    "reasons=%s — NOT EXECUTED (manual mode)",
                    d.action,
                    d.brain_id,
                    d.current_status,
                    d.target_status,
                    d.reasons,
                )
                # Emit alert so humans see pending decisions
                emit_brain_alert(
                    d.brain_id,
                    "governance_manual_blocked",
                    {
                        "action": d.action,
                        "current_status": d.current_status,
                        "target_status": d.target_status,
                        "reasons": d.reasons,
                        "metrics": d.metrics_snapshot,
                    },
                )
        changes: list[str] = []
    else:
        changes = rule_engine.execute_transitions(decisions)

    # ── Persist governance state to disk AFTER transitions ──
    # DQAF-20260804-001 (L3): save must happen AFTER execute_transitions so
    # throttle/promote decisions actually persist.  The previous pre-transition
    # save persisted only the perf injection; every in-memory transition (e.g.
    # BTC_Swing_V4 live→probation throttle) was discarded on the next 60s
    # reload → governance_state.json never converged.  Single save covers both
    # deployment paths (container scheduler_service + bare-metal launcher).
    _gov_save_path = base_dir / "governance_state.json"
    governance_service.save(str(_gov_save_path), lock_timeout=1.0)
    logger.info(
        "[GOV_LIVE] Governance state saved to %s (transitions=%s)",
        _gov_save_path,
        ",".join(changes) if changes else "none",
    )

    return {
        "base_dir": str(base_dir),
        "brains_with_live_data": len(perf),
        "decisions": [
            {
                "brain_id": d.brain_id,
                "action": d.action,
                "current_status": d.current_status,
                "target_status": d.target_status,
                "approved": d.approved,
                "reasons": d.reasons,
            }
            for d in decisions
        ],
        "changes": changes,
        "manual_mode": manual_mode,
        "updated_at": datetime.now(UTC).isoformat(),
    }
