"""Tests for retraining_trigger module."""

import json
from pathlib import Path

# ── _assess_brain tests ──


def _make_leaderboard_entry(
    brain_id, signal_count=20, long_pct=0.55, short_pct=0.40, linked_trades=10, win_rate=0.55
):
    return {
        "brain_id": brain_id,
        "signal_count": signal_count,
        "direction_distribution": {
            "long_pct": long_pct,
            "short_pct": short_pct,
            "neutral_pct": round(1.0 - long_pct - short_pct, 4),
        },
        "trade_performance": {
            "linked_trades": linked_trades,
            "win_rate": win_rate,
            "total_pnl": 150.0,
            "avg_pnl": 15.0,
        },
    }


def test_assess_healthy_brain():
    from scripts.training.retraining_trigger import _assess_brain

    entry = _make_leaderboard_entry("V9", win_rate=0.60)
    result = _assess_brain(entry)
    assert result is None


def test_assess_win_rate_low():
    from scripts.training.retraining_trigger import _assess_brain

    entry = _make_leaderboard_entry("V9", win_rate=0.35, linked_trades=10)
    result = _assess_brain(entry)
    assert result is not None
    assert result["urgency"] == "warning"
    assert any(i["type"] == "win_rate_low" for i in result["issues"])


def test_assess_win_rate_critical():
    from scripts.training.retraining_trigger import _assess_brain

    entry = _make_leaderboard_entry("XGB", win_rate=0.25, linked_trades=15)
    result = _assess_brain(entry)
    assert result is not None
    assert result["urgency"] == "critical"
    assert any(
        i["type"] == "win_rate_low" and i["severity"] == "critical" for i in result["issues"]
    )


def test_assess_win_rate_insufficient_trades():
    from scripts.training.retraining_trigger import _assess_brain

    # Only 3 linked trades — not enough to trigger win_rate_low
    entry = _make_leaderboard_entry("V9", win_rate=0.20, linked_trades=3)
    result = _assess_brain(entry)
    # Should not have win_rate_low issue
    if result is not None:
        assert not any(i["type"] == "win_rate_low" for i in result["issues"])


def test_assess_signal_starvation():
    from scripts.training.retraining_trigger import _assess_brain

    entry = _make_leaderboard_entry("V9", signal_count=1, linked_trades=0, win_rate=None)
    result = _assess_brain(entry)
    assert result is not None
    assert any(i["type"] == "signal_starvation" for i in result["issues"])


def test_assess_direction_collapse():
    from scripts.training.retraining_trigger import _assess_brain

    entry = _make_leaderboard_entry("OU", long_pct=0.92, short_pct=0.05)
    result = _assess_brain(entry)
    assert result is not None
    assert any(i["type"] == "direction_collapse" for i in result["issues"])


def test_assess_win_rate_drop_vs_baseline():
    from scripts.training.retraining_trigger import _assess_brain

    entry = _make_leaderboard_entry("V9", win_rate=0.42, linked_trades=20)
    baseline = _make_leaderboard_entry("V9", win_rate=0.65, linked_trades=50)
    result = _assess_brain(entry, baseline)
    assert result is not None
    assert any(
        i["type"] == "win_rate_drop_vs_baseline" and i["severity"] == "critical"
        for i in result["issues"]
    )  # 0.65 → 0.42 = -0.23 > 0.20


def test_assess_win_rate_drop_warning():
    from scripts.training.retraining_trigger import _assess_brain

    entry = _make_leaderboard_entry("V9", win_rate=0.50, linked_trades=20)
    baseline = _make_leaderboard_entry("V9", win_rate=0.62, linked_trades=50)
    result = _assess_brain(entry, baseline)
    assert result is not None
    assert any(
        i["type"] == "win_rate_drop_vs_baseline" and i["severity"] == "warning"
        for i in result["issues"]
    )  # 0.62 → 0.50 = -0.12 > 0.10


def test_assess_no_baseline_no_drop_check():
    from scripts.training.retraining_trigger import _assess_brain

    entry = _make_leaderboard_entry("V9", win_rate=0.45, linked_trades=20)
    # Without baseline, 0.45 > 0.40 so no win_rate_low
    result = _assess_brain(entry)
    # 0.45 > 0.40, no direction collapse, no starvation
    assert result is None


def test_guess_lane_exact():
    from scripts.training.retraining_trigger import _guess_lane

    assert _guess_lane("V9") == "sur"
    assert _guess_lane("XGB") == "boost"
    assert _guess_lane("OU") == "arb"


def test_guess_lane_fuzzy():
    from scripts.training.retraining_trigger import _guess_lane

    assert _guess_lane("V9_institutional_01") == "sur"
    assert _guess_lane("xgboost_v4.5") == "boost"
    assert _guess_lane("unknown_brain") == "unclassified"


# ── detect_degradation tests ──


def test_detect_degradation_empty_leaderboard():
    from scripts.training.retraining_trigger import detect_degradation

    lb = {"leaderboard": [], "generated_at": "2026-05-04T00:00:00Z"}
    result = detect_degradation(lb)
    assert result["total_brains_assessed"] == 0
    assert result["degraded_count"] == 0
    assert result["overall_urgency"] == "ok"


def test_detect_degradation_all_healthy():
    from scripts.training.retraining_trigger import detect_degradation

    lb = {
        "leaderboard": [
            _make_leaderboard_entry("V9", win_rate=0.60),
            _make_leaderboard_entry("XGB", win_rate=0.55),
        ],
        "generated_at": "2026-05-04T00:00:00Z",
    }
    result = detect_degradation(lb)
    assert result["degraded_count"] == 0
    assert result["overall_urgency"] == "ok"


def test_detect_degradation_mixed():
    from scripts.training.retraining_trigger import detect_degradation

    lb = {
        "leaderboard": [
            _make_leaderboard_entry("V9", win_rate=0.65),
            _make_leaderboard_entry("XGB", win_rate=0.30, linked_trades=10),
            _make_leaderboard_entry("OU", signal_count=1, linked_trades=0, win_rate=None),
        ],
        "generated_at": "2026-05-04T00:00:00Z",
    }
    result = detect_degradation(lb)
    assert result["degraded_count"] == 2
    assert result["overall_urgency"] == "critical"  # XGB at 0.30 < 0.30 is critical


def test_detect_degradation_with_baseline():
    from scripts.training.retraining_trigger import detect_degradation

    current = {
        "leaderboard": [
            _make_leaderboard_entry("V9", win_rate=0.48, linked_trades=15),
        ],
        "generated_at": "2026-05-10T00:00:00Z",
    }
    baseline = {
        "leaderboard": [
            _make_leaderboard_entry("V9", win_rate=0.70, linked_trades=30),
        ],
        "generated_at": "2026-05-01T00:00:00Z",
    }
    result = detect_degradation(current, baseline)
    assert result["degraded_count"] == 1
    assert any(
        s["brain_id"] == "V9" and any(i["type"] == "win_rate_drop_vs_baseline" for i in s["issues"])
        for s in result["signals"]
    )


# ── detect_degradation end-to-end with JSON files ──


def test_main_dry_run_no_degradation(tmp_path: Path):
    from scripts.training.retraining_trigger import main

    leaderboard = tmp_path / "leaderboard.json"
    leaderboard.write_text(
        json.dumps(
            {
                "schema_version": "brain_leaderboard.v1",
                "generated_at": "2026-05-04T00:00:00Z",
                "leaderboard": [
                    _make_leaderboard_entry("V9", win_rate=0.62),
                    _make_leaderboard_entry("XGB", win_rate=0.58),
                ],
            }
        ),
        encoding="utf-8",
    )

    # Redirect stdout to capture JSON
    import io
    import sys

    old_stdout = sys.stdout
    sys.stdout = buf = io.StringIO()
    try:
        exit_code = main(["--leaderboard", str(leaderboard)])
    finally:
        sys.stdout = old_stdout

    assert exit_code == 0
    report = json.loads(buf.getvalue())
    assert report["degraded_count"] == 0
    assert report["overall_urgency"] == "ok"


def test_main_dry_run_with_degradation(tmp_path: Path):
    from scripts.training.retraining_trigger import main

    leaderboard = tmp_path / "leaderboard.json"
    leaderboard.write_text(
        json.dumps(
            {
                "schema_version": "brain_leaderboard.v1",
                "generated_at": "2026-05-04T00:00:00Z",
                "leaderboard": [
                    _make_leaderboard_entry("V9", win_rate=0.25, linked_trades=12),
                ],
            }
        ),
        encoding="utf-8",
    )

    import io
    import sys

    old_stdout = sys.stdout
    sys.stdout = buf = io.StringIO()
    try:
        exit_code = main(["--leaderboard", str(leaderboard)])
    finally:
        sys.stdout = old_stdout

    assert exit_code == 3  # critical urgency
    report = json.loads(buf.getvalue())
    assert report["degraded_count"] == 1
    assert report["signals"][0]["brain_id"] == "V9"
    assert report["signals"][0]["lane"] == "sur"


def test_main_missing_leaderboard(tmp_path: Path):
    import io
    import sys

    from scripts.training.retraining_trigger import main

    old_stdout = sys.stdout
    sys.stdout = io.StringIO()
    try:
        exit_code = main(["--leaderboard", str(tmp_path / "nope.json")])
    finally:
        sys.stdout = old_stdout

    assert exit_code == 2


def test_execute_retraining_dry_run(tmp_path: Path):
    from scripts.training.retraining_trigger import execute_retraining

    signals = [
        {
            "brain_id": "V9",
            "lane": "sur",
            "urgency": "warning",
            "issues": [{"type": "win_rate_low", "severity": "warning"}],
        }
    ]
    result = execute_retraining(
        signals,
        feature_store_dir=tmp_path / "features",
        output_dir=tmp_path / "output",
        dry_run=True,
    )
    assert result["executed"] is False
    assert result["results"][0]["steps"][0]["status"] == "dry_run"


def test_execute_retraining_no_actionable_lanes():
    from scripts.training.retraining_trigger import execute_retraining

    signals = [
        {
            "brain_id": "MysteryBrain",
            "lane": "unknown",
            "urgency": "warning",
            "issues": [],
        }
    ]
    result = execute_retraining(
        signals,
        feature_store_dir=Path("/tmp"),
        output_dir=Path("/tmp"),
    )
    assert result["executed"] is False
    assert result["reason"] == "no_actionable_lanes"
