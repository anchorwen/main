"""FIX-20260824-004 Live Fire 敢死队 — 验收闸门 G5-G8 (投委会方向 B 裁决).

接线: core/runtime/shadow_ops/live_fire_breaker.py 熔断器 +
core/runtime/live_cycle.py 模块级函数 _dispatch_live_fire_micro_scaler +
core/runtime/shadow_ops/runtime.py live_fire 开关 (默认 False).

  G5 熔断器   — evaluate_drawdown 事件溯源 (magic 过滤 + action=close + pnl 非空) /
                熔断判定 / flag 写读 / 幂等首时间保留
  G6 开关语义 — ShadowOpsRuntime 默认 disabled → live_fire_enabled False, config {}
  G7 止血带   — breaker OPEN / cooldown / 有持仓 / 无价格 → skip, 零派发
  G8 真实派发 — 正常 D10 信号 → dispatch_live_order 调用 + 事件落盘 + 节流时间戳
  G9 触发语义 — FIX-20260824-005: 触发判定基于 raw |pred| (非校准 cal). 真实
                模型 + 真实 trigger + 真实特征行, 断言 triggered == (abs(raw)>=thr).

红线: 熔断器 OPEN 即 fail-closed (永久停火); 敢死队 payload 无 shadow 标记
(strategy="micro_scaler_v2_live_fire" 无 "shadow_ops_" 前缀) → Layer-2 fuse 放行;
单笔 SL/TP 永远在场 (ATR 或 % 双下限取 max, FIX-20260824-005 对称 1×ATR).
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
    # FIX-20260824-005 (IC 裁决): 对称 1×ATR 括号 (SL=TP=1×ATR).
    # 原 tp_pred_mult (pred 0.08%×2000.5=$1.6) vs sl 2×ATR ($5.0) → RR=0.32 被证伪.
    "sl_atr_mult": 1.0,
    "tp_atr_mult": 1.0,
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


def test_g5_evaluate_drawdown_twin_write_dedup(tmp_path):
    """FIX-20260826-003/DQAF-20260826-003: 双写重复平仓行只计一次 (consumer 幂等).

    回归锁: 若未来有人移除 ticket 级去重, XAU twin-write 会把同一笔已实现盈亏
    重复累加 → 假熔断. 同一 position_ticket 的重复 close 记录必须只计一次.
    MT5 Deal IN/OUT close 顶层 magic=0 (magic 挂 open) → 经 FIX-001 ticket 反查继承.
    """
    from core.runtime.shadow_ops.live_fire_breaker import evaluate_drawdown

    j = tmp_path / "live_trade_journal.jsonl"
    _write_journal(
        j,
        [
            # open 承载真实 magic (FIX-20260826-001: close.magic 可能为 0/None)
            {"action": "open", "magic": 90601, "position_ticket": "T1", "pnl": None},
            # twin-write: 同一 ticket 两条完全相同的 close (桥接器重试/回调重复)
            {"action": "close", "magic": 0, "position_ticket": "T1", "pnl": -8.7},
            {"action": "close", "magic": 0, "position_ticket": "T1", "pnl": -8.7},
        ],
    )
    r = evaluate_drawdown(journal_path=j, magic=90601, max_drawdown_usd=50.0)
    assert r["realized_pnl_usd"] == pytest.approx(-8.7)  # 只计一次, 非 -17.4
    assert r["n_closed"] == 1
    assert r["breached"] is False


def test_g5_evaluate_drawdown_dedup_keeps_distinct_tickets(tmp_path):
    """FIX-20260826-003: 不同 position_ticket 的平仓行互不干扰 (去重不误伤)."""
    from core.runtime.shadow_ops.live_fire_breaker import evaluate_drawdown

    j = tmp_path / "live_trade_journal.jsonl"
    _write_journal(
        j,
        [
            {"action": "open", "magic": 90601, "position_ticket": "T1", "pnl": None},
            {"action": "open", "magic": 90601, "position_ticket": "T2", "pnl": None},
            {"action": "close", "magic": 0, "position_ticket": "T1", "pnl": -20.0},
            {"action": "close", "magic": 0, "position_ticket": "T2", "pnl": -6.0},
        ],
    )
    r = evaluate_drawdown(journal_path=j, magic=90601, max_drawdown_usd=50.0)
    assert r["realized_pnl_usd"] == pytest.approx(-26.0)  # 两票各计一次
    assert r["n_closed"] == 2


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


def test_g6_real_config_live_fire_enabled_guarded():
    """真实 configs/live.yaml 上, live_fire 点火 (FIX-20260824-005) → enabled True.

    原断言 "must stay disabled" 是 FIX-005 点火前的部署安全基线; IC 裁决方向 B
    点火后, 敢死队安全护栏从"默认禁用"转为"$50 生死状 fail-closed 熔断"
    (FIX-20260826-001 Sev-1). 本测试锁存点火状态下安全关键参数被正确读取:
      - 点火开关 = True (FIX-005)
      - 生死状 max_drawdown_usd = 50.0 (全局共享)
      - RR 1:2 非对称括号 (sl_atr_mult=1.0 / tp_atr_mult=2.0, FIX-20260826-002 P2)
    若 config 致 live_fire 畸形 → fail-closed 返回 disabled (由
    test_g6_runtime_live_fire_disabled_by_default 覆盖).
    """
    import tempfile

    from core.runtime.shadow_ops.runtime import ShadowOpsRuntime

    with tempfile.TemporaryDirectory() as td:
        rt = ShadowOpsRuntime(symbol="XAUUSDc", base_dir=td, config_path="configs/live.yaml")
    assert rt.live_fire_enabled is True  # FIX-005 点火: 敢死队已接火
    cfg = rt.live_fire_config
    assert cfg.get("magic") == 90601  # 敢死队专属 magic
    assert cfg.get("max_drawdown_usd") == 50.0  # 生死状 guard (全局共享)
    assert (
        cfg.get("sl_atr_mult") == 1.0 and cfg.get("tp_atr_mult") == 2.0
    )  # RR 1:2 (FIX-20260826-002 P2)


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
    # XAU: price=2000.5(ask), atr=2.5 → sl_dist=max(2.5, 1.0)=2.5; tp_dist=max(2.5, 0.6)=2.5
    # (FIX-20260824-005 对称 1×ATR 括号, RR=1.0)
    assert pl["sl"] == pytest.approx(1998.0)
    assert pl["tp"] == pytest.approx(2003.0)

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
    # short: price=bid=1999.5 → sl=+2.5 → 2002.0, tp=-2.5 → 1997.0
    # (FIX-20260824-005 对称 1×ATR 括号)
    assert pl["sl"] == pytest.approx(2002.0)
    assert pl["tp"] == pytest.approx(1997.0)


# ────────────────────────────────────────────────────────────────────
# G9 — 触发语义回归锁 (FIX-20260824-005: raw |pred| 触发, 非校准 cal)
# ────────────────────────────────────────────────────────────────────
def _load_real_feature_vectors(max_rows: int = 600) -> list[list[float]]:
    """从真实 XAU M5 特征库加载 current-gen v9_40 向量 (G9 回归用).

    与 emit 脚本同源过滤: schema_name == v9_institutional_40 + 40 全字段 + 无 NaN.
    """
    from core.features.schemas.v9_institutional_schema import V9_INSTITUTIONAL_40_FEATURES

    fs = (
        REPO_ROOT
        / "data"
        / "feature_store"
        / "records"
        / "symbol=XAUUSDc"
        / "timeframe=M5"
        / "features.jsonl"
    )
    if not fs.exists():
        return []
    names = list(V9_INSTITUTIONAL_40_FEATURES)
    canon = set(names)
    vecs: list[list[float]] = []
    for line in fs.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if rec.get("schema_name") != "v9_institutional_40":
            continue
        vals = rec.get("values", {})
        if not canon.issubset(vals.keys()):
            continue
        vec = [float(vals[nm]) for nm in names]
        if any(v != v for v in vec):  # NaN 行剔除
            continue
        vecs.append(vec)
        if len(vecs) >= max_rows:
            break
    return vecs


def test_g9_scorer_trigger_is_raw_pred_based(tmp_path):
    """触发判定 = raw |pred| >= 阈值 (FIX-20260824-005, IC 裁决).

    回归锁: 若未来有人把 scorer 触发改回 abs(cal) >= threshold, 在 Isotonic
    平坦区 (raw∈[0.0015,0.0478] → cal=0.06007 台阶吸附) 存在真实行 abs(cal)>=阈值
    而 abs(raw)<阈值 → 不变量破裂 → 本测试 FAIL. 真实模型 + 真实 trigger + 真实特征.
    """
    from core.runtime.shadow_ops.micro_scaler_scorer import MicroScalerScorer
    from core.runtime.shadow_ops.trigger_contract import TriggerContract

    model_path = REPO_ROOT / "data" / "training" / "micro_scaler_v2" / "micro_scaler_v2_reg.txt"
    trigger_path = (
        REPO_ROOT / "data" / "training" / "micro_scaler_v2" / "micro_scaler_v2_trigger.json"
    )
    report_path = (
        REPO_ROOT / "data" / "training" / "micro_scaler_v2" / "micro_scaler_v2_reg_report.json"
    )
    if not (model_path.exists() and trigger_path.exists() and report_path.exists()):
        pytest.skip("model/trigger/report artifacts missing (无数据基线)")
    # P1 (FIX-20260826-002/DQAF-20260826-002) 阈值 re-pin 至 0.075% (触发率 ~0.25%)
    # → 需放大样本池才覆盖阈值两侧, 否则 n_above=0 (高阈值尾部稀疏).
    vecs = _load_real_feature_vectors(max_rows=6000)
    if not vecs:
        pytest.skip("no current-gen v9_40 feature rows")

    scorer = MicroScalerScorer(
        model_path=model_path,
        trigger=TriggerContract(trigger_path, ttl_seconds=60),
        calibration_report_path=report_path,
        feature_schema="v9_institutional_40",
    )
    threshold = float(
        json.loads(trigger_path.read_text(encoding="utf-8"))["threshold_abs_pred_pct"]
    )
    assert threshold > 0.0

    n_above = n_below = 0
    for vec in vecs:
        sig = scorer.predict(vec)
        expected = abs(sig.raw_pred_pct) >= threshold
        assert sig.triggered == expected, (
            f"trigger 语义破裂: raw={sig.raw_pred_pct:.6f} cal={sig.pred_pct:.6f} "
            f"thr={threshold} triggered={sig.triggered} expected={expected}"
        )
        if abs(sig.raw_pred_pct) >= threshold:
            n_above += 1
        else:
            n_below += 1
    assert n_above > 0 and n_below > 0, "特征池未覆盖阈值两侧 (触发率异常)"


# ────────────────────────────────────────────────────────────────────
# G10 — 全局生死状跨树聚合 (FIX-20260826-005/DQAF-20260826-005)
# IC 2026-08-26 裁决 Q2 (一荣俱荣一损俱损): 敢死队家族同池, 非单 magic.
# 90601 XAU Micro Scaler + 90452 BTC V4_SHORT 特区 → 同 $50 血槽.
# ────────────────────────────────────────────────────────────────────
def test_g10_aggregate_multi_magic_cross_tree(tmp_path):
    """magics=(90601,90452) → 跨树聚合同池 (各树×各magic 独立求 e_DD 后求和)."""
    from core.runtime.shadow_ops.live_fire_breaker import aggregate_live_fire_drawdown

    tree_a = tmp_path / "data"  # XAU
    tree_b = tmp_path / "data_btc"  # BTC
    tree_a.mkdir()
    tree_b.mkdir()
    _write_journal(
        tree_a / "live_trade_journal.jsonl",
        [
            {"action": "close", "magic": 90601, "pnl": -20.0},  # XAU 敢死队
            {"action": "close", "magic": 99997, "pnl": -100.0},  # 非家族 → 不计
        ],
    )
    _write_journal(
        tree_b / "live_trade_journal.jsonl",
        [
            {"action": "close", "magic": 90452, "pnl": -15.0},  # BTC V4_SHORT 特区
        ],
    )
    agg = aggregate_live_fire_drawdown(
        magics=(90601, 90452),
        max_drawdown_usd=50.0,
        base_dirs=[tree_a, tree_b],
    )
    assert agg["realized_pnl_usd"] == pytest.approx(-35.0)  # -20 + -15 同池
    assert agg["n_closed"] == 2
    assert agg["breached"] is False
    assert agg["per_tree"][str(tree_a)]["by_magic"]["90601"]["realized_pnl_usd"] == pytest.approx(
        -20.0
    )
    assert agg["per_tree"][str(tree_b)]["by_magic"]["90452"]["realized_pnl_usd"] == pytest.approx(
        -15.0
    )


def test_g10_aggregate_backward_compat_single_magic(tmp_path):
    """旧调用 (magic=90601 单值) 向后兼容: 只聚合该 magic, 不透传家族兄弟."""
    from core.runtime.shadow_ops.live_fire_breaker import aggregate_live_fire_drawdown

    tree_a = tmp_path / "data"
    tree_b = tmp_path / "data_btc"
    tree_a.mkdir()
    tree_b.mkdir()
    _write_journal(
        tree_a / "live_trade_journal.jsonl",
        [
            {"action": "close", "magic": 90601, "pnl": -20.0},
        ],
    )
    _write_journal(
        tree_b / "live_trade_journal.jsonl",
        [
            {"action": "close", "magic": 90452, "pnl": -15.0},
        ],
    )
    agg = aggregate_live_fire_drawdown(
        magic=90601, max_drawdown_usd=50.0, base_dirs=[tree_a, tree_b]
    )
    assert agg["realized_pnl_usd"] == pytest.approx(-20.0)  # 90452 不计 (单 magic 口径)
    assert agg["n_closed"] == 1
    assert "90452" not in agg["per_tree"][str(tree_b)]["by_magic"]


def test_g10_aggregate_defaults_to_tracked_family(tmp_path):
    """未传 magic/magics → 缺省聚合 LIVE_FIRE_TRACKED_MAGICS (单点扩展)."""
    from core.runtime.shadow_ops.live_fire_breaker import (
        LIVE_FIRE_TRACKED_MAGICS,
        aggregate_live_fire_drawdown,
    )

    assert LIVE_FIRE_TRACKED_MAGICS == (90601, 90452)  # 家族血槽注册
    tree_a = tmp_path / "data"
    tree_b = tmp_path / "data_btc"
    tree_a.mkdir()
    tree_b.mkdir()
    _write_journal(
        tree_a / "live_trade_journal.jsonl",
        [
            {"action": "close", "magic": 90601, "pnl": -20.0},
        ],
    )
    _write_journal(
        tree_b / "live_trade_journal.jsonl",
        [
            {"action": "close", "magic": 90452, "pnl": -15.0},
        ],
    )
    agg = aggregate_live_fire_drawdown(max_drawdown_usd=50.0, base_dirs=[tree_a, tree_b])
    assert agg["realized_pnl_usd"] == pytest.approx(-35.0)  # 全家族同池
    assert agg["n_closed"] == 2


# ────────────────────────────────────────────────────────────────────
# FIX-20260826-006 — Vanguard Interceptor: check_vanguard_breaker
# (IC 最高阻断令: 把熔断器挂载到特区枪管; 非特区绝不误杀)
# ────────────────────────────────────────────────────────────────────
def test_check_vanguard_breaker_non_vanguard_never_blocks(tmp_path):
    """边界控制: 非特区 (execution_zone 空/其他) 恒 False — 熔断器只针对敢死队."""
    from core.runtime.shadow_ops.live_fire_breaker import (
        check_vanguard_breaker,
        write_breaker_flag,
    )

    # 即使 flag 已存在, 非特区也放行 (绝不误杀正常 live 策略).
    write_breaker_flag(base_dir=tmp_path, net_pnl_usd=-70.0, n_closed=6, max_drawdown_usd=50.0)
    assert check_vanguard_breaker("", str(tmp_path)) is False
    assert check_vanguard_breaker("other_zone", str(tmp_path)) is False
    assert check_vanguard_breaker("normal", str(tmp_path)) is False


def test_check_vanguard_breaker_vanguard_flag_blocks(tmp_path):
    """特区 + 生死状 flag 已存在 → True (fail-closed)."""
    from core.runtime.shadow_ops.live_fire_breaker import (
        check_vanguard_breaker,
        write_breaker_flag,
    )

    write_breaker_flag(base_dir=tmp_path, net_pnl_usd=-70.0, n_closed=6, max_drawdown_usd=50.0)
    assert check_vanguard_breaker("live_fire_vanguard", str(tmp_path)) is True


def test_check_vanguard_breaker_breach_writes_flag_and_blocks(tmp_path):
    """自感知闭环: flag 未写但聚合击穿 → 幂等写 flag + True (XAU 静默时 BTC 也拦)."""
    from core.runtime.shadow_ops.live_fire_breaker import (
        check_vanguard_breaker,
        is_breaker_open,
    )

    _write_journal(
        tmp_path / "live_trade_journal.jsonl",
        [{"action": "close", "magic": 90452, "pnl": -55.0}],
    )
    assert is_breaker_open(tmp_path) is False  # 初始无 flag
    assert check_vanguard_breaker("live_fire_vanguard", str(tmp_path)) is True
    assert is_breaker_open(tmp_path) is True  # 击穿 → flag 已幂等写入


def test_check_vanguard_breaker_vanguard_under_threshold_passes(tmp_path):
    """特区 + 未击穿 → 放行 (False)."""
    from core.runtime.shadow_ops.live_fire_breaker import check_vanguard_breaker

    _write_journal(
        tmp_path / "live_trade_journal.jsonl",
        [{"action": "close", "magic": 90452, "pnl": -20.0}],
    )
    assert check_vanguard_breaker("live_fire_vanguard", str(tmp_path)) is False
