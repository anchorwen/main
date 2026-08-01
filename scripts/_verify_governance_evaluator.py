"""Verify the observation hold mechanism (FIX-20260801-012) deterministically.

Iron Law #11: stdout is the sole legal evidence.

NOTE: data_btc/brain_performance.json is a ROLLING window-100 updated by the
live trading system every cycle — V4's real-time PF has been observed moving
0.69 → 0.94 within minutes.  Any assertion pinned to live data is therefore
flaky by construction.  This script therefore verifies the HOLD MECHANISM
with synthetic, deterministic data:

  1. Config SSOT: BTC_Swing_V4.observation_hold_until loads from config.
  2. HOLD BLOCK: a held live brain's throttle (live→probation) is refused by
     the sole writer (execute_transitions) → status stays live.
  3. NO-HOLD control: the same throttle WITHOUT a hold is applied.
  4. EXPIRED-hold control: an expired hold no longer blocks demotion.
  5. PROMOTION passthrough: a held brain's promotion is never blocked.
  6. Live sanity: evaluate_governance_state on a snapshot keeps V4 live.
"""

from __future__ import annotations

import shutil
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from core.brains.services.brain_promotion import BrainPromotionDecision
from core.deployment.governance_evaluator import (
    evaluate_governance_state,
    load_observation_holds,
)
from core.governance.governance_rule_engine import GovernanceRuleEngine
from core.governance.governance_service import GovernanceService

NOW_UTC = datetime.now(UTC).replace(tzinfo=None)


def _make_decision(brain_id: str, current: str, target: str) -> BrainPromotionDecision:
    return BrainPromotionDecision(
        brain_id=brain_id,
        current_status=current,
        action="throttle" if target == "probation" and current == "live" else "promote",
        target_status=target,
        approved=True,
        reasons=["synthetic_throttle" if target == "probation" else "synthetic_promote"],
        metrics_snapshot={"win_rate": 0.35, "profit_factor": 0.60, "signal_count": 100},
    )


def main() -> int:
    # ── Assert 1: config SSOT hold loads ──
    holds = load_observation_holds("configs/brains_btc")
    print("=== OBSERVATION HOLDS (configs/brains_btc) ===")
    for bid, dt in sorted(holds.items()):
        print(f"  {bid}: hold_until={dt.isoformat()}Z")
    v4_hold = holds.get("BTC_Swing_V4")
    assert v4_hold is not None, "V4 observation_hold_until missing from config SSOT!"
    assert v4_hold == datetime(2026, 8, 3, 23, 59, 59), f"V4 hold date wrong: {v4_hold}"
    print("ASSERT PASS 1: BTC_Swing_V4 hold_until=2026-08-03T23:59:59Z loaded")

    # ── Assert 2: HOLD BLOCK — throttle refused for a held live brain ──
    gov = GovernanceService()
    gov.register_brain("TestHeld", "live")
    engine = GovernanceRuleEngine(gov)
    engine.set_observation_holds({"TestHeld": NOW_UTC + timedelta(days=1)})
    changes = engine.execute_transitions([_make_decision("TestHeld", "live", "probation")])
    assert changes and "BLOCKED" in changes[0], f"throttle was NOT blocked: {changes}"
    assert gov.get_brain_state("TestHeld")["status"] == "live", "held brain was demoted!"
    print(f"ASSERT PASS 2: held live→probation BLOCKED → {changes[0]} (status stays live)")

    # ── Assert 3: NO-HOLD control — same throttle applies without a hold ──
    gov2 = GovernanceService()
    gov2.register_brain("TestHeld", "live")
    engine2 = GovernanceRuleEngine(gov2)
    changes2 = engine2.execute_transitions([_make_decision("TestHeld", "live", "probation")])
    assert changes2 and "BLOCKED" not in changes2[0], f"no-hold throttle was blocked: {changes2}"
    assert (
        gov2.get_brain_state("TestHeld")["status"] == "probation"
    ), "no-hold throttle did not apply"
    print(f"ASSERT PASS 3: no-hold live→probation applied → {changes2[0]}")

    # ── Assert 4: EXPIRED-hold control — expired hold no longer blocks ──
    gov3 = GovernanceService()
    gov3.register_brain("TestHeld", "live")
    engine3 = GovernanceRuleEngine(gov3)
    engine3.set_observation_holds({"TestHeld": NOW_UTC - timedelta(days=1)})
    assert not engine3._hold_blocked(
        "TestHeld", "live", "probation"
    ), "expired hold must NOT block demotion"
    changes3 = engine3.execute_transitions([_make_decision("TestHeld", "live", "probation")])
    assert changes3 and "BLOCKED" not in changes3[0], f"expired-hold throttle blocked: {changes3}"
    assert gov3.get_brain_state("TestHeld")["status"] == "probation"
    print(f"ASSERT PASS 4: expired hold → demotion resumes → {changes3[0]}")

    # ── Assert 5: PROMOTION passthrough — held brain promotion is never blocked ──
    gov4 = GovernanceService()
    gov4.register_brain("TestHeld", "probation")
    engine4 = GovernanceRuleEngine(gov4)
    engine4.set_observation_holds({"TestHeld": NOW_UTC + timedelta(days=1)})
    changes4 = engine4.execute_transitions([_make_decision("TestHeld", "probation", "live")])
    assert changes4 and "BLOCKED" not in changes4[0], f"held promotion was blocked: {changes4}"
    assert gov4.get_brain_state("TestHeld")["status"] == "live"
    print(f"ASSERT PASS 5: held probation→live promotion passes through → {changes4[0]}")

    # ── Assert 6: live sanity — evaluate_governance_state keeps V4 live ──
    # Live-data dependent, so only assert the INVARIANT: V4 is never demoted
    # while the hold is active (regardless of whether the evaluator throttles
    # or holds this snapshot).
    src_dir = Path("data_btc")
    with tempfile.TemporaryDirectory(prefix="gov_eval_verify_") as tmp:
        tmp_dir = Path(tmp)
        for fname in ("governance_state.json", "brain_performance.json"):
            src = src_dir / fname
            if not src.exists():
                print(f"MISSING: {src}")
                return 2
            shutil.copy2(src, tmp_dir / fname)
        gov_snap = GovernanceService.load(str(tmp_dir / "governance_state.json"))
        result = evaluate_governance_state(gov_snap, tmp_dir, brains_dir="configs/brains_btc")
        v4 = [d for d in result["decisions"] if d["brain_id"] == "BTC_Swing_V4"]
        if v4:
            print(
                f"  V4 snapshot decision: action={v4[0]['action']} "
                f"approved={v4[0]['approved']} reasons={v4[0]['reasons']}"
            )
        for c in result["changes"]:
            print(f"  change: {c}")
        gov_after = GovernanceService.load(str(tmp_dir / "governance_state.json"))
        st = gov_after.get_brain_state("BTC_Swing_V4")
        assert st["status"] == "live", f"V4 was demoted despite hold! status={st['status']}"
        print("ASSERT PASS 6: V4 stays live after evaluate_governance_state (hold invariant)")

    print("VERIFY OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
