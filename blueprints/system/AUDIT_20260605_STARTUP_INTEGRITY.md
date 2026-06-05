# 启动链完整性审计报告 (2026-06-05)

## 结论：6 个状态缺口，最高风险为 SL 冷却 + 断路器状态丢失

## LiveCycleState 持久化覆盖

`LiveCycleState` 共 45 字段。**仅 ~10 个字段在重启后恢复**，其余归零或重建。

### 已持久化 ✅

| 字段 | 恢复方式 |
|------|---------|
| `consecutive_sl_hits` | bootstrap 重放 journal 近 7 天平仓 |
| `_reentry_states` | bootstrap 构建 ExitRecord 并 record_exit() |
| `position_manager` | `load_state()` 从 active_position.json v3 |
| `_pending_sl_records` | bootstrap 追加 SL 记录 |
| `_last_daily_ops_utc` | 从 daily_ops_state.json 加载 |
| `_cooldown_registry` | `execution_state.restore_execution_state()` |
| `_family_entry_tracker` | `execution_state.restore_execution_state()` |

### 重启丢失 ❌ — 6 个高风险缺口

| # | 字段 | 丢失后果 | 严重度 |
|---|------|---------|--------|
| 1 | `sl_streak_blocked_until` / `sl_streak_blocked_all_until` | SL 连胜冷却计时器归零，应被拦截的入场被放行 | 🔴 |
| 2 | `_consecutive_degraded_cycles` / `_circuit_breaker_tripped` | 断路器状态清除，应进入 management-only 的系统恢复正常交易 | 🔴 |
| 3 | `intraday_dd_kill` | 日内回撤击杀状态清除，DD-kill block 解除 | 🔴 |
| 4 | `_recent_atr_values` / `_recent_mid_prices` / `_recent_consensus_scores` | 滚动缓冲区冷启动，ATR/ER/P80 估计不准确 | 🟡 |
| 5 | `block_new_entries` | 硬阻断标志丢失 | 🟡 |
| 6 | `correlation_tracker` / `portfolio_risk_controller` | VaR/相关性历史丢失，风控从零开始 | 🟡 |

## Bootstrap 边缘情况

| 场景 | 行为 | 风险 |
|------|------|------|
| journal 缺失或为空 | 静默返回，无 reentry 状态 → 所有策略被视为"首次入场" | 🟡 |
| journal 损坏（JSONDecodeError） | bootstrap_restart_state → except Exception → return | 🟡 |
| governance 缺失 | 所有大脑未过滤运行，vote_weight=1.0 | 🟡 |
| governance 损坏 | json.JSONDecodeError → 捕获 → fail-open（不过滤） | 🟡 |
| SL streak block 使用 wall-clock `now` | 如果 SL 发生在崩溃前 15 分钟，重启后的 block 只持续 15 分钟（而非 30 分钟） | 🟡 |

## 合成 Bar 绕过暖启动

- BarSync 降级唤醒 → 使用合成 bar（从 M1 tick 构造），绕过 `_degraded` 标志
- 系统可在 MT5 形成真实 M5 bar 之前恢复交易
- `live_intent_loop.py:2017-2033` — `fetch_synthetic_bar()` 路径

## 建议修复优先级

1. **P0**: 将 `_cooldown_registry` 的 SL streak 冷却计时器纳入 `execution_state` 持久化（已覆盖 cooldown，确认 SL streak 是否在其中）
2. **P0**: 将 `_consecutive_degraded_cycles` 纳入持久化
3. **P1**: governance fail-open → fail-closed（缺失/损坏时拒绝所有大脑，需手动修复）
4. **P2**: 滚动缓冲区可选择性持久化（ATR 最关键）
