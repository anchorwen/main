"""Pure-logic tests for live_intent_loop (no MetaTrader5)."""

from scripts.live_intent_loop import (
    compute_sl_tp_for_side,
    cooldown_blocks_fire,
    decide_side_from_anchor,
)
from scripts.send_live_order import resolve_protection_flag_path


def test_decide_side_long_short_none():
    assert decide_side_from_anchor(101.0, 90.0, 10.0) == "long"
    assert decide_side_from_anchor(89.0, 100.0, 10.0) == "short"
    assert decide_side_from_anchor(95.0, 100.0, 10.0) is None


def test_compute_sl_tp_long_short():
    sl, tp, ref = compute_sl_tp_for_side(
        "long",
        ref_long=100.0,
        ref_short=99.9,
        sl_distance=5.0,
        tp_distance=10.0,
    )
    assert (sl, tp, ref) == (95.0, 110.0, 100.0)

    sl, tp, ref = compute_sl_tp_for_side(
        "short",
        ref_long=100.0,
        ref_short=99.9,
        sl_distance=5.0,
        tp_distance=10.0,
    )
    assert (sl, tp, ref) == (104.9, 89.9, 99.9)


def test_cooldown_blocks_fire():
    assert cooldown_blocks_fire(100.0, 0.0, 150.0) is True
    assert cooldown_blocks_fire(200.0, 0.0, 150.0) is False


def test_resolve_protection_flag_prefers_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    flag = tmp_path / "data" / "live_dispatch_block.flag"
    flag.write_text("{}", encoding="utf-8")
    resolved = resolve_protection_flag_path("data", "data/live_dispatch_block.flag")
    assert resolved.resolve() == flag.resolve()


def test_resolve_protection_flag_fallback_base_dir(tmp_path, monkeypatch):
    wrong = tmp_path / "wrong_cwd"
    wrong.mkdir()
    monkeypatch.chdir(wrong)
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    flag = data_dir / "live_dispatch_block.flag"
    flag.write_text("{}", encoding="utf-8")
    resolved = resolve_protection_flag_path(str(data_dir), "data/live_dispatch_block.flag")
    assert resolved.resolve() == flag.resolve()
