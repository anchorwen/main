#!/usr/bin/env python
"""Window 4 - Architecture Guardrails: Cross-Symbol + Registry + Schema
======================================================================
Checks:
  A. Cross-Symbol Test Matrix: compare BTC vs XAU calibrator/governance/leaderboard
  B. Registry Reflection: do all registered modules have matching blueprints?
  C. State Schema Enforcement: validate ephemeral JSON state files

Output: self-contained closing report for architecture guardrails.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_json(path: Path) -> Any:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ── A. Cross-Symbol Comparison ──────────────────────────────────────────


def compare_calibrators() -> dict[str, Any]:
    """Compare BTC vs XAU calibrator states."""
    result: dict[str, Any] = {}
    for label, data_dir in [("BTC", Path("data_btc")), ("XAU", Path("data"))]:
        cal_path = data_dir / "conformal_calibrator_state.json"
        if cal_path.exists():
            cal = load_json(cal_path)
            history = cal.get("history", [])
            pwins = [h.get("p_win") or 0.5 for h in history]
            result[label] = {
                "exists": True,
                "entries": len(history),
                "unique_pwin": len(set(pwins)),
                "pct_05": round(sum(1 for p in pwins if p == 0.5) / max(len(pwins), 1) * 100, 1),
                "threshold": cal.get("threshold"),
                "cold_started": cal.get("cold_started"),
                "total_computations": cal.get("total_computations"),
            }
        else:
            result[label] = {"exists": False}
    return result


def compare_leaderboards() -> dict[str, Any]:
    """Compare BTC vs XAU leaderboards."""
    result: dict[str, Any] = {}
    for label, data_dir in [("BTC", Path("data_btc")), ("XAU", Path("data"))]:
        # DQAF-053: leaderboard.json lives in reports/ subdirectory
        lb_path = data_dir / "reports" / "leaderboard.json"
        if not lb_path.exists():
            lb_path = data_dir / "leaderboard.json"  # legacy location fallback
        if lb_path.exists():
            lb = load_json(lb_path)
            brains = lb.get("brains", lb.get("entries", []))
            if isinstance(brains, list):
                count = len(brains)
                statuses: dict[str, int] = {}
            elif isinstance(brains, dict):
                count = len(brains)
                statuses = {}
                for b in brains.values():
                    s = b.get("status", "unknown")
                    statuses[s] = statuses.get(s, 0) + 1
            else:
                count = 0
                statuses = {}
            result[label] = {
                "exists": True,
                "brain_count": count,
                "status_dist": statuses,
            }
        else:
            result[label] = {"exists": False, "brain_count": 0}
    return result


def compare_governance() -> dict[str, Any]:
    """Compare BTC vs XAU governance states."""
    result: dict[str, Any] = {}
    for label, data_dir in [("BTC", Path("data_btc")), ("XAU", Path("data"))]:
        gov_path = data_dir / "governance_state.json"
        if gov_path.exists():
            gov = load_json(gov_path)
            brain_states = gov.get("brain_states", gov.get("brains", {}))
            if isinstance(brain_states, dict):
                count = len(brain_states)
                live = sum(1 for b in brain_states.values() if b.get("status") == "live")
                candidate = sum(1 for b in brain_states.values() if b.get("status") == "candidate")
                frozen = sum(1 for b in brain_states.values() if b.get("status") == "frozen")
            else:
                count = len(brain_states) if isinstance(brain_states, list) else 0
                live = candidate = frozen = 0
            result[label] = {
                "exists": True,
                "total_brains": count,
                "live": live,
                "candidate": candidate,
                "frozen": frozen,
            }
        else:
            result[label] = {"exists": False, "total_brains": 0}
    return result


# ── B. Registry Reflection ──────────────────────────────────────────────


def check_registry_reflection() -> dict[str, Any]:
    """Check if all state files have corresponding generator code references."""
    result: dict[str, Any] = {}

    # Ephemeral state files (per CLAUDE.md architecture)
    ephemeral_states = [
        "leaderboard.json",
        "governance_state.json",
        "data_health_state.json",
        "execution_state.json",
        "daily_ops_state.json",
        "alpha_allocation.json",
        "mt5_bridge_health.json",
        "retraining_signal_prev.json",
        "training_readiness.json",
    ]

    # Known generator modules (from architecture docs)
    generators = {
        "leaderboard.json": "core/feedback/brain_leaderboard.py + scripts/daily_ops.py",
        "governance_state.json": "core/governance/governance_service.py + scripts/daily_ops.py",
        "data_health_state.json": "core/state/data_health.py + scripts/daily_ops.py",
        "execution_state.json": "core/execution/execution_service.py + scripts/daily_ops.py",
        "daily_ops_state.json": "scripts/daily_ops.py",
        "alpha_allocation.json": "core/execution/alpha_allocation.py",
        "mt5_bridge_health.json": "core/mt5/bridge_health.py",
        "retraining_signal_prev.json": "scripts/training/governance_scheduler.py",
        "training_readiness.json": "scripts/training/governance_scheduler.py",
    }

    for data_dir_name in ["data_btc", "data"]:
        data_dir = Path(data_dir_name)
        for state_file in ephemeral_states:
            # DQAF-053: check reports/ subdirectory first, then legacy root
            path = data_dir / "reports" / state_file
            if not path.exists():
                path = data_dir / state_file
            key = f"{data_dir_name}/{state_file}"
            if path.exists():
                gen = generators.get(state_file, "UNKNOWN")
                result[key] = {
                    "exists": True,
                    "size": path.stat().st_size,
                    "generator": gen,
                }
            else:
                result[key] = {
                    "exists": False,
                    "generator": generators.get(state_file, "UNKNOWN"),
                }

    return result


# ── C. State Schema Validation ──────────────────────────────────────────


def validate_state_schemas() -> dict[str, Any]:
    """Basic structural validation of ephemeral state JSON files."""
    result: dict[str, Any] = {}

    # Required fields per state file type
    required_fields = {
        "leaderboard.json": ["brains"],
        "governance_state.json": ["brain_states"],
        "execution_state.json": [],
        "data_health_state.json": [],
        "alpha_allocation.json": [],
        "training_readiness.json": [],
    }

    for data_dir_name in ["data_btc", "data"]:
        data_dir = Path(data_dir_name)
        for state_file, required in required_fields.items():
            # DQAF-053: check reports/ subdirectory first, then legacy root
            path = data_dir / "reports" / state_file
            if not path.exists():
                path = data_dir / state_file
            key = f"{data_dir_name}/{state_file}"
            if not path.exists():
                result[key] = {"valid": False, "error": "FILE_MISSING"}
                continue

            try:
                data = load_json(path)
            except (json.JSONDecodeError, OSError) as e:
                result[key] = {"valid": False, "error": f"JSON_ERROR: {e}"}
                continue

            missing = [f for f in required if f not in data]
            if missing:
                result[key] = {"valid": False, "error": f"MISSING_FIELDS: {missing}"}
            else:
                result[key] = {"valid": True, "keys": list(data.keys())[:10]}

    return result


def main():
    print("=" * 70)
    print("  WINDOW 4: Architecture Guardrails")
    print("=" * 70)

    # ── A. Cross-Symbol ──
    print(f"\n{'─' * 70}")
    print("  A. Cross-Symbol Test Matrix")
    print(f"{'─' * 70}")

    cal = compare_calibrators()
    print("\n  Calibrator Comparison:")
    for label, c in cal.items():
        if c["exists"]:
            print(
                f"    {label}: {c['entries']} entries, {c['unique_pwin']} unique pwin, "
                f"{c['pct_05']}% =0.5, threshold={c['threshold']}"
            )
        else:
            print(f"    {label}: FILE MISSING")

    lb = compare_leaderboards()
    print("\n  Leaderboard Comparison:")
    for label, l in lb.items():
        print(f"    {label}: {l['brain_count']} brains, status={l.get('status_dist', {})}")

    gov = compare_governance()
    print("\n  Governance Comparison:")
    for label, g in gov.items():
        print(
            f"    {label}: {g['total_brains']} total, "
            f"live={g.get('live', 0)}, candidate={g.get('candidate', 0)}, "
            f"frozen={g.get('frozen', 0)}"
        )

    # Cross-symbol inconsistencies
    print("\n  Cross-Symbol Inconsistencies:")
    issues = []
    if cal.get("BTC", {}).get("pct_05", 0) > 50 and cal.get("XAU", {}).get("pct_05", -1) < 30:
        issues.append("Calibrator: BTC heavily poisoned but XAU healthy — asymmetric degradation")
    if lb.get("BTC", {}).get("brain_count", 0) > 0 and lb.get("XAU", {}).get("brain_count", 0) == 0:
        issues.append(
            "Leaderboard: BTC has brains, XAU has 0 — BTC-only fix (FIX-132) needs XAU mirror"
        )
    if gov.get("BTC", {}).get("live", 0) == 0 and gov.get("BTC", {}).get("candidate", 0) > 0:
        issues.append(
            f"Governance: BTC has {gov['BTC']['candidate']} candidates but 0 live — governance atrophy"
        )
    if not issues:
        print("  (none)")
    else:
        for i in issues:
            print(f"  [!] {i}")

    # ── B. Registry Reflection ──
    print(f"\n{'─' * 70}")
    print("  B. Registry Reflection Assertions")
    print(f"{'─' * 70}")

    reg = check_registry_reflection()
    missing = {k: v for k, v in reg.items() if not v["exists"]}
    orphan = {k: v for k, v in reg.items() if v["exists"] and v["generator"] == "UNKNOWN"}

    print(f"  Total state files checked: {len(reg)}")
    print(f"  Present: {len(reg) - len(missing)}")
    print(f"  Missing: {len(missing)}")
    if missing:
        for k, v in missing.items():
            print(f"    [!] MISSING: {k} (generator: {v['generator']})")
    print(f"  Unknown generators: {len(orphan)}")

    # ── C. State Schema ──
    print(f"\n{'─' * 70}")
    print("  C. State Schema Enforcement")
    print(f"{'─' * 70}")

    schema = validate_state_schemas()
    invalid = {k: v for k, v in schema.items() if not v.get("valid")}
    valid = len(schema) - len(invalid)
    print(f"  Valid: {valid}/{len(schema)}")
    if invalid:
        for k, v in invalid.items():
            print(f"    [!] {k}: {v['error']}")

    # ── Verdict ──
    print(f"\n{'=' * 70}")
    print("  ARCHITECTURE GUARDRAILS VERDICT")
    print(f"{'=' * 70}")

    total_issues = len(issues) + len(missing) + len(orphan) + len(invalid)
    if total_issues > 0:
        print(f"  {total_issues} issues across 3 guardrails")
        if issues:
            print(f"\n  Cross-Symbol ({len(issues)}):")
            for i in issues:
                print(f"    - {i}")
        if missing:
            print(f"\n  Registry Reflection ({len(missing)} missing):")
            for k in missing:
                print(f"    - {k}")
        if invalid:
            print(f"\n  State Schema ({len(invalid)} invalid):")
            for k, v in invalid.items():
                print(f"    - {k}: {v['error']}")
    else:
        print("  ALL GUARDRAILS PASS")

    print("\n[DONE] Window 4 complete.")


if __name__ == "__main__":
    main()
