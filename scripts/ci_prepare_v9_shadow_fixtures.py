"""Prepare V9 shadow JSON fixtures under D:/cursor/data for CI (Windows).

Requires the repo to be available at ``D:\\cursor`` (junction to ``GITHUB_WORKSPACE``).
Run after ``pip install -e ".[dev]"`` from the checkout root.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _write_json(path: str, payload: object) -> None:
    file_path = Path(path)
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> int:
    # CI fixture prep is definitionally stub-isolated: the shadow container is
    # built with pure in-memory stub feature sources (no MT5/ZMQ). Declare stub
    # explicitly so the Shadow Veto (TECH_DEBT-010 Blueprint A, bootstrap_v9)
    # sees a declared non-network adapter instead of inheriting the repo's
    # production adapter name (mt5_zmq) from configs/live.yaml (DQAF-20260819-006).
    os.environ.setdefault("QUANTOS_SHADOW_ADAPTER", "stub")

    # Ensure imports resolve when executed as a script
    repo_root = Path(__file__).resolve().parents[1]
    if str(repo_root) not in sys.path:
        sys.path.insert(0, str(repo_root))

    from apps.engine.main_v9_shadow import (
        build_edge_allow_stub_feature_source,
        build_edge_deny_stub_feature_source,
        build_long_actionable_stub_feature_source,
        build_short_actionable_stub_feature_source,
        build_stub_feature_source,
        rebuild_formal_baseline_suites,
    )

    Path("D:/cursor/data/snapshots").mkdir(parents=True, exist_ok=True)
    Path("D:/cursor/data/replays/v9_shadow_baselines").mkdir(parents=True, exist_ok=True)

    neutral = build_stub_feature_source()
    long_case = build_long_actionable_stub_feature_source()
    short_case = build_short_actionable_stub_feature_source()
    edge_allow = build_edge_allow_stub_feature_source()
    edge_deny = build_edge_deny_stub_feature_source()

    _write_json(
        "D:/cursor/data/snapshots/v9_shadow_neutral.json",
        {
            "name": "v9_shadow_neutral",
            "description": "Default reference sample expected to remain passive and abstain.",
            "features": neutral,
        },
    )
    _write_json(
        "D:/cursor/data/snapshots/v9_shadow_long.json",
        {
            "name": "v9_shadow_long",
            "description": "Lower H1_Hurst to push the model into an open long decision.",
            "features": long_case,
        },
    )
    _write_json(
        "D:/cursor/data/snapshots/v9_shadow_short.json",
        {
            "name": "v9_shadow_short",
            "description": "Invert the M15 feature group to trigger an open short decision.",
            "features": short_case,
        },
    )
    _write_json(
        "D:/cursor/data/snapshots/v9_shadow_edge_allow.json",
        {
            "name": "edge_allow_case",
            "description": "Borderline long setup expected to become risk-allowed.",
            "features": edge_allow,
        },
    )
    _write_json(
        "D:/cursor/data/snapshots/v9_shadow_edge_deny.json",
        {
            "name": "edge_deny_case",
            "description": "Borderline long setup expected to remain risk-blocked.",
            "features": edge_deny,
        },
    )

    _write_json(
        "D:/cursor/data/snapshots/v9_shadow_batch.json",
        [
            {
                "name": "neutral_case",
                "description": "Default reference sample expected to remain passive and abstain.",
                "features": neutral,
            },
            {
                "name": "long_case",
                "description": "Lower H1_Hurst to push the model into an open long decision.",
                "features": long_case,
            },
            {
                "name": "short_case",
                "description": "Invert the M15 feature group to trigger an open short decision.",
                "features": short_case,
            },
        ],
    )
    _write_json(
        "D:/cursor/data/snapshots/v9_shadow_actionable_batch.json",
        [
            {
                "name": "long_case",
                "description": "Lower H1_Hurst to push the model into an open long decision.",
                "features": long_case,
            },
            {
                "name": "short_case",
                "description": "Invert the M15 feature group to trigger an open short decision.",
                "features": short_case,
            },
        ],
    )
    _write_json(
        "D:/cursor/data/snapshots/v9_shadow_neutral_batch.json",
        [
            {
                "name": "neutral_case",
                "description": "Default reference sample expected to remain passive and abstain.",
                "features": neutral,
            },
        ],
    )
    _write_json(
        "D:/cursor/data/snapshots/v9_shadow_edge_batch.json",
        [
            {
                "name": "edge_allow_case",
                "description": "Borderline long setup expected to become risk-allowed.",
                "features": edge_allow,
            },
            {
                "name": "edge_deny_case",
                "description": "Borderline long setup expected to remain risk-blocked.",
                "features": edge_deny,
            },
        ],
    )

    manifest_path = "D:/cursor/data/replays/v9_shadow_baselines/manifest.json"
    _write_json(
        manifest_path,
        {
            "version": "2",
            "description": (
                "Formal baseline suites for V9 shadow neutral stability, actionable decision, "
                "and risk boundary acceptance checks."
            ),
            "suites": [
                {
                    "key": "neutral_stability",
                    "batch_file": "D:/cursor/data/snapshots/v9_shadow_neutral_batch.json",
                    "baseline_dir": "D:/cursor/data/replays/v9_shadow_baselines/neutral_stability",
                },
                {
                    "key": "actionable_decisions",
                    "batch_file": "D:/cursor/data/snapshots/v9_shadow_actionable_batch.json",
                    "baseline_dir": "D:/cursor/data/replays/v9_shadow_baselines/actionable_decisions",
                },
                {
                    "key": "risk_boundary",
                    "batch_file": "D:/cursor/data/snapshots/v9_shadow_edge_batch.json",
                    "baseline_dir": "D:/cursor/data/replays/v9_shadow_baselines/risk_boundary",
                },
            ],
        },
    )

    rebuild_formal_baseline_suites(manifest_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
