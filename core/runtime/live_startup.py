"""Live trading startup utilities — brain loading, governance, regime warm-start.

Extracted from scripts/live_intent_loop.py per the Strangler Fig pattern (#9).
Self-contained init/loader functions that the CLI shell delegates to.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def decide_side_from_anchor(price: float, anchor: float, threshold: float) -> str | None:
    """Determine trade direction from price vs anchor +- threshold."""
    if price > anchor + threshold:
        return "long"
    if price < anchor - threshold:
        return "short"
    return None


def _resolve_consensus_side(consensus: dict[str, Any], min_confidence: float) -> str | None:
    """Convert ParliamentService consensus dict to trade side."""
    bias = consensus.get("aggregated_bias", "neutral")
    score = consensus.get("consensus_score", 0.0)
    if score < min_confidence or bias == "neutral":
        return None
    if bias in ("long", "short"):
        return bias
    return None


def bootstrap_regime_detector(
    mt5_worker: Any, symbol: str, detector: Any, *, bootstrap_bars: int = 200
) -> bool:
    """Warm-start regime detector from MT5 historical ATR data."""
    if detector.is_warmed_up and detector.atr_mean > 0.1:
        return True

    import numpy as np

    from core.runtime.fault_handler import FaultLevel, FaultTolerantContext

    rates = None
    with FaultTolerantContext(
        level=FaultLevel.CRASH, component="MT5_IPC:copy_rates_from_pos:warm_start_regime"
    ):
        rates = mt5_worker.copy_rates_from_pos(symbol, 5, 0, bootstrap_bars)

    try:
        if rates is None or len(rates) < 30:
            return False

        h = np.array([r["high"] for r in rates], dtype=np.float64)
        low = np.array([r["low"] for r in rates], dtype=np.float64)
        c = np.array([r["close"] for r in rates], dtype=np.float64)
        n = len(c)

        atr_period = 14
        atr_values = []
        for i in range(atr_period, n):
            cur_h = h[i - atr_period + 1 : i + 1]
            cur_l = low[i - atr_period + 1 : i + 1]
            prev_c = c[i - atr_period : i]
            tr = np.maximum(
                cur_h - cur_l,
                np.maximum(np.abs(cur_h - prev_c), np.abs(cur_l - prev_c)),
            )
            atr_val = float(np.mean(tr))
            if atr_val > 0.01:
                atr_values.append(atr_val)
                detector.update(atr_val)

        if atr_values and detector.count > 0:
            sample_mean = float(np.mean(atr_values))
            sample_var = float(np.var(atr_values))
            if sample_mean > 0.1 and sample_var > 0.01:
                detector._mean = sample_mean
                detector._var = sample_var - detector._eps

        return detector.is_warmed_up
    except Exception:  # BLE001:REVIEWED
        return False


def load_normalization_config(path: str, *, project_root: Path | None = None) -> dict[str, Any]:
    """Load normalization config from JSON, resolving relative paths."""
    p = Path(path)
    if not p.is_absolute():
        if project_root is not None:
            p = project_root / p
        if not p.exists():
            p = Path.cwd() / Path(path)
    if not p.exists():
        raise FileNotFoundError(f"normalization config not found: {p}")
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def load_brain_entry(path: str, *, project_root: Path | None = None) -> dict[str, Any]:
    """Load brain registry entry from JSON, resolving relative paths."""
    p = Path(path)
    if not p.is_absolute():
        if project_root is not None:
            p = project_root / p
        if not p.exists():
            p = Path.cwd() / Path(path)
    if not p.exists():
        raise FileNotFoundError(f"brain entry not found: {p}")
    with open(p, encoding="utf-8") as fh:
        return json.load(fh)


def load_brain_entries_from_dir(
    brains_dir: str, *, project_root: Path | None = None
) -> list[dict[str, Any]]:
    """Load all brain registry entry JSON files from a directory."""
    p = Path(brains_dir)
    if not p.is_absolute():
        if project_root is not None:
            p = project_root / p
        if not p.is_dir():
            p = Path.cwd() / Path(brains_dir)
    if not p.is_dir():
        raise FileNotFoundError(f"brains directory not found: {p}")
    entries: list[dict[str, Any]] = []
    for f in sorted(p.glob("*.json")):
        if f.name.endswith(".normalization.json"):
            continue
        try:
            entry = json.loads(f.read_text(encoding="utf-8"))
            if entry.get("schema_version") == "brain_registry_entry.v1":
                entry["_source_path"] = str(f.resolve())
                entries.append(entry)
        except (json.JSONDecodeError, OSError):
            pass
    if not entries:
        raise FileNotFoundError(f"no brain_registry_entry.v1 files found in {p}")
    return entries


def apply_governance_filter(
    entries: list[dict[str, Any]], base_dir: str
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Filter brain entries by governance status and apply weight penalties."""
    report: dict[str, Any] = {
        "governance_loaded": False,
        "total_entries": len(entries),
        "removed": [],
        "penalized": [],
        "kept": [],
    }
    gov_path = Path(base_dir) / "governance_state.json"
    if not gov_path.exists():
        report["reason"] = "no_governance_state"
        return entries, report

    try:
        from core.governance.governance_service import GovernanceService

        gov = GovernanceService.load(gov_path)
        report["governance_loaded"] = True
    except Exception as exc:  # BLE001:REVIEWED
        report["reason"] = f"governance_load_failed: {exc}"
        return entries, report

    filtered: list[dict[str, Any]] = []
    for entry in entries:
        brain_id = entry.get("brain_id", "unknown")
        state = gov.get_brain_state(brain_id)

        if state is None:
            filtered.append(entry)
            report["kept"].append(brain_id)
            continue

        status = state.get("status", "candidate")
        if status in ("retired", "frozen"):
            report["removed"].append({"brain_id": brain_id, "status": status})
            print(
                json.dumps(
                    {
                        "event": "brain_governance_skip",
                        "brain_id": brain_id,
                        "status": status,
                        "reason": f"brain is {status}",
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )
            continue

        if status == "probation":
            entry = dict(entry)
            original_weight = entry.get("vote_weight", 1.0)
            entry["vote_weight"] = round(original_weight * 0.5, 4)
            entry["_governance_status"] = "probation"
            report["penalized"].append(
                {
                    "brain_id": brain_id,
                    "original_weight": original_weight,
                    "new_weight": entry["vote_weight"],
                }
            )
            print(
                json.dumps(
                    {
                        "event": "brain_governance_penalty",
                        "brain_id": brain_id,
                        "status": status,
                        "vote_weight": entry["vote_weight"],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

        # ── FIX-20260609-011: candidate brain penalty ────────────────────
        # candidate brains have NEVER demonstrated profitability — they are
        # newly registered and unproven.  Previously candidate received full
        # vote_weight while probation (formerly live, now degraded) was
        # penalised 0.5× — a logical inversion.  candidate now receives the
        # same 0.5× penalty as probation so that unproven brains don't
        # dominate the ensemble vote.
        if status == "candidate":
            entry = dict(entry)
            original_weight = entry.get("vote_weight", 1.0)
            entry["vote_weight"] = round(original_weight * 0.5, 4)
            entry["_governance_status"] = "candidate"
            report["penalized"].append(
                {
                    "brain_id": brain_id,
                    "original_weight": original_weight,
                    "new_weight": entry["vote_weight"],
                }
            )
            print(
                json.dumps(
                    {
                        "event": "brain_governance_penalty",
                        "brain_id": brain_id,
                        "status": status,
                        "vote_weight": entry["vote_weight"],
                    },
                    ensure_ascii=False,
                ),
                flush=True,
            )

        report["kept"].append(brain_id)
        filtered.append(entry)

    return filtered, report


def check_single_brain_governance(brain_id: str, base_dir: str) -> dict[str, Any]:
    """Check whether a single brain should be blocked or warned by governance."""
    gov_path = Path(base_dir) / "governance_state.json"
    if not gov_path.exists():
        return {"blocked": False, "warning": False, "reason": "no_governance_state"}

    try:
        from core.governance.governance_service import GovernanceService

        gov = GovernanceService.load(gov_path)
    except Exception as exc:  # BLE001:REVIEWED
        return {"blocked": False, "warning": False, "reason": f"governance_load_failed: {exc}"}

    state = gov.get_brain_state(brain_id)
    if state is None:
        return {"blocked": False, "warning": False, "reason": "not_registered"}

    status = state.get("status", "candidate")
    if status in ("retired", "frozen"):
        return {
            "blocked": True,
            "warning": False,
            "status": status,
            "reason": f"brain is {status}",
        }
    if status == "probation":
        return {
            "blocked": False,
            "warning": True,
            "status": status,
            "reason": "brain is on probation — run with reduced weight",
        }

    return {"blocked": False, "warning": False, "status": status}


def inject_performance_metrics(pnl_store: Any, base_dir: str) -> None:
    """Inject per-brain performance metrics into governance state every cycle.

    FIX-20260617-001: Replaced BrainPnLStore (backtest/shadow PnL) with
    brain_performance.json (live execution outcomes from MT5 fills).
    """
    # ── FIX-20260617-001: Skip injection — scheduler_service.py handles this ──
    # The governance performance_metrics are now injected from
    # brain_performance.json by the governance_scheduler (every 60s).
    # This live-cycle injection is a duplicate path; keep disabled
    # until the scheduler path is fully validated in production.
    _GOVERNANCE_SKIP_INJECTION = True
    if _GOVERNANCE_SKIP_INJECTION:
        return

    import json as _json
    from pathlib import Path as _P

    _gov_path = _P(base_dir) / "governance_state.json"
    _bp_path = _P(base_dir) / "brain_performance.json"
    if not _gov_path.exists() or not _bp_path.exists():
        return
    try:
        from core.governance.governance_service import GovernanceService

        gov = GovernanceService.load(str(_gov_path))
        _bp_data = _json.loads(_bp_path.read_text(encoding="utf-8"))
        _bp_records = _bp_data.get("records", {})
        for _bid, _records in _bp_records.items():
            if not isinstance(_records, list):
                continue
            _wins = sum(
                1 for r in _records
                if isinstance(r, dict) and r.get("execution_outcome") == "win"
            )
            _losses = sum(
                1 for r in _records
                if isinstance(r, dict) and r.get("execution_outcome") == "loss"
            )
            _total = _wins + _losses
            if _total == 0:
                continue
            _wr = _wins / _total
            gov.set_performance_metrics(
                _bid,
                {
                    "win_rate": round(_wr, 4),
                    "total_trades": _total,
                    "wins": _wins,
                    "losses": _losses,
                    "source": "brain_performance",
                },
            )
        gov.save(str(_gov_path), lock_timeout=1.0)
    except Exception:  # BLE001:REVIEWED
        import logging as _inj_log

        _inj_log.getLogger(__name__).warning(
            "performance_metrics_injection_failed — governance metrics will be stale"
        )


def init_risk_service() -> Any:
    """Create RiskEvaluationService with standard live trading policies."""
    from core.risk.risk_evaluation_service import RiskEvaluationService
    from core.risk.risk_policies import (
        ConcentrationPolicy,
        DrawdownPolicy,
        ExposurePolicy,
        ModePolicy,
        PositionLimitPolicy,
    )

    svc = RiskEvaluationService()
    svc.add_policy(DrawdownPolicy(max_drawdown_pct=5.0))
    svc.add_policy(PositionLimitPolicy(max_open_positions=10))
    svc.add_policy(ConcentrationPolicy(max_per_symbol=3))
    svc.add_policy(ExposurePolicy(max_notional=1_000_000.0))
    svc.add_policy(ModePolicy())
    return svc
