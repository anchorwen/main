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
  6. PERSISTENCE (DQAF-20260804-001): a throttle decision written by
     evaluate_governance_state survives a disk reload — the L3 fix that moved
     save() AFTER execute_transitions.  Deterministic synthetic data.
"""

from __future__ import annotations

import json
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

    # ── Assert 6: PERSISTENCE — transitions survive reload (DQAF-20260804-001) ──
    # Deterministic, no live data.  The old "V4 stays live during active hold"
    # invariant is stale — the hold expired 2026-08-03T23:59:59Z and the L3 fix
    # (save AFTER execute_transitions) now persists in-memory transitions.  This
    # asserts the FIX: a throttle decision written by evaluate_governance_state
    # must land on disk and survive a fresh GovernanceService.load().
    with tempfile.TemporaryDirectory(prefix="gov_persist_verify_") as tmp:
        tmp_dir = Path(tmp)
        gov = GovernanceService()
        gov.register_brain("TestPersist", "live")
        gov.save(str(tmp_dir / "governance_state.json"))
        # 18 wins / 42 losses → WR=0.30 (<0.38), PF=0.43 (<0.80), count=60.
        # count∈[50,100): min_live_samples reached, _new_brain=True → the
        # protected path returns a live→probation throttle (not frozen/retire).
        _recs = [{"execution_outcome": "win"} for _ in range(18)] + [
            {"execution_outcome": "loss"} for _ in range(42)
        ]
        (tmp_dir / "brain_performance.json").write_text(
            json.dumps({"records": {"TestPersist": _recs}}), encoding="utf-8"
        )
        empty_brains = tmp_dir / "brains"  # no observation holds → not blocked
        empty_brains.mkdir()
        result = evaluate_governance_state(
            gov, tmp_dir, brains_dir=str(empty_brains), manual_mode=False
        )
        persisted = [c for c in result["changes"] if "TestPersist" in c]
        print(f"  TestPersist transition in cycle: {persisted}")
        gov_after = GovernanceService.load(str(tmp_dir / "governance_state.json"))
        st = gov_after.get_brain_state("TestPersist")
        assert (
            st["status"] == "probation"
        ), f"transition did NOT persist to disk! status={st['status']}"
        print(
            "ASSERT PASS 6: throttle transition PERSISTED to governance_state.json "
            "(DQAF-20260804-001 save-after-transition)"
        )

    print("VERIFY OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
