"""FIX-20260824-004 Live Fire 敢死队 — 验收闸门 G5-G8 (投委会方向 B 裁决).

接线: core/runtime/shadow_ops/live_fire_breaker.py 熔断器 +
core/runtime/live_cycle.py 模块级函数 _dispatch_live_fire_micro_scaler +
core/runtime/shadow_ops/runtime.py live_fire 开关 (默认 False).

  G5 熔断器   — evaluate_drawdown 事件溯源 (magic 过滤 + action=close + pnl 非空) /
                熔断判定 / flag 写读 / 幂等首时间保留
  G6 开关语义 — ShadowOpsRuntime 默认 disabled → live_fire_enabled False, config {}
  G7 止血带   — breaker OPEN / cooldown / 有持仓 / 无价格 → skip, 零派发
  G8 真实派发 — 正常 D10 信号 → dispatch_live_order 调用 + 事件落盘 + 节流时间戳

红线: 熔断器 OPEN 即 fail-closed (永久停火); 敢死队 payload 无 shadow 标记
(strategy="micro_scaler_v2_live_fire" 无 "shadow_ops_" 前缀) → Layer-2 fuse 放行;
单笔 SL/TP 永远在场 (ATR 或 % 双下限取 max).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

_LF_CONFIG: dict[str, Any] = {
    "magic": 90601,
    "volume": 0.01,
    "cooldown_seconds": 1500.0,
    "max_drawdown_usd": 50.0,
    "sl_atr_mult": 2.0,
    "tp_pred_mult": 1.0,
    "min_sl_pct": 0.05,
    "min_tp_pct": 0.03,
    "block_when_positions": True,
}


def _write_journal(path: Path, recs: list[Any]) -> None:
    # recs 可为 dict (JSON 序列化) 或 str (原始行, 模拟坏行)
    with open(path, "w", encoding="utf-8") as fh:
        for rec in recs:
            fh.write(rec if isinstance(rec, str) else json.dumps(rec, ensure_ascii=False) + "\n")


# ────────────────────────────────────────────────────────────────────
# G5 — 熔断器 (事件溯源 + 幂等)
# ────────────────────────────────────────────────────────────────────
def test_g5_evaluate_drawdown_empty_journal(tmp_path):
    from core.runtime.shadow_ops.live_fire_breaker import evaluate_drawdown

    r = evaluate_drawdown(
        journal_path=tmp_path / "live_trade_journal.jsonl", magic=90601, max_drawdown_usd=50.0
    )
    assert r["realized_pnl_usd"] == 0.0
    assert r["n_closed"] == 0
    assert r["breached"] is False


def test_g5_evaluate_drawdown_filters(tmp_path):
    from core.runtime.shadow_ops.live_fire_breaker import evaluate_drawdown

    j = tmp_path / "live_trade_journal.jsonl"
    _write_journal(
        j,
        [
            {"action": "close", "magic": 90601, "pnl": -20.0},  # 敢死队 close → 计入
            {"action": "open", "magic": 90601, "pnl": None},  # open 不计
            {"action": "close", "magic": 99999, "pnl": -999.0},  # 其他 magic 不计
            {"action": "close", "magic": 90601, "pnl": None},  # pnl 缺失 (pending) 不计
            {"action": "close", "magic": 90601, "pnl": -12.5},  # 计入
            {"action": "close", "magic": 90601, "pnl": -19.0},  # 计入
            "{not-json",  # 坏行跳过
        ],
    )
    r = evaluate_drawdown(journal_path=j, magic=90601, max_drawdown_usd=50.0)
    assert r["realized_pnl_usd"] == pytest.approx(-51.5)
    assert r["n_closed"] == 3
    assert r["breached"] is True  # -51.5 <= -50 生死状击穿


def test_g5_evaluate_drawdown_below_line_not_breached(tmp_path):
    from core.runtime.shadow_ops.live_fire_breaker import evaluate_drawdown

    j = tmp_path / "live_trade_journal.jsonl"
    _write_journal(j, [{"action": "close", "magic": 90601, "pnl": -49.99}])
    r = evaluate_drawdown(journal_path=j, magic=90601, max_drawdown_usd=50.0)
    assert r["realized_pnl_usd"] == pytest.approx(-49.99)
    assert r["breached"] is False


def test_g5_breaker_flag_write_read_idempotent(tmp_path):
    from core.runtime.shadow_ops.live_fire_breaker import (
        is_breaker_open,
        live_fire_flag_path,
        write_breaker_flag,
    )

    assert not is_breaker_open(tmp_path)
    p1 = write_breaker_flag(base_dir=tmp_path, net_pnl_usd=-60.0, n_closed=5, max_drawdown_usd=50.0)
    assert live_fire_flag_path(tmp_path) == p1
    assert is_breaker_open(tmp_path)  # flag 存在 = OPEN (fail-closed)
    first_at = json.loads(p1.read_text(encoding="utf-8"))["breaker_at_utc"]

    # 幂等: 二次写保留首次熔断时间
    time.sleep(0.01)
    write_breaker_flag(base_dir=tmp_path, net_pnl_usd=-70.0, n_closed=6, max_drawdown_usd=50.0)
    again = json.loads(p1.read_text(encoding="utf-8"))
    assert again["breaker_at_utc"] == first_at
    assert again["net_pnl_usd"] == pytest.approx(-70.0)
    assert again["state"] == "OPEN"


# ────────────────────────────────────────────────────────────────────
# G6 — runtime 开关语义 (默认 disabled, fail-open)
# ────────────────────────────────────────────────────────────────────
def test_g6_runtime_live_fire_disabled_by_default(tmp_path):
    from core.runtime.shadow_ops.runtime import ShadowOpsRuntime

    rt = ShadowOpsRuntime(
        symbol="XAUUSDc",
        base_dir=str(tmp_path),
        config_path=str(tmp_path / "missing.yaml"),  # 无效配置路径 → 整体 disabled (fail-open)
    )
    assert rt.enabled is False
    assert rt.live_fire_enabled is False
    assert rt.live_fire_config == {}


def test_g6_real_config_live_fire_still_disabled():
    """真实 configs/live.yaml 上, live_fire 必须保持 disabled (部署安全基线)."""
    import tempfile

    from core.runtime.shadow_ops.runtime import ShadowOpsRuntime

    with tempfile.TemporaryDirectory() as td:
        rt = ShadowOpsRuntime(symbol="XAUUSDc", base_dir=td, config_path="configs/live.yaml")
    assert rt.live_fire_enabled is False  # 未点火 → 绝不真实派发
    assert rt.live_fire_config == {}


# ────────────────────────────────────────────────────────────────────
# G7 — 止血带 (skip 路径, 零派发)
# ────────────────────────────────────────────────────────────────────
def _env(tmp_path: Path):
    from core.runtime.live_cycle import LiveCycleConfig, _dispatch_live_fire_micro_scaler

    config = LiveCycleConfig(
        symbol="XAUUSDc",
        base_dir=str(tmp_path),
        ignore_protection_flag=True,
        adapter_name="mt5",
    )
    state = SimpleNamespace(_live_fire_last_open={})
    broker = SimpleNamespace(
        fetch_prices=lambda symbol: (2000.0, 1999.5, 2000.5),
        fetch_current_atr=lambda symbol: 2.5,
        count_positions=lambda symbol: 0,
    )
    signal = SimpleNamespace(direction="long", pred_pct=0.08)
    so_runtime = SimpleNamespace(live_fire_config=dict(_LF_CONFIG))
    return _dispatch_live_fire_micro_scaler, config, state, broker, signal, so_runtime


def _events(tmp_path: Path) -> list[dict[str, Any]]:
    p = tmp_path / "shadow_ops" / "live_fire_events.jsonl"
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_g7_breaker_open_blocks_dispatch(tmp_path):
    from core.runtime.shadow_ops.live_fire_breaker import write_breaker_flag

    write_breaker_flag(base_dir=tmp_path, net_pnl_usd=-80.0, n_closed=7, max_drawdown_usd=50.0)
    fn, config, state, broker, signal, so_runtime = _env(tmp_path)
    with mock.patch("core.execution.live_order_sender.dispatch_live_order") as m_dispatch:
        fn(config=config, state=state, broker=broker, signal=signal, so_runtime=so_runtime)
        m_dispatch.assert_not_called()
    reasons = [e["reason"] for e in _events(tmp_path)]
    assert "skip_breaker_open" in reasons


def test_g7_cooldown_blocks_repeat_same_direction(tmp_path):
    fn, config, state, broker, signal, so_runtime = _env(tmp_path)
    state._live_fire_last_open["long"] = time.time()  # 距上次不足 1500s
    with mock.patch("core.execution.live_order_sender.dispatch_live_order") as m_dispatch:
        fn(config=config, state=state, broker=broker, signal=signal, so_runtime=so_runtime)
        m_dispatch.assert_not_called()
    assert "skip_cooldown" in [e["reason"] for e in _events(tmp_path)]


def test_g7_open_position_blocks_dispatch(tmp_path):
    fn, config, state, broker, signal, so_runtime = _env(tmp_path)
    broker.count_positions = lambda symbol: 1
    with mock.patch("core.execution.live_order_sender.dispatch_live_order") as m_dispatch:
        fn(config=config, state=state, broker=broker, signal=signal, so_runtime=so_runtime)
        m_dispatch.assert_not_called()
    assert "skip_position_open" in [e["reason"] for e in _events(tmp_path)]


def test_g7_no_price_blocks_dispatch(tmp_path):
    fn, config, state, broker, signal, so_runtime = _env(tmp_path)
    broker.fetch_prices = mock.Mock(side_effect=RuntimeError("mt5 bridge silent"))
    with mock.patch("core.execution.live_order_sender.dispatch_live_order") as m_dispatch:
        fn(config=config, state=state, broker=broker, signal=signal, so_runtime=so_runtime)
        m_dispatch.assert_not_called()
    assert "skip_no_price" in [e["reason"] for e in _events(tmp_path)]


def test_g7_dispatch_error_is_logged_not_raised(tmp_path):
    fn, config, state, broker, signal, so_runtime = _env(tmp_path)
    with mock.patch(
        "core.execution.live_order_sender.dispatch_live_order",
        side_effect=RuntimeError("order rejected"),
    ) as m_dispatch:
        fn(config=config, state=state, broker=broker, signal=signal, so_runtime=so_runtime)  # 不抛
        m_dispatch.assert_called_once()
    assert "dispatch_error" in [e["reason"] for e in _events(tmp_path)]


# ────────────────────────────────────────────────────────────────────
# G8 — 真实派发 (旁路 + 单笔 SL/TP + 节流时间戳)
# ────────────────────────────────────────────────────────────────────
def test_g8_dispatches_real_order_with_sl_tp(tmp_path):
    fn, config, state, broker, signal, so_runtime = _env(tmp_path)
    with mock.patch("core.execution.live_order_sender.dispatch_live_order") as m_dispatch:
        m_dispatch.return_value = {"status": "ok", "dispatched": True, "intent_id": "it_1"}
        fn(config=config, state=state, broker=broker, signal=signal, so_runtime=so_runtime)
        m_dispatch.assert_called_once()
    call = m_dispatch.call_args
    assert call.kwargs["base_dir"] == str(tmp_path)
    assert call.kwargs["symbol"] == "XAUUSDc"
    assert call.kwargs["skip_price_guard"] is True  # 旁路价格守卫 (已本地校验 SL/TP)
    pl: dict[str, Any] = call.kwargs["execution_payload"]
    assert pl["action"] == "open"
    assert pl["side"] == "long"
    assert pl["magic"] == 90601
    assert pl["volume"] == 0.01
    assert pl["strategy"] == "micro_scaler_v2_live_fire"  # 无 shadow_ops_ 前缀 → fuse 放行
    # XAU: price=2000.5(ask), atr=2.5 → sl_dist=max(5.0, 1.0)=5.0; tp_dist=max(1.6, 0.6)=1.6
    assert pl["sl"] == pytest.approx(1995.5)
    assert pl["tp"] == pytest.approx(2002.1)

    ev = [e for e in _events(tmp_path) if e["reason"] == "dispatched"]
    assert len(ev) == 1
    assert ev[0]["magic"] == 90601
    assert ev[0]["price"] == pytest.approx(2000.5)

    # 节流时间戳已记录 → 同向立即重发会被冷却拦截
    assert state._live_fire_last_open["long"] == pytest.approx(time.time(), abs=5.0)
    with mock.patch("core.execution.live_order_sender.dispatch_live_order") as m2:
        fn(config=config, state=state, broker=broker, signal=signal, so_runtime=so_runtime)
        m2.assert_not_called()


def test_g8_short_direction_uses_bid_price(tmp_path):
    fn, config, state, broker, signal, so_runtime = _env(tmp_path)
    signal.direction = "short"
    with mock.patch("core.execution.live_order_sender.dispatch_live_order") as m_dispatch:
        m_dispatch.return_value = {"status": "ok", "dispatched": True}
        fn(config=config, state=state, broker=broker, signal=signal, so_runtime=so_runtime)
        m_dispatch.assert_called_once()
    pl: dict[str, Any] = m_dispatch.call_args.kwargs["execution_payload"]
    assert pl["side"] == "short"
    # short: price=bid=1999.5 → sl=+5.0 → 2004.5, tp=-1.6 → 1997.9
    assert pl["sl"] == pytest.approx(2004.5)
    assert pl["tp"] == pytest.approx(1997.9)
