"""TECH_DEBT-017 清偿回归测试 — FaultTolerantContext 降级路径作用域安全锁.

根因 (FIX-20260819-004, DQAF-20260819-004): 8/11→8/13 共 38 次 intent_loop 崩溃
(休市期每天 11 连崩) — FTC 级别 ∈ {DEGRADE/LOG/IGNORE} 吞异常继续执行 × Python
作用域语义 "块内 RHS 抛出则变量名永不绑定" → 异常处理路径引用未绑定局部变量 →
UnboundLocalError (继承 NameError, 不在 live_cycle/live_intent_loop 的 except
元组内) → 穿透崩溃 → launcher 5s→30s respawn 无限循环.

fault_handler.py docstring 官方自述此陷阱, 但调用点未系统性预绑定 → 根因分层
L3 架构缺陷 (Iron Law #12), 修复 = Scope-Safe Pre-binding (所有异常处理路径
可能引用的变量在块前最顶层预绑定安全默认值).

本测试锁死 4 处修复点:
  1. live_cycle.py startup_reconciliation: `_positions` 预绑定 None → DEGRADE/超时
     跳过 reconciliation, known_open_tickets 保留 (不丢持仓跟踪).
  2. live_cycle.py PnL_to_equity: `_eq` 预绑定 0.0 → DEGRADE 时 pnl_pct=0.
  3. live_intent_loop.py while 循环体最顶层: `_EVENT_STREAM_MODE = True` 前置绑定.
  4. group_consensus.py CorrelationTracker:penalty: `dynamic_volume` 预绑定
     raw_volume (无惩罚回落).

Behavioral 锁 (group_consensus): get_correlation_penalty 抛 RuntimeError
(模拟 DEGRADE 吞异常) → 不崩 + dynamic_volume == raw_volume.
静态锁 (4 处): 预绑定语句必须出现在 FTC component 标记之前 — 未来删除/后移
预绑定即触发断言.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core.parliament.group_consensus import compute_contract_group_consensus

_ROOT = Path(__file__).resolve().parents[2]


def _load(rel: str) -> str:
    return (_ROOT / rel).read_text(encoding="utf-8")


def _assert_pre_bind_before(rel: str, pre_bind: str, anchor: str) -> None:
    """Assert pre-binding statement appears before the FTC component marker."""
    src = _load(rel)
    i_pre = src.find(pre_bind)
    i_anchor = src.find(anchor)
    assert i_pre != -1, f"{rel}: pre-binding {pre_bind!r} missing"
    assert i_anchor != -1, f"{rel}: FTC anchor {anchor!r} missing"
    assert i_pre < i_anchor, (
        f"{rel}: pre-binding {pre_bind!r} must appear BEFORE FTC block "
        f"{anchor!r} (scope trap regression)"
    )


# ── Behavioral lock: group_consensus DEGRADE fallback ───────────────────────

_WEIGHTER_PATH = "core.brains.services.dynamic_brain_weighter.DynamicBrainWeighter"
_ALL_GROUPS_PATH = "core.parliament.contract_groups.compute_all_group_signals"
_RESOLVE_PATH = "core.execution.capital_allocator.resolve_conflicts"
_VOLUME_PATH = "core.execution.capital_allocator.compute_volume"


def _mock_weighter_class() -> MagicMock:
    """DynamicBrainWeighter mock safe to instantiate + call."""
    mock = MagicMock()
    mock_instance = MagicMock()
    mock_instance.get_weights.return_value = {}
    mock.return_value = mock_instance
    return mock


def _make_raisy_correlation_tracker() -> MagicMock:
    """correlation_tracker whose get_correlation_penalty raises (DEGRADE path)."""
    tracker = MagicMock()
    tracker.get_correlation_penalty.side_effect = RuntimeError("MT5 IPC offline")
    return tracker


def test_group_consensus_penalty_degrades_to_raw_volume():
    """get_correlation_penalty 抛 RuntimeError → 不崩 + dynamic_volume == raw_volume."""
    mock_gs = SimpleNamespace(
        direction="long",
        confidence=0.7,
        total_count=1,
        brain_ids=["B1"],
    )
    corr = _make_raisy_correlation_tracker()

    with (
        patch(_WEIGHTER_PATH, _mock_weighter_class()),
        patch(_ALL_GROUPS_PATH, return_value={"barrier_12bar": mock_gs}),
        patch(
            _RESOLVE_PATH,
            return_value=SimpleNamespace(
                should_trade=True,
                direction="long",
                confidence=0.7,
                agreement_level="majority",
                active_groups=["barrier_12bar"],
                dissenting_groups=[],
                reason="",
            ),
        ),
        patch(_VOLUME_PATH, lambda base_volume, decision, regime, vol_atr: base_volume),
    ):
        result = compute_contract_group_consensus(
            raw_proposals=[
                SimpleNamespace(
                    direction="long",
                    confidence=0.8,
                    brain_id="B1",
                    contract_group="barrier_12bar",
                    vote_weight=1.0,
                    dynamic_scale=1.0,
                    fallback=False,
                )
            ],
            brains=[{"brain_id": "B1", "contract_group": "barrier_12bar"}],
            tracker=SimpleNamespace(),
            pnl_ledger=SimpleNamespace(),
            correlation_tracker=corr,
            base_volume=0.05,
            current_atr=4.5,
        )

    # 异常路径确实被执行 (非跳过) — 证明 DEGRADE 吞异常 + 预绑定回落生效
    corr.get_correlation_penalty.assert_called_once()
    assert result["direction"] == "long"
    # 无惩罚回落: dynamic_volume == raw_volume (compute_volume mock 返回 base_volume)
    assert result["dynamic_volume"] == 0.05


# ── Static locks: 4 处修复点预绑定位置 ─────────────────────────────────────


def test_live_cycle_positions_prebound_before_degrades():
    """startup_reconciliation: _positions 预绑定必须先于 positions_get DEGRADE 块."""
    _assert_pre_bind_before(
        "core/runtime/live_cycle.py",
        "_positions: list[Any] | None = None",
        "MT5_IPC:positions_get:startup_reconciliation",
    )


def test_live_cycle_equity_prebound_before_degrades():
    """PnL_to_equity: _eq 预绑定 0.0 必须先于 account_info DEGRADE 块."""
    _assert_pre_bind_before(
        "core/runtime/live_cycle.py",
        "_eq: float = 0.0",
        "MT5_IPC:account_info:PnL_to_equity",
    )


def test_intent_loop_event_stream_mode_prebound_at_loop_top():
    """live_intent_loop: _EVENT_STREAM_MODE 首次绑定必须先于其后的循环体首行."""
    src = _load("scripts/live_intent_loop.py")
    i_pre = src.find("_EVENT_STREAM_MODE = True")
    assert i_pre != -1, "live_intent_loop: _EVENT_STREAM_MODE pre-binding missing"
    # 取 i_pre 之后首个 last_heartbeat (L2311 循环体首行), 排除更早守护循环的同名行.
    i_after = src.find("state.last_heartbeat = time.time()", i_pre)
    assert i_after != -1, "live_intent_loop: loop-body last_heartbeat missing"
    assert i_pre < i_after, (
        "live_intent_loop: _EVENT_STREAM_MODE pre-binding must appear at loop "
        "top BEFORE loop body (scope trap regression)"
    )


def test_group_consensus_dynamic_volume_prebound_before_degrades():
    """CorrelationTracker:penalty: 首次 dynamic_volume=raw_volume 必须先于 DEGRADE 块."""
    _assert_pre_bind_before(
        "core/parliament/group_consensus.py",
        "dynamic_volume = raw_volume",
        "CorrelationTracker:penalty",
    )
