"""Gate reachability analyzer — static analysis of gate thresholds vs capabilities.

Layer 4 of the 4-layer config defense system (FIX-20260712-003 Phase 2).
Performs offline static analysis of all strategy gates to detect:
1. **Unreachable gates**: threshold > max possible value (dead gate)
2. **Cold-start deadlock risk**: p_win floor < dynamic breakeven at startup
3. **Confidence ceiling conflicts**: brain's max confidence can't reach gate thresholds

Usage:
    from core.execution.gate_reachability import analyze_gate_reachability
    report = analyze_gate_reachability("configs/live_btc.yaml", "configs/brains_btc")
    for r in report:
        if not r.reachable:
            print(f"UNREACHABLE: {r.gate_name} — {r.detail}")

CLI:
    python -m core.execution.gate_reachability --live-yaml configs/live_btc.yaml
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class GateReachabilityReport:
    """Single gate reachability assessment."""

    gate_name: str
    category: str  # "confidence", "p_win", "rr_ratio", "cold_start"
    threshold: float
    max_possible_value: float
    min_possible_value: float
    reachable: bool
    cold_start_deadlock_risk: bool = False
    dead_gate_risk: bool = False  # gate never fires in practice
    detail: str = ""
    affected_brains: list[str] = field(default_factory=list)


# ── Confidence estimation by calibration method ──


def _estimate_max_confidence(brain_config: dict) -> float:
    """Estimate the maximum possible confidence a brain can produce.

    Based on the calibration method declared in training_params or inferred
    from the model type.
    """
    tp = brain_config.get("training_params", {})
    cal_method = tp.get("calibration_method", tp.get("confidence_method", ""))

    if not cal_method:
        # Infer from model type
        bt = brain_config.get("brain_type", "")
        if "xgboost" in bt:
            cal_method = "quantile_gaussian"  # default for XGBoost brains
        elif "lightgbm" in bt:
            cal_method = "tanh"
        else:
            cal_method = "tanh"

    if "quantile_gaussian" in cal_method:
        # peak_conf * |score|/p95, max when |score| >= p95
        peak = float(tp.get("peak_conf", 1.0))
        return min(peak, 1.0)

    if "tanh" in cal_method:
        # tanh asymptote at ~0.76
        return 0.76

    if "isotonic" in cal_method:
        # Isotonic regression maps to [0,1] empirically
        return 0.99

    if "platt" in cal_method:
        # Platt scaling → sigmoid, ~0.99 asymptote
        return 0.99

    # Unknown method — conservative estimate
    return 0.80


def _get_cold_start_p_win(governance_path: str | None = None) -> float:
    """Estimate p_win at cold start (no active brains, no history).

    From resolve_p_win() fallback chain:
    1. MetaFilter p_win (requires active brains) → N/A at cold start
    2. Governance rolling WR (requires trade history) → N/A at cold start
    3. Cold explore fallback → 0.50 (ultimate floor)
    """
    # Default cold-explore p_win from pwin_chain.py resolve_p_win()
    return 0.50


def analyze_gate_reachability(
    live_yaml_path: str,
    brains_dir: str,
    *,
    governance_state_path: str | None = None,
) -> list[GateReachabilityReport]:
    """Analyze all gates for reachability given brain capability ceilings.

    Args:
        live_yaml_path: Path to live.yaml or live_btc.yaml
        brains_dir: Path to brain config directory (configs/brains_btc, etc.)
        governance_state_path: Optional path to governance_state.json for
            cold-start analysis.

    Returns:
        List of GateReachabilityReport, one per analyzed gate.
    """
    import yaml as _yaml

    reports: list[GateReachabilityReport] = []

    # ── Load live.yaml ──
    try:
        with open(live_yaml_path, encoding="utf-8") as f:
            live_cfg = _yaml.safe_load(f) or {}
    except (OSError, ValueError, RuntimeError):
        return reports

    # ── Load brain configs ──
    brain_configs: dict[str, dict] = {}
    brains_path = Path(brains_dir)
    if brains_path.is_dir():
        for cfg_file in sorted(brains_path.glob("*.json")):
            if ".normalization." in cfg_file.name:
                continue
            try:
                cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
                if cfg.get("schema_version") == "brain_registry_entry.v1":
                    bid = cfg.get("brain_id", "")
                    if bid:
                        brain_configs[bid] = cfg
            except (json.JSONDecodeError, OSError, KeyError):
                pass

    if not brain_configs:
        return reports

    # ── Extract global thresholds ──
    strategy_cfg = live_cfg.get("strategy", {}) or {}
    reentry_cfg = live_cfg.get("reentry", {}) or {}
    session_cfg = live_cfg.get("session", {}) or {}

    global_confidence_threshold = float(strategy_cfg.get("confidence_threshold", 0.35))
    reentry_confidence_penalty = float(strategy_cfg.get("reentry_confidence_penalty", 0.15))
    sl_confidence_penalty = float(reentry_cfg.get("sl_confidence_penalty", 0.15))
    bleed_confidence_penalty = float(reentry_cfg.get("bleed_confidence_penalty", 0.15))
    min_rr_ratio_global = float(strategy_cfg.get("min_rr_ratio", 0.0))
    confidence_drop_threshold = float(strategy_cfg.get("confidence_drop_threshold", 0.15))

    # ── Analyze per strategy_line ──
    strategy_lines = live_cfg.get("strategy_lines", {}) or {}
    if isinstance(strategy_lines, list):
        # Legacy list format — convert to dict
        strategy_lines = {
            sl.get("name", f"unknown_{i}"): sl
            for i, sl in enumerate(strategy_lines)
            if isinstance(sl, dict)
        }
    analyzed_strategies: set[str] = set()

    for sname, sl in strategy_lines.items():
        if not isinstance(sl, dict):
            continue
        analyzed_strategies.add(sname)

        # Per-strategy overrides
        sl_conf_threshold = float(sl.get("confidence_threshold", global_confidence_threshold))
        # min_rr_ratio may be at sl.min_rr_ratio (nested) or top-level
        sl_min_rr_raw = sl.get("min_rr_ratio")
        if sl_min_rr_raw is None:
            sl_sub = sl.get("sl")
            if isinstance(sl_sub, dict):
                sl_min_rr_raw = sl_sub.get("min_rr_ratio")
        sl_min_rr = float(sl_min_rr_raw) if sl_min_rr_raw is not None else min_rr_ratio_global
        sl_min_p_win = float(sl.get("min_p_win", 0.45))
        sl_breakeven = 1.0 / (1.0 + sl_min_rr) if sl_min_rr > 0 else 0.0

        # Brains for this strategy line — matched by brain_type
        sl_brain_types = set(sl.get("brain_types", []))
        sl_brains = {
            bid: bc for bid, bc in brain_configs.items() if bc.get("brain_type") in sl_brain_types
        }
        if not sl_brains:
            # Fallback: if no brain_types filter, use all non-retired brains
            sl_brains = {
                bid: bc
                for bid, bc in brain_configs.items()
                if bc.get("status") not in ("retired", "frozen", "archived")
            }
        if not sl_brains:
            continue

        # ── Check 1: Base confidence gate ──
        max_conf = max(_estimate_max_confidence(bc) for bc in sl_brains.values())
        min_conf = min(_estimate_max_confidence(bc) for bc in sl_brains.values())

        reports.append(
            GateReachabilityReport(
                gate_name=f"{sname}.confidence_threshold",
                category="confidence",
                threshold=sl_conf_threshold,
                max_possible_value=max_conf,
                min_possible_value=min_conf,
                reachable=max_conf >= sl_conf_threshold,
                detail=(
                    f"Max brain confidence={max_conf:.2f} vs threshold={sl_conf_threshold:.2f}"
                    if max_conf < sl_conf_threshold
                    else f"All brains can reach threshold (max={max_conf:.2f})"
                ),
                affected_brains=list(sl_brains.keys()),
            )
        )

        # ── Check 2: SL recovery confidence gate ──
        sl_recovery_threshold = sl_conf_threshold + sl_confidence_penalty
        reports.append(
            GateReachabilityReport(
                gate_name=f"{sname}.sl_recovery_confidence",
                category="confidence",
                threshold=sl_recovery_threshold,
                max_possible_value=max_conf,
                min_possible_value=min_conf,
                reachable=max_conf >= sl_recovery_threshold,
                detail=(
                    f"SL recovery needs {sl_recovery_threshold:.2f}, max confidence={max_conf:.2f}"
                    if max_conf < sl_recovery_threshold
                    else f"SL recovery reachable (max={max_conf:.2f} >= {sl_recovery_threshold:.2f})"
                ),
                affected_brains=list(sl_brains.keys()),
            )
        )

        # ── Check 3: Bleed reentry confidence gate ──
        bleed_threshold = sl_conf_threshold + bleed_confidence_penalty
        reports.append(
            GateReachabilityReport(
                gate_name=f"{sname}.bleed_reentry_confidence",
                category="confidence",
                threshold=bleed_threshold,
                max_possible_value=max_conf,
                min_possible_value=min_conf,
                reachable=max_conf >= bleed_threshold,
                detail=(
                    f"Bleed reentry needs {bleed_threshold:.2f}, max confidence={max_conf:.2f}"
                    if max_conf < bleed_threshold
                    else f"Bleed reentry reachable (max={max_conf:.2f} >= {bleed_threshold:.2f})"
                ),
                affected_brains=list(sl_brains.keys()),
            )
        )

        # ── Check 4: Confidence decay exit ──
        # Entry at max_conf, exit when conf drops < confidence_drop_threshold below entry
        decay_floor = max_conf - confidence_drop_threshold
        reports.append(
            GateReachabilityReport(
                gate_name=f"{sname}.confidence_decay_exit",
                category="confidence",
                threshold=confidence_drop_threshold,
                max_possible_value=max_conf,
                min_possible_value=0.0,
                reachable=True,  # always reachable — drop is measured from entry
                detail=f"Entry max={max_conf:.2f}, exit at <= {decay_floor:.2f} (drop={confidence_drop_threshold:.2f})",
                affected_brains=list(sl_brains.keys()),
            )
        )

        # ── Check 5: p_win dynamic floor ──
        # breakeven = 1/(1+min_rr_ratio)
        cold_p_win = _get_cold_start_p_win()
        reports.append(
            GateReachabilityReport(
                gate_name=f"{sname}.p_win_dynamic_floor",
                category="p_win",
                threshold=sl_breakeven,
                max_possible_value=1.0,  # after warm-up, p_win can reach 1.0
                min_possible_value=cold_p_win,
                reachable=cold_p_win < sl_breakeven or sl_min_p_win >= sl_breakeven,
                cold_start_deadlock_risk=(
                    cold_p_win < sl_breakeven and sl_min_p_win < sl_breakeven
                ),
                detail=(
                    f"COLD START DEADLOCK RISK: cold p_win={cold_p_win:.3f} < breakeven={sl_breakeven:.3f} "
                    f"(min_rr={sl_min_rr:.2f}), min_p_win={sl_min_p_win:.3f} also below breakeven. "
                    f"Gate unreachable until governance accumulates >= 1 active brain."
                    if cold_p_win < sl_breakeven and sl_min_p_win < sl_breakeven
                    else f"p_win floor reachable (breakeven={sl_breakeven:.3f}, cold_p_win={cold_p_win:.3f})"
                ),
                affected_brains=list(sl_brains.keys()),
            )
        )

        # ── Check 6: min_p_win vs breakeven ──
        reports.append(
            GateReachabilityReport(
                gate_name=f"{sname}.min_p_win_vs_breakeven",
                category="p_win",
                threshold=sl_breakeven,
                max_possible_value=1.0,
                min_possible_value=sl_min_p_win,
                reachable=sl_min_p_win >= sl_breakeven,
                detail=(
                    f"min_p_win={sl_min_p_win:.3f} < breakeven={sl_breakeven:.3f} — "
                    f"signals with p_win between [{sl_min_p_win:.3f}, {sl_breakeven:.3f}] pass p_win gate "
                    f"but fail breakeven floor"
                    if sl_min_p_win < sl_breakeven
                    else f"min_p_win={sl_min_p_win:.3f} >= breakeven={sl_breakeven:.3f}"
                ),
                affected_brains=list(sl_brains.keys()),
            )
        )

        # ── Check 7: RR ratio gate ──
        reports.append(
            GateReachabilityReport(
                gate_name=f"{sname}.rr_below_minimum",
                category="rr_ratio",
                threshold=sl_min_rr,
                max_possible_value=10.0,  # TP/SL can be arbitrarily large
                min_possible_value=0.0,
                reachable=True,  # not a confidence gate — depends on market structure
                detail=f"min_rr_ratio={sl_min_rr:.2f}, max achieved depends on market ATR and SL/TP config",
                affected_brains=list(sl_brains.keys()),
            )
        )

    # ── Check 8: Per-brain confidence ceilings ──
    for bid, bc in brain_configs.items():
        status = bc.get("status", "unknown")
        if status not in ("live", "probation", "candidate"):
            continue

        max_conf = _estimate_max_confidence(bc)
        reports.append(
            GateReachabilityReport(
                gate_name=f"brain.{bid}.confidence_ceiling",
                category="confidence",
                threshold=global_confidence_threshold,
                max_possible_value=max_conf,
                min_possible_value=0.0,
                reachable=max_conf >= global_confidence_threshold,
                detail=(
                    f"Brain {bid} (status={status}) max_conf={max_conf:.2f} vs "
                    f"global threshold={global_confidence_threshold:.2f}"
                ),
                affected_brains=[bid],
            )
        )

    return reports


# ── CLI ──────────────────────────────────────────────────────────────────────


def main() -> None:
    """CLI entry point: analyze gate reachability and print report."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Gate reachability static analyzer — detect dead gates and cold-start deadlocks"
    )
    parser.add_argument(
        "--live-yaml",
        default="configs/live_btc.yaml",
        help="Path to live.yaml (default: configs/live_btc.yaml)",
    )
    parser.add_argument(
        "--brains-dir",
        default=None,
        help="Brain config directory (auto-detected from live-yaml path if not specified)",
    )
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    # Auto-detect brains dir from live yaml path
    brains_dir = args.brains_dir
    if brains_dir is None:
        yp = Path(args.live_yaml)
        if "btc" in yp.stem.lower():
            brains_dir = "configs/brains_btc"
        elif "xau" in yp.stem.lower():
            brains_dir = "configs/brains_xau"
        else:
            brains_dir = "configs/brains"

    reports = analyze_gate_reachability(args.live_yaml, brains_dir)

    if args.json:
        import json as _json

        print(
            _json.dumps(
                [
                    {
                        "gate_name": r.gate_name,
                        "category": r.category,
                        "threshold": r.threshold,
                        "max_possible_value": r.max_possible_value,
                        "reachable": r.reachable,
                        "cold_start_deadlock_risk": r.cold_start_deadlock_risk,
                        "detail": r.detail,
                        "affected_brains": r.affected_brains,
                    }
                    for r in reports
                ],
                indent=2,
            )
        )
        return

    # ── Formatted output ──
    unreachable = [r for r in reports if not r.reachable]
    deadlock = [r for r in reports if r.cold_start_deadlock_risk]
    reachable = [r for r in reports if r.reachable]

    print("=== Gate Reachability Analysis ===")
    print(f"Live YAML:   {args.live_yaml}")
    print(f"Brains dir:  {brains_dir}")
    print(f"Total gates: {len(reports)}")
    print(f"Reachable:   {len(reachable)}")
    print(f"Unreachable: {len(unreachable)}")
    print(f"Deadlock risk:{len(deadlock)}")
    print()

    if deadlock:
        print("!!! COLD START DEADLOCK RISK !!!")
        for r in deadlock:
            print(f"  [{r.gate_name}] {r.detail}")
            if r.affected_brains:
                print(f"    Affected: {', '.join(r.affected_brains)}")
        print()

    if unreachable:
        print("--- Unreachable Gates ---")
        print(f"{'Gate':<45} {'Threshold':>10} {'Max':>8} {'Category':<15}")
        print("-" * 80)
        for r in unreachable:
            print(
                f"{r.gate_name:<45} {r.threshold:>10.3f} {r.max_possible_value:>8.3f} "
                f"{r.category:<15}"
            )
        print()
        for r in unreachable:
            print(f"  [{r.gate_name}] {r.detail}")
    else:
        print("All gates reachable — no blocking issues detected.")

    print()
    print("[DONE] Gate reachability analysis complete.")


if __name__ == "__main__":
    main()
