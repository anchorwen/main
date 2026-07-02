"""Institutional SSOT reconciliation script — DQAF-20260702-FP005.
Architecture Committee mandate: align governance_state with brain config SSOT
after Changeover Vacuum field promotions (DQAF-20260702-FP001 through FP004).

Operations:
  1. SSOT reconcile — register missing brains, fix below-floor governance states
  2. Demote H1_V2: live→probation (H1_V3 is the h1_swing vanguard)
  3. Freeze M30_V3 in governance: probation→frozen (IDENTITY_LEAK confirmed)

Usage:
  python scripts/_institutional_reconcile.py           # DRY-RUN
  python scripts/_institutional_reconcile.py --apply   # EXECUTE
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
BRAINS_DIR = PROJECT_ROOT / "configs" / "brains"
GOV_PATH = DATA_DIR / "governance_state.json"

STATUS_RANK = {
    "shadow": 0,
    "candidate": 1,
    "probation": 2,
    "live": 3,
    "frozen": -1,
    "retired": -2,
    "archived": -2,
}


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(tzinfo=None).isoformat()


def load_gov() -> dict:
    if not GOV_PATH.exists():
        return {"brain_states": {}, "schema_version": "governance_state.v1"}
    return json.loads(GOV_PATH.read_text(encoding="utf-8"))


def save_gov(data: dict) -> None:
    GOV_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


def load_brain_configs() -> dict[str, dict]:
    configs: dict[str, dict] = {}
    for fpath in sorted(BRAINS_DIR.glob("*.json")):
        if "normalization" in fpath.name.lower():
            continue
        cfg = json.loads(fpath.read_text(encoding="utf-8"))
        if cfg.get("schema_version") != "brain_registry_entry.v1":
            continue
        bid = cfg.get("brain_id", fpath.stem)
        configs[bid] = cfg
    return configs


def reconcile(apply_changes: bool = False) -> dict:
    """Align governance_state.json with brain config SSOT."""
    gov = load_gov()
    gov_states = gov.setdefault("brain_states", {})
    configs = load_brain_configs()

    report: dict = {
        "newly_registered": [],
        "below_floor_restored": [],
        "above_floor_kept": [],
        "vote_weight_synced": [],
        "demotions_applied": [],
    }

    # ── 1. Register missing brains ──
    for bid, cfg in configs.items():
        if bid not in gov_states:
            cfg_status = cfg.get("status", "candidate")
            cfg_vw = cfg.get("vote_weight", 0.5)
            report["newly_registered"].append(f"{bid}: {cfg_status} (vw={cfg_vw})")
            if apply_changes:
                gov_states[bid] = {
                    "status": cfg_status,
                    "vote_weight": cfg_vw,
                    "registered_at": utc_now_iso(),
                    "last_transition_at": utc_now_iso(),
                    "transition_count": 1,
                    "freeze_count": 0,
                    "_fix_id": "DQAF-20260702-FP005",
                }

    # ── 2. Status alignment (FIX-163: config-as-floor) ──
    for bid, cfg in configs.items():
        if bid not in gov_states:
            continue
        cfg_status = cfg.get("status", "candidate")
        gov_status = gov_states[bid].get("status", "candidate")
        cfg_rank = STATUS_RANK.get(cfg_status, 0)
        gov_rank = STATUS_RANK.get(gov_status, 0)

        if cfg_status != gov_status:
            if gov_rank < cfg_rank:
                report["below_floor_restored"].append(
                    f"{bid}: gov={gov_status} → cfg={cfg_status} (below floor, restoring)"
                )
                if apply_changes:
                    gov_states[bid]["status"] = cfg_status
                    gov_states[bid]["last_transition_at"] = utc_now_iso()
                    gov_states[bid]["transition_count"] = (
                        gov_states[bid].get("transition_count", 0) + 1
                    )
                    gov_states[bid]["_fix_id"] = "DQAF-20260702-FP005"
            else:
                report["above_floor_kept"].append(
                    f"{bid}: gov={gov_status} cfg={cfg_status} (gov promoted above floor, keeping gov)"
                )

        # ── Vote weight sync ──
        cfg_vw = cfg.get("vote_weight")
        if cfg_vw is not None:
            gov_vw = gov_states[bid].get("vote_weight")
            if gov_vw is None or (cfg_vw == 0.0 and gov_vw != 0.0):
                # Special: config has explicit IC_MANDATE vote_weight → sync
                report["vote_weight_synced"].append(f"{bid}: vw {gov_vw} → {cfg_vw}")
                if apply_changes:
                    gov_states[bid]["vote_weight"] = cfg_vw

    # ── 3. IC_MANDATE demotions (explicit Architecture Committee overrides) ──
    # These are above-floor cases where the committee explicitly overrides
    # governance auto-promotions.
    ic_demotions = [
        {
            "brain_id": "Swing_V9_H1_V2",
            "target": "probation",
            "reason": (
                "IC_MANDATE:DQAF-20260702-FP005 — H1_V2 PF=1.11 marginal, "
                "H1_V3 (PF=1.97) is now the h1_swing vanguard. "
                "Demote to probation per institutional clean-up mandate. "
                "Previous: auto-promoted to live by governance Rule 75."
            ),
        },
        {
            "brain_id": "Swing_V9_M30_V3",
            "target": "frozen",
            "reason": (
                "IC_MANDATE:DQAF-20260702-FP004 — IDENTITY_LEAK confirmed: "
                "artifact_hash identical to M30_V2. Freeze governance to align "
                "with config SSOT (vote_weight=0.0 already set at config level)."
            ),
        },
    ]

    for dm in ic_demotions:
        bid = dm["brain_id"]
        if bid not in gov_states:
            report["demotions_applied"].append(f"{bid}: SKIP — not in governance")
            continue

        current = gov_states[bid].get("status", "?")
        target = dm["target"]

        if current == target:
            report["demotions_applied"].append(f"{bid}: ALREADY {target}")
            continue

        report["demotions_applied"].append(f"{bid}: {current} → {target}")
        if apply_changes:
            gov_states[bid]["status"] = target
            gov_states[bid]["last_transition_at"] = utc_now_iso()
            gov_states[bid]["transition_count"] = gov_states[bid].get("transition_count", 0) + 1
            gov_states[bid]["_ic_mandate_demotion"] = {
                "docket": "DQAF-20260702-FP005",
                "date": utc_now_iso(),
                "reason": dm["reason"],
            }

    if apply_changes:
        gov["_last_reconciled_at"] = utc_now_iso()
        gov["_reconcile_docket"] = "DQAF-20260702-FP005"
        save_gov(gov)

    return report


def verify() -> int:
    """Verify governance state consistency after reconciliation."""
    gov = load_gov()
    gov_states = gov.get("brain_states", {})
    configs = load_brain_configs()

    issues = 0
    print(f"\n{'='*70}")
    print("POST-RECONCILIATION VERIFICATION")
    print(f"{'='*70}")
    print(f"\n{'Brain ID':30s} | {'Config':12s} | {'Governance':12s} | Status")
    print(f"{'-'*30} | {'-'*12} | {'-'*12} | {'-'*20}")

    all_bids = sorted(set(list(configs.keys()) + list(gov_states.keys())))
    for bid in all_bids:
        if "Swing_V9" not in bid and not any(
            bid.startswith(p) for p in ["OU_", "BTC_", "StatArb_"]
        ):
            continue
        cfg_status = (
            configs.get(bid, {}).get("status", "MISSING") if bid in configs else "NO_CONFIG"
        )
        gov_status = (
            gov_states.get(bid, {}).get("status", "MISSING") if bid in gov_states else "NO_GOV"
        )

        match = "✅" if cfg_status == gov_status else "❌ MISMATCH"
        if cfg_status == gov_status:
            pass
        else:
            issues += 1

        print(f"{bid:30s} | {cfg_status:12s} | {gov_status:12s} | {match}")

    print(f"\nTotal mismatches: {issues}")
    return 0 if issues == 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="DQAF-20260702-FP005 Institutional Reconciliation")
    parser.add_argument("--apply", action="store_true", help="Apply changes (default: dry-run)")
    args = parser.parse_args()

    mode = "APPLY" if args.apply else "DRY-RUN"
    print(f"[DQAF-20260702-FP005] Institutional Reconciliation — {mode}")
    print("  Authority: ARCHITECTURE_COMMITTEE")
    print("  Date: 2026-07-02")
    print("  Refs: DQAF-20260702-FP001/2/3/4 (Changeover Vacuum field promotions)")
    print()

    report = reconcile(apply_changes=args.apply)

    print("── 1. Newly Registered ──")
    for item in report["newly_registered"]:
        print(f"  REGISTER: {item}")
    if not report["newly_registered"]:
        print("  (none)")

    print("\n── 2. Below-Floor Restored ──")
    for item in report["below_floor_restored"]:
        print(f"  RESTORE: {item}")
    if not report["below_floor_restored"]:
        print("  (none)")

    print("\n── 3. Above-Floor (Kept) ──")
    for item in report["above_floor_kept"]:
        print(f"  KEEP: {item}")
    if not report["above_floor_kept"]:
        print("  (none)")

    print("\n── 4. Vote Weight Synced ──")
    for item in report["vote_weight_synced"]:
        print(f"  SYNC: {item}")
    if not report["vote_weight_synced"]:
        print("  (none)")

    print("\n── 5. IC_MANDATE Demotions ──")
    for item in report["demotions_applied"]:
        print(f"  DEMOTE: {item}")

    if args.apply:
        print("\n[OK] Changes applied. governance_state.json updated.")
        return verify()
    else:
        print("\n[DRY-RUN] No changes applied. Use --apply to execute.")
        return 0


if __name__ == "__main__":
    sys.exit(main())
