# 实盘全链路审计报告 — 2026-05-22

## 审计范围

28 个模块蓝图 + FIX_REGISTRY (2026 全量) + live_intent_loop.py + live_cycle.py + event_bar_sync.py

## 严重程度定义

| 级别 | 含义 |
|------|------|
| **CRITICAL** | 即刻导致错单/崩溃/资金损失 |
| **HIGH** | 边缘条件下静默降级/错误行为 |
| **MEDIUM** | 技术债/脆弱性/维护负担 |
| **LOW** | 表面问题/不太可能触发 |

---

# 一、CRITICAL — 立即修复

## 1.1 cycle_count 重复递增 (数据损坏)

**文件**: `scripts/live_intent_loop.py:1844, 4947`

`state.cycle_count` 在两个地方递增：
- `execute_live_cycle` 内成功 dispatch 后 (+1)
- 外层主循环每次 cycle 返回后 (+1)

交易 cycle 被计两次。依赖 cycle_count 的下游逻辑（状态保存间隔、对账触发、冷却计时）全部偏移。

**建议**: 删除其中一处，保留外层统一递增。

## 1.2 cooldown 冷却在无交易时失效

**文件**: `core/runtime/live_cycle.py:3843`

`cooldown_blocks_fire` 比较 `time.monotonic()` 与 `state.last_fire`，但 `last_fire` 仅在 **实际 dispatch 交易时** 更新。如果市场安静数小时，`last_fire` 仍是数小时前的时间戳，冷却检查立即放行 —— 300s 冷却形同虚设。

**建议**: 空 cycle 时也更新 `last_fire` 为当前时间，或将冷却检查改为 "距上次评估时间"。

## 1.3 后台预热线程与主循环并发访问 MT5

**文件**: `scripts/live_intent_loop.py:1791-1794`

守护线程 `_background_warm_start` 调用 `mt5.copy_rates_from_pos()` 和 `compute_all()`，与主循环的 MT5 调用并发。MT5 C 扩展在释放 GIL 时，两个线程可同时操作同一 terminal handle，产生未定义行为。

**建议**: 使用 `threading.Lock` 保护所有 MT5 调用，或等预热线程完全结束后再进入主循环。

## 1.4 管理阶段任一价格获取失败 → 整周期跳过

**文件**: `core/runtime/live_cycle.py:922-923`

`_execute_management_phase` 中单个 `price_fetch` 失败 → 返回 `False` → 整个管理阶段（trail、breakeven、exit check）跳过。MT5 IPC 在负载下间歇性故障时，仓位可能连续多个周期无管理。

**建议**: 单仓位价格获取失败应跳过该仓位，继续管理其余仓位。

---

# 二、HIGH — 应尽快修复

## 2.1 `except Exception: pass` 泛滥 (52+ 处)

跨三个文件计数到 52+ 个裸 `except Exception: pass`。每个单独看是防御性编程，但累积效果是：
- 系统通过多个故障模式静默降级
- 根因被吞没，生产问题几乎无法调试

**高风险位置**:
| 位置 | 影响 |
|------|------|
| `live_intent_loop.py:1265` 仓位状态恢复 | 状态损坏 → 静默回退到 MT5 恢复 |
| `live_intent_loop.py:1416` 未管理仓位审计 | 仓位可能无 trailing stop |
| `live_intent_loop.py:1200` MetaFilterGate 加载 | 静默运行无 gate |
| `live_intent_loop.py:1576` 配置热加载 | 热加载静默断裂，用过期配置继续 |
| `live_cycle.py:4381` 市场 regime gate | Regime 过滤静默禁用 |

**建议**: 每个 `except Exception: pass` 至少加 `emit_brain_alert` + 结构化日志事件。

## 2.2 degraded wakeup 返回过期数据被当新鲜数据用

**文件**: `core/protocol/event_bar_sync.py:159-169`

降级唤醒返回的 dict 中 `high/low/close` 全是上一根完整 K 线的收盘价（可能已过期 5+ 分钟），`volume=0`。调用方 `live_intent_loop.py:1889` 只检查 `_degraded` 标志并打日志，然后**用这份过期数据继续 cycle**。FeatureService 会检测到 stale cache 回退到 live compute，但 live compute 用的价格是当前实时价格 → 特征与价格时间错位。

**建议**: degraded wakeup 时跳过特征计算，直接进入管理阶段（trail/exit check 仍需要）。

## 2.3 journal 文件无锁并发读写

**文件**: 多处

Journal `live_trade_journal.jsonl` 读取用 `Path.read_text()`（无锁），写入用 `FileLock`（仅对账路径）。MT5 bridge 从独立进程写入。无共享锁协议 → 并发读写产生交错/截断行。

**建议**: 所有 journal 读写统一使用 `portalocker` 或 `fcntl.flock`。

## 2.4 状态文件无事务性检查点

**文件**: 全局

状态分散在 >=7 个文件中：`rolling_norm_state.json`, `regime_detector_state.json`, `brain_performance.json`, `brain_pnl_ledger.json`, `active_position.json`, `bar_sync_state.json`, `meta_filter_state.json`。任意时刻崩溃 → 这些文件处于不一致组合（如仓位在 journal view 中存在但 `active_position.json` 中不存在，或反之）。

**建议**: 引入原子性检查点机制（先写临时文件，最后 `os.replace` 批量提交）。

## 2.5 三个独立的大脑加载路径

**文件**: `live_intent_loop.py`, `service_container.py`, `BrainRegistryService`

大脑配置通过三条独立路径加载，`enabled` 标志至少被两条绕过。此前已导致僵尸大脑投票（FIX-20260521-002），但架构上仍未统一。

**建议**: 所有路径收敛到 `FeatureBrainRegistry.list_active_entries()` 单一入口。

## 2.6 `brain_types` 过滤仅在 barrier_12bar 生效

**文件**: `core/runtime/live_cycle.py:2703-2709`

`brain_type` 运行时过滤只对 `barrier_12bar` 加了。其他策略（statarb_dynamic 等）仍允许任意 brain_type 投票。若未来有新 brain 误配到其他策略，会重复 barrier_12bar 的历史问题。

**建议**: 将 brain_type 过滤抽取为通用逻辑，对所有 contract_group 生效。

## 2.7 DeepResMLP ONNX 模型永久损坏

**文件**: `data/models/deepresmlp_v2_new.onnx`

ONNX 模型使用外部数据格式，`.onnx.data` 文件已损坏/丢失。每次启动都报 `UnicodeDecodeError`，回退到确定性 stub 模式（输出固定中性值）。该 brain 已被 contract-mute（vote_weight=0），但每次仍尝试加载并失败。

**建议**: 重训或从备份恢复 ONNX 模型，或从 brain registry 中禁用。

## 2.8 SIGTERM 无处理 → 优雅关闭被跳过

**文件**: `scripts/live_intent_loop.py`

无 `signal.signal(SIGTERM, ...)`。`SIGTERM` 杀死进程时 `finally` 块不执行 → 状态不保存、分布式锁不释放。容器化/守护进程部署中每次优雅重启都导致状态损坏。

**建议**: 注册 SIGTERM handler 触发 `shutdown_flag.set()`。

---

# 三、MEDIUM — 计划修复

## 3.1 M5 时间周期硬编码蔓延

**文件**: 全局

- `event_bar_sync.py`: M5 硬编码
- `feature_service.py`: `store_timeframe="M5"` 硬编码
- `live_cycle.py`: trail 逻辑用 M5-bar cycle 标定
- `_DEFAULT_HORIZON = 12`（12 根 M5 = 1 小时）作为所有 brain 的回退值

切换到其他时间周期需修改每一层。

## 3.2 硬编码常量和魔法数字

| 常量 | 位置 | 问题 |
|------|------|------|
| `_DEFAULT_HORIZON = 12` | `live_cycle.py:72` | 对 micro brain（3 bar）过大，对 swing（D1）过小 |
| `float(pnl) / 1000.0` (PnL%) | `live_cycle.py:784` | 假设 $1,000 账户，实际若 $10,000 则偏差 10x |
| `DEFAULT_FALLBACK_INTERVAL = 60` | `event_bar_sync.py:47` | 不可按 symbol/session 配置 |
| `MAX_MT5_ERROR_RETRIES = 3` | `event_bar_sync.py:49` | 不可按环境调优 |
| `META_FILTER_GATE_THRESHOLD = 0.40` | `live_cycle.py:73` | 不可通过 YAML/CLI 覆盖 |

## 3.3 修复引入回归的模式

历史记录显示多次修复引入新 bug：
- FIX-20260519-017 引入 `UnboundLocalError` with `r_now`
- FIX-20260522-006 意外将快速失败变成持久阻塞
- FIX-20260519-016 过度修正使 OU brain 静默 16 小时
- FIX-20260515-010 错误删除活跃 brain 配置

根因：修复后只验证了修复点本身，未验证下游级联影响。

## 3.4 cross-module 管道数据丢失

多处存在 "数据存在于某层但未传递到消费层"：
- `StrategyDecision.confidence` 沿 4 段链丢失
- `brain_status_map` 从未传给 `record_brain_votes()`
- `live.yaml` 的 `live_trading.risk_budget_usd` 读入但从未使用
- `portfolio_netting_mode` 从未连接到 `PortfolioRiskController`

## 3.5 两个已记录的循环依赖

| 循环 | 风险 |
|------|------|
| `execution ↔ runtime` (strategy_line ↔ shadow_recorder) | 低 — shadow_recorder 仅写入 |
| `execution ↔ deployment` (live_order_sender ↔ service_container) | 中 — DI 容器接口稳定但脆弱 |

## 3.6 meta_exit 模型质量不足

**日志确认**: `n_wins=7, win_rate=0.1186, min_wins=15, min_win_rate=0.2` → 每次启动都被拒绝，回退到 `atr_trailing_stop_layer1`。系统完全依赖单层 ATR trailing 出场。

## 3.7 4 个 swing 策略无活跃 brain

`daily_swing`, `m15_swing`, `m30_swing`, `h1_swing` 在 live.yaml 中 enabled:true，但所有对应的 XGBoost swing brain 都 disabled。这些策略占用评估时间但从不产生信号。

---

# 四、已完成（本次审计期间）

本次审计前/中已修复的问题：

| 问题 | FIX ID | 状态 |
|------|--------|------|
| 符号反转 Bug — 5 个 adapter `_score_to_direction()` | FIX-20260522-013 | ✅ |
| counter-trend 豁免 barrier_12bar | FIX-20260522-013 | ✅ |
| BarSyncPoller 120s→360s 超时 | FIX-20260522-010 | ✅ |
| BarSyncPoller 降级唤醒 | FIX-20260522-011 | ✅ |
| MetaFilterGate 阈值 0.60→0.40 | (上轮) | ✅ |
| barrier_12bar 从 Track 3 移除 (外科手术隔离) | (上轮) | ✅ |
| Dictator Protocol (Huber 独裁) | (上轮) | ✅ |

---

# 五、修复优先级路线图

**立即 (本周)**:
1. ~~cycle_count 重复递增~~ ✅ FIX-20260522-014 (CRITICAL-3)
2. ~~cooldown 无交易时失效~~ ❌ 用户确认正确设计（空 cycle 无交易时不应更新 last_fire，冷却只防 burst trading）
3. ~~管理阶段单点失败 → 整周期跳过~~ ✅ FIX-20260522-014 (CRITICAL-1)
4. ~~后台预热线程 MT5 并发~~ ✅ FIX-20260522-014 (CRITICAL-2)

**紧急 (下周)**:
5. ~~`except: pass` → 加告警日志 (3 个关键路径)~~ ✅ FIX-20260522-014 (HIGH-5)
6. ~~degraded wakeup 过期数据使用~~ ✅ FIX-20260522-014 (HIGH-6)
7. journal 文件锁 — 待实施
8. ~~状态文件事务性检查点~~ ✅ FIX-20260522-014 (HIGH-8)

**计划 (下月)**:
9. 三条大脑加载路径统一
10. brain_type 过滤通用化
11. ONNX 模型修复/禁用
12. SIGTERM handler
13. 硬编码常量 → 可配置
14. meta_exit 模型重训
