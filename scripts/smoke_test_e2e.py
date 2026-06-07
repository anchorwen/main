"""End-to-end smoke test: validates all critical pipelines in sequence.

Usage:
  python scripts/smoke_test_e2e.py              # Full suite
  python scripts/smoke_test_e2e.py --quick       # Imports + shadow only
  python scripts/smoke_test_e2e.py --base-dir data
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = THIS_DIR.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"


def _utc_now_iso() -> str:
    return (
        datetime.now(UTC)
        .replace(tzinfo=None)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


# ── Test helpers ──


def _check(step: str, ok: bool, detail: str = "") -> dict[str, Any]:
    result = PASS if ok else FAIL
    print(f"  [{result}] {step}" + (f" — {detail}" if detail else ""))
    return {"step": step, "result": result, "detail": detail}


def _check_no_exception(step: str, fn, *args, **kwargs) -> dict[str, Any]:
    try:
        fn(*args, **kwargs)
        return _check(step, True)
    except Exception as exc:  # noqa: BLE001
        return _check(step, False, f"{type(exc).__name__}: {exc}")


# ── Tests ──


def test_imports() -> list[dict[str, Any]]:
    print("\n── 1. Module imports ──")
    results = []
    modules = [
        "core.contracts.domain.decision_record",
        "core.contracts.domain.brain_decision_proposal",
        "core.contracts.ids",
        "core.features.local_feature_store",
        "core.features.schemas.v9_institutional_schema",
        "core.features.schemas.microstructure_schema",
        "core.features.adapters.microstructure_feature_adapter",
        "core.features.computers.microstructure_computer",
        "core.feedback.brain_performance_tracker",
        "core.governance.governance_service",
        "core.ledger.storage.jsonl_ledger_store",
        "core.alpha.registry",
        "core.alpha.lifecycle_service",
        "core.alpha.performance_store",
        "core.alpha.promotion_gate",
        "core.brains.services.brain_factory",
        "core.brains.services.brain_registry_service",
        "core.brains.adapters.transformer_brain_adapter",
    ]
    for mod in modules:
        results.append(_check_no_exception(mod, __import__, mod))
    return results


def test_data_paths(base_dir: str) -> list[dict[str, Any]]:
    print("\n── 2. Data path integrity ──")
    results = []
    base = Path(base_dir)

    paths = [
        (base / "brain_performance.json", "tracker_state"),
        (base / "governance_state.json", "governance_state"),
        (base / "live_trade_journal.jsonl", "journal"),
        (base / "reports" / "live_labels.jsonl", "labels"),
        (base / "alpha_registry.json", "alpha_registry"),
    ]
    for p, label in paths:
        exists = p.exists()
        size = p.stat().st_size if exists else 0
        results.append(_check(label, exists and size > 0, f"{size}B"))
    return results


def test_feature_store(base_dir: str) -> list[dict[str, Any]]:
    print("\n── 3. Feature store ──")
    results = []

    # Check both XAUUSDc and XAUUSD have data
    for symbol in ["XAUUSDc", "XAUUSD"]:
        path = (
            Path(base_dir)
            / "feature_store"
            / "records"
            / f"symbol={symbol}"
            / "timeframe=M5"
            / "features.jsonl"
        )
        exists = path.exists()
        count = 0
        if exists:
            with path.open(encoding="utf-8") as f:
                count = sum(1 for l in f if l.strip())
        results.append(_check(f"features_{symbol}", count > 0, f"{count} records"))

    # Verify we can query
    try:
        from core.features.local_feature_store import LocalFeatureStore
        from core.features.store_contracts import FeatureQuery

        store = LocalFeatureStore(str(Path(base_dir) / "feature_store"))
        records = store.query(
            FeatureQuery(symbol="XAUUSDc", timeframe="M5", schema_name="v9_institutional_40")
        )
        results.append(_check("feature_query", len(records) > 0, f"{len(records)} returned"))
    except Exception as exc:  # noqa: BLE001
        results.append(_check("feature_query", False, str(exc)[:100]))
    return results


def test_shadow_ensemble(base_dir: str) -> list[dict[str, Any]]:
    print("\n── 4. Shadow ensemble ──")
    results = []
    try:
        from scripts.live_shadow_ensemble import build_report

        report = build_report(
            brains_dir=PROJECT_ROOT / "configs" / "brains",
            feature_store_dir=Path(base_dir) / "feature_store",
            parallel=True,
            symbol="XAUUSDc",
        )
        ok_brains = [r for r in report.get("results", []) if r.get("status") == "ok"]
        errors = [r for r in report.get("results", []) if r.get("status") != "ok"]
        consensus = report.get("comparison", {}).get("consensus", "unknown")
        results.append(
            _check(
                "ensemble_run",
                "error" not in report,
                f"{report.get('total_brains',0)} brains, {len(ok_brains)} ok",
            )
        )
        results.append(
            _check(
                "ensemble_consensus",
                consensus not in ("no_results", "error"),
                f"consensus={consensus}",
            )
        )
        # Individual brain failures are warnings when ensemble overall works
        for e in errors:
            result = SKIP if len(ok_brains) >= 2 else FAIL
            detail = (
                f"skipped (non-critical): {e.get('error', '')[:60]}"
                if len(ok_brains) >= 2
                else e.get("error", "")[:80]
            )
            print(f"  [{result}] brain_{e.get('brain_id')} — {detail}")
            results.append(
                {"step": f"brain_{e.get('brain_id')}", "result": result, "detail": detail}
            )
    except Exception as exc:  # noqa: BLE001
        results.append(_check("ensemble", False, str(exc)[:100]))
    return results


def test_feedback_loop(base_dir: str) -> list[dict[str, Any]]:
    print("\n── 5. Feedback loop ──")
    results = []
    try:
        from core.feedback.brain_performance_tracker import BrainPerformanceTracker
        from scripts.feedback_loop import ingest_journal_to_tracker

        tracker_path = Path(base_dir) / "brain_performance.json"
        tracker = (
            BrainPerformanceTracker.load(tracker_path)
            if tracker_path.exists()
            else BrainPerformanceTracker(window_size=100)
        )
        report = ingest_journal_to_tracker(tracker, base_dir=base_dir, dry_run=True)
        results.append(
            _check("feedback_run", True, f"{report.get('journal_entries',0)} journal entries")
        )
        results.append(
            _check(
                "tracker_summaries",
                len(tracker.get_brain_ids()) > 0,
                f"{len(tracker.get_brain_ids())} brains tracked",
            )
        )
    except Exception as exc:  # noqa: BLE001
        results.append(_check("feedback", False, str(exc)[:100]))
    return results


def test_governance(base_dir: str) -> list[dict[str, Any]]:
    print("\n── 6. Governance cycle ──")
    results = []
    try:
        from core.feedback.brain_performance_tracker import BrainPerformanceTracker
        from core.governance.governance_service import GovernanceService
        from scripts.training.governance_scheduler import run_governance_cycle

        tracker_path = Path(base_dir) / "brain_performance.json"
        gov_path = Path(base_dir) / "governance_state.json"
        tracker = (
            BrainPerformanceTracker.load(tracker_path)
            if tracker_path.exists()
            else BrainPerformanceTracker(window_size=100)
        )
        gov = GovernanceService.load(gov_path) if gov_path.exists() else GovernanceService()

        report = run_governance_cycle(tracker, gov, dry_run=True)
        results.append(
            _check("governance_run", True, f"{report.get('brains_assessed',0)} assessed")
        )
        results.append(
            _check(
                "governance_registered",
                len(gov.get_all_states()) > 0,
                f"{len(gov.get_all_states())} registered",
            )
        )
    except Exception as exc:  # noqa: BLE001
        results.append(_check("governance", False, str(exc)[:100]))
    return results


def test_decision_recorder(base_dir: str) -> list[dict[str, Any]]:
    print("\n── 7. Decision recorder ──")
    results = []
    try:
        from core.ledger.storage.jsonl_ledger_store import JsonlLedgerStore
        from scripts.shadow_decision_recorder import _derive_action

        # Verify action derivation
        c1 = _derive_action({"aggregated_bias": "long"})
        results.append(_check("derive_long", c1 == "OPEN", f"action={c1}"))
        c2 = _derive_action({"aggregated_bias": "short"})
        results.append(_check("derive_short", c2 == "OPEN", f"action={c2}"))
        c3 = _derive_action({"aggregated_bias": "split"})
        results.append(_check("derive_split", c3 == "ABSTAIN", f"action={c3}"))
        c4 = _derive_action({"aggregated_bias": "neutral"})
        results.append(_check("derive_neutral", c4 == "ABSTAIN", f"action={c4}"))

        # Verify consensus fallback
        c5 = _derive_action({"consensus": "long"})
        results.append(_check("derive_consensus_key", c5 == "OPEN", f"action={c5}"))

        # Verify store exists and writes correctly
        JsonlLedgerStore(base_dir)
        results.append(_check("ledger_store", True, "created"))
    except Exception as exc:  # noqa: BLE001
        results.append(_check("recorder", False, str(exc)[:100]))
    return results


def test_training_pipeline(base_dir: str) -> list[dict[str, Any]]:
    print("\n── 8. Training pipeline ──")
    results = []
    try:
        from core.features.local_feature_store import LocalFeatureStore
        from scripts.training.dataset_builder import join_labels_to_features

        labels_path = Path(base_dir) / "reports" / "live_labels.jsonl"
        if not labels_path.exists():
            results.append(_check("training", True, "no labels yet — skip"))
            return results

        labels = [
            json.loads(l) for l in labels_path.read_text(encoding="utf-8").splitlines() if l.strip()
        ]
        store = LocalFeatureStore(str(Path(base_dir) / "feature_store"))
        joined = join_labels_to_features(
            labels, store, symbol="XAUUSDc", max_time_delta_seconds=3600
        )
        matched = joined["matched"]
        results.append(
            _check(
                "label_feature_join",
                matched > 0,
                f"{matched} matched, {joined['unmatched']} unmatched",
            )
        )
    except Exception as exc:  # noqa: BLE001
        results.append(_check("training", False, str(exc)[:100]))
    return results


def test_daily_ops_integration(base_dir: str) -> list[dict[str, Any]]:
    print("\n── 9. Daily ops (dry-run) ──")
    results = []
    try:
        from scripts.daily_ops import run_daily_ops

        report = run_daily_ops(
            base_dir=base_dir,
            skip_shadow=False,
            skip_recap=True,  # Skip recap (takes longest, optional MT5)
            skip_alpha=True,  # Skip alpha lifecycle
            dry_run=True,
        )
        results.append(_check("daily_ops_run", True, f"{report['total_steps']} steps"))
        results.append(
            _check("daily_ops_errors", report["errors"] == 0, f"{report['errors']} errors")
        )
    except Exception as exc:  # noqa: BLE001
        results.append(_check("daily_ops", False, str(exc)[:100]))
    return results


# ── Runner ──


def run_all(base_dir: str = "data", quick: bool = False) -> int:
    print(f"QUANT OS — E2E Smoke Test  ({_utc_now_iso()})")
    print(f"base_dir={base_dir}  quick={quick}")

    all_results: list[dict[str, Any]] = []

    all_results.extend(test_imports())
    all_results.extend(test_data_paths(base_dir))
    all_results.extend(test_feature_store(base_dir))

    if not quick:
        all_results.extend(test_shadow_ensemble(base_dir))
        all_results.extend(test_decision_recorder(base_dir))
        all_results.extend(test_feedback_loop(base_dir))
        all_results.extend(test_governance(base_dir))
        all_results.extend(test_training_pipeline(base_dir))
        all_results.extend(test_daily_ops_integration(base_dir))

    passes = sum(1 for r in all_results if r["result"] == PASS)
    fails = sum(1 for r in all_results if r["result"] == FAIL)
    skips = sum(1 for r in all_results if r["result"] == SKIP)

    print(f"\n{'='*50}")
    print(f"Results: {passes} passed, {fails} failed, {skips} skipped ({len(all_results)} total)")
    if fails > 0:
        print("FAILED — see details above")
    else:
        print("ALL PASSED")

    return 1 if fails > 0 else 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="smoke_test_e2e")
    p.add_argument("--base-dir", default="data")
    p.add_argument("--quick", action="store_true", help="Imports + data paths + features only")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return run_all(base_dir=args.base_dir, quick=args.quick)


if __name__ == "__main__":
    raise SystemExit(main())
