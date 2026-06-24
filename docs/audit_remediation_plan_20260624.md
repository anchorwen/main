# 🏛️ 全链数据审计修复方案 (Institutional Audit Remediation Plan)

**Docket ID**: DQAF-20260624-AUDIT-REMEDIATION
**签发日期**: 2026-06-24
**签发依据**: Iron Law Ω — 全链审计 (11 scripts, Scene D → #11)
**架构师审核**: APPROVED WITH COMMENDATION — "Masterful Diagnostic"
**总模块数**: 4 (可并行在独立 VSCode 窗口中执行)

---

## 架构事实修正 (Architecture Reality Check)

> ⚠️ **架构师诊断修正**: "两个品种的 live 进程都死了" — 此诊断**不准确**。

**实际情况** (经 `live_launcher.py` 进程树验证):

| 进程 | PID | 状态 |
|------|-----|------|
| main.py live (hub) | 24824 | ✅ 运行中 |
| live_launcher.py (XAU) | 6048 | ✅ 运行中 |
| live_launcher.py (BTC) | 22400 | ✅ 运行中 |
| mt5_bridge_worker.py (XAU) | 23980 | ✅ connected, 644 msgs |
| mt5_bridge_worker.py (BTC) | 1932 | ✅ connected, 263 msgs |
| live_intent_loop.py (XAU) | 21852 | ✅ lock refreshed 127s ago |
| live_intent_loop.py (BTC) | 25972 | ✅ lock refreshed 131s ago |
| watchdog_daily_ops.py (XAU) | 14892 | ✅ running |
| watchdog_daily_ops.py (BTC) | 19364 | ✅ running |

**修正后的根本阻塞点诊断**:
- **BTC 无交易**: 不是因为进程死亡，而是因为 `negative_ev_low_rr` 正确拒绝了所有信号（p_win ≈ 0.577 vs breakeven=0.602）。这是**业务逻辑正确防守**。
- **XAU 排行榜过期 2684min**: daily_ops 上次运行 12.9h 前（在 UTC 22:00 主窗口执行），watchdog 每 6h 检查一次，24h 后才自动触发。排行榜 TTL=240min，过期是正常的 `between-window staleness`，不是进程死亡。
- **vote_weight=0**: DQAF-072/074 代码修复已完成，但 BTC 唯一 live brain (V12_H1_Survival) 只有 56 笔交易 — 在 governance 中其 vote_weight 被正确设置为 0 因为样本量不足。这是**设计预期的防守行为**。

**真正的问题不是心脏骤停，而是:**
1. 🔴 `strategy_line.py` 3 行代码将全部大脑（包括异议者）错误归因 → 污染所有下游 PnL 数据
2. 🔴 MetaFilter 代码修复后未热加载到运行中进程
3. 🟡 BTC 市场结构不支持传统高 R:R 策略 → 需要生存策略但交易量不足以晋升

---

## 模块总览

| Module | Scene | 优先级 | 修改文件数 | 预计时间 | 可并行 |
|--------|-------|:---:|:---:|:---:|:---:|
| **M1**: 污染根因修复 | B | **P0** | 1 文件, 3 行 | 30min | 独立 |
| **M2**: Journal Schema 修复 | A | **P1** | 1-2 文件 | 45min | 独立 |
| **M3**: MetaFilter + 进程验证 | D | **P1** | 0 文件 (诊断) | 30min | 独立 |
| **M4**: 战略性搁置登记 | E | **P2** | 3-4 文件 (.md) | 30min | 最后执行 |

---

---

# Module 1 (P0): 污染根因修复 — `strategy_line.py` 3 行修正

## [Ω-Routing: Scene B → #0 → #6 → #5]

---

## 1.1 问题描述

`verify_pnl_data_integrity.py` 审计检测到 FIX-20260527-002 污染复发: 多个不同大脑被分配了完全相同的交易记录。

**根因 (3 个 Agent 并行研究确认)**:

在 `core/execution/strategy_line.py` 的共识计算中，`ContractGroupConsensus` 对象**同时持有**两个字段:
- `brain_ids` — 合约组中**全部**参与大脑 (包括投票方向与获胜方向相反的大脑)
- `supporting_brains` — 仅投票方向与获胜方向**一致**的大脑

但 `_compute_consensus()` 和 `_compute_weighted_fallback()` 两处返回时，都错误地使用了 `brain_ids`（全部大脑），而非 `supporting_brains`（支持获胜方向的大脑）。

**后果链**:
```
strategy_line.py:1569 signal.brain_ids (ALL brains)
  → position_registration.py:242 存储到 known_open_tickets
  → reconciliation.py:277 复制到 close_entry
  → reconciliation.py:319-350 为每个 brain_id 创建 SignalSettled 事件 (含异议者!)
  → feedback_loop.py:267-275 为每个 brain_id 创建 BrainPerformanceTracker (含异议者!)
  → 异议大脑获得正收益/负收益的错误归因
  → 所有下游 PnL 评估 + 贝叶斯资金分配被污染
```

**为什么原始 FIX-20260527-002 失效**: 原始修复只改了 `feedback_loop.py` 的 `ingest_journal_to_tracker()` ("从 open_entry 取 brain_ids 而非按时间搜索")，但从未触及上游 `strategy_line.py` 的源头 — `brain_ids` 字段本身就包含了错误的大脑集合。原始修复是**缩小爆炸半径**（从跨策略 → 同策略组内），但从未**消除爆炸**。

---

## 1.2 [PRE-EDIT CHECKLIST — Iron Law #0]

| # | 检查项 | 状态 |
|---|--------|------|
| 1 | 修改动机来自系统行为观察? | ✅ DQAF-20260624-AUDIT via verify_pnl_data_integrity.py + 3 Agent 并行研究 |
| 2 | DQAF 报告已输出? | ✅ 全链审计报告 (2026-06-24) |
| 3 | IC 已 Approved? | ⏳ 待审核批准 |
| 4 | 蓝图已查阅? | ✅ FIX_REGISTRY.md (FIX-20260527-002, FIX-20260621-030), strategy_line blueprint |
| 5 | FIX_REGISTRY 已检索? | ✅ 无冲突; 本次为 FIX-20260527-002 的补丁修复 (L1 → 应为 L2) |

---

## 1.3 具体修改

**文件**: `d:\future\core\execution\strategy_line.py`

### 修改 1: Line 1569 — 主共识路径

```python
# ❌ 修改前:
return (
    direction,
    confidence,
    signal.brain_ids,        # ← ALL brains in group (including opponents)
    signal.supporting_count,
    signal.total_count,
)

# ✅ 修改后:
return (
    direction,
    confidence,
    signal.supporting_brains,  # ← ONLY brains that voted FOR the winning direction
    signal.supporting_count,
    signal.total_count,
)
```

### 修改 2: Line 1628 — 加权回退: neutral 返回

```python
# ❌ 修改前:
return "neutral", 0.0, brain_ids, 0, len(proposals)

# ✅ 修改后:
return "neutral", 0.0, [], 0, len(proposals)
```

### 修改 3: Line 1648 — 加权回退: 有方向返回

```python
# ❌ 修改前:
return direction, round(float(confidence), 4), brain_ids, support_count, total

# ✅ 修改后:
return direction, round(float(confidence), 4), supporting, support_count, total
```

---

## 1.4 Iron Law #12: 根因分层

| 层级 | 判定 |
|------|------|
| **L1** | `brain_ids` vs `supporting_brains` 字段选择错误 (1 个字符之差) |
| **L2** | 原始 FIX-20260527-002 仅修复下游消费端 (feedback_loop.py)，未溯源上游生产端 (strategy_line.py) — **不彻底的修复模式** |
| **L3** | `ConsensusResult` 同时暴露 `brain_ids` 和 `supporting_brains` 两个语义相近的字段，下游调用者极易混淆 — **API 设计致错** |

**修复层级匹配**: L3 架构修复 (重命名歧义字段) 推迟 → `PATCH_NOT_ARCHITECTURE` + FIX_REGISTRY Deferred 表:
- 短期 (本次): L1 精确修复 (3 行)，立即止血
- 长期 (Deferred): 将 `brain_ids` 重命名为 `all_participating_brains`，`supporting_brains` 重命名为 `winning_brains`，从 API 设计层面消除歧义

---

## 1.5 Iron Law #5: 模式搜索

搜索 `signal.brain_ids` 和 `ConsensusResult` 的所有引用:
```
Grep pattern: "brain_ids|supporting_brains" in core/execution/strategy_line.py
Grep pattern: "brain_ids" in core/runtime/reconciliation.py
Grep pattern: "brain_ids" in core/runtime/position_registration.py
Grep pattern: "brain_ids" in scripts/feedback_loop.py
```

下游消费者 (reconciliation.py, signal_settlement.py, feedback_loop.py, position_close_adapter.py) **无需修改** — 它们从 open_entry 读取 brain_ids，上游修正后它们自动接收正确的 brain_ids 集合。

---

## 1.6 验证步骤

```bash
# 1. Ruff 检查
python -m ruff check core/execution/strategy_line.py

# 2. Mypy 类型检查
python -m mypy core/execution/strategy_line.py

# 3. 完整验证
python verify.py --full

# 4. 污染检测验证 (核心!)
python scripts/verify_pnl_data_integrity.py --base-dir data_btc
# 预期: "POTENTIAL CONTAMINATION" 段消失或大幅减少

# 5. 单元测试 (如有)
python -m pytest tests/ -k "strategy_line or consensus" -v
```

---

## 1.7 四维闸门 (Iron Law #1.1)

| 维度 | 评估 | 说明 |
|------|:---:|------|
| **Stability** | ↑ | 纯增量修改，不改执行流程，不改 I/O，只修正数据内容的正确性 |
| **Repairability** | ↑ | 减少了下游 PnL 污染的排查步骤，根因从 5 步链压缩为 1 步 |
| **Decoupling** | → | 修改局限在 strategy_line.py 模块边界内，下游接口向后兼容 |
| **Iterability** | → | 3 行修改集中在一处，新增 Deferred 架构项清晰登记 |

---

## 1.8 提交信息模板

```
fix(execution): FIX-20260624-XXX — Contamination Root Cause: brain_ids→supporting_brains

[Ω-Routing: Scene B → #0 → #6 → #5]

Root Cause Layer: L1 — field selection error at strategy_line.py:1569/1628/1648
L2 — FIX-20260527-002 only mitigated downstream, never fixed upstream source
L3 — ConsensusResult API ambiguity (brain_ids vs supporting_brains) — DEFERRED

FIX-20260527-002补丁: 3行修正将错误归因的异议大脑从P&L账本中移除。
ContractGroupConsensus.supporting_brains = 只含投票方向与获胜方向一致的大脑。
原代码使用 brain_ids (全部参与大脑，含异议者) → 导致异议大脑获得错误P&L归因。

Deferred: 重命名 brain_ids→all_participating_brains, supporting_brains→winning_brains

Stability: ↑ (pure correctness fix)
Repairability: ↑ (eliminates one root cause class for PnL contamination)
Decoupling: → (same module, backward-compatible)
Iterability: → (3 lines, single file)
```

---

---

# Module 2 (P1): Journal Schema 修复 — `eq_btc_swing_*` 缺少 symbol/side

## [Ω-Routing: Scene A → #9 → #8 → #12 → #6 → #5]

---

## 2.1 问题描述

`live_data_quality_report.py` 检测到 6 条无效 journal 条目 (来自 3 笔 BTC swing 交易的开仓+平仓):

```
[eq_btc_swing_207084c9ab70] missing required field: symbol
[eq_btc_swing_207084c9ab70] missing required field: side
[eq_btc_swing_be94c2c3fc0b] missing required field: symbol
[eq_btc_swing_be94c2c3fc0b] missing required field: side
[eq_btc_swing_1f0b269050e9] missing required field: symbol
[eq_btc_swing_1f0b269050e9] missing required field: side
```

**message_id 模式**: `eq_btc_swing_*` — 注意前缀是 `eq_` 而非 `eq_btc_swing_`？实际是 `eq_btc_swing_` 为完整的 strategy 名。

---

## 2.2 诊断步骤 (Iron Law #8 + #9)

### STOP → LOOKUP → DIG → MAP → PLAN

**STEP 1 — ECoL 证据锚定**:
运行诊断脚本获取完整上下文:

```bash
cd d:\future
python -c "
import json
with open('data_btc/live_trade_journal.jsonl') as f:
    for line in f:
        entry = json.loads(line)
        mid = entry.get('message_id','')
        if '207084c9ab70' in mid or 'be94c2c3fc0b' in mid or '1f0b269050e9' in mid:
            print(json.dumps({k: entry.get(k) for k in ['message_id','action','symbol','side','strategy','magic','position_ticket','recorded_at']}, indent=2))
"
```

**STEP 2 — 根因假设**:
- H1: Journal writer 在写入 `eq_btc_swing_*` 条目时，`symbol`/`side` 字段未从 context 中提取
- H2: 这些是 MIA (Missing In Action) close 条目，mia_detected 路径未填充 symbol/side
- H3: DQAF-059 的 strategy 回填只处理了 `strategy` 字段，未处理 `symbol`/`side`

**STEP 3 — 定位写入源**:
搜索 journal 中 `message_id` 前缀为 `eq_btc_swing` 的条目创建位置:
```
Grep: "eq_btc_swing" in core/runtime/*.py, scripts/*.py
Grep: "live_trade_journal" append/write in core/runtime/reconciliation.py
```

---

## 2.3 [PRE-EDIT CHECKLIST — Iron Law #0]

| # | 检查项 | 状态 |
|---|--------|------|
| 1 | 修改动机来自系统行为观察? | ✅ live_data_quality_report.py: 6 invalid entries |
| 2 | DQAF 报告已输出? | ⏳ 需先输出 DQAF_LITE_REPORT (Sev 3) |
| 3 | IC 已 Approved? | ⏳ 待审核批准 |
| 4 | 蓝图已查阅? | ⚠️ 需先读 reconciliation.py + journal writer |
| 5 | FIX_REGISTRY 已检索? | ⚠️ 需先检索 |

> ⚠️ **Module 2 执行前必须先完成诊断步骤 (Iron Law #9)**，输出 DQAF_LITE_REPORT 并获 IC Approved。上述 PRE-EDIT CHECKLIST 标注 ⚠️ 的项目由执行者在诊断阶段完成。

---

## 2.4 预期修改范围

基于 `eq_btc_swing` 前缀和 DQAF-059 的修复上下文，最可能的修改点:

1. **journal writer** (可能位置: reconciliation.py 或 position_close_adapter.py): 补齐 symbol/side 字段填充
2. **journal_validator.py**: 如修复范围包含 schema 防御性增强，在 validator 中添加 symbol/side 的 mandatory check + 具体错误信息

---

## 2.5 验证步骤

```bash
# 1. 修复后重跑数据质量报告
python scripts/live_data_quality_report.py --base-dir data_btc
# 预期: invalid 从 6 降为 0

# 2. Schema 验证
python scripts/validators/journal_validator.py --base-dir data_btc

# 3. 完整验证
python verify.py --full
```

---

---

# Module 3 (P1): MetaFilter 加载验证 + 进程健康确认 + XAU 排行榜刷新

## [Ω-Routing: Scene D → #11]

---

## 3.1 问题背景

审计报告显示:
```
MetaFilter state: model=, wr=None
XAU leaderboard: 2,684min stale (limit=120min)
BTC brain_output_health: BRAIN_SILENCE_LOW (0/1 brains producing output)
```

但架构研究揭示了关键事实:

### MetaFilter 真相

`meta_filter_state.json` **实际上在运行** — 包含 pred_history (100+ 预测), pred_buffer, atr_buffer。`model=` 字段为空不代表 filter 不工作，只是 state file 不记录 model_path。

代码修复状态:
- ✅ FIX-20260624-107: ImportError 已加入 except 元组
- ✅ DQAF-058-bis: joblib→JSON scaler 加载已修复
- ✅ 所有模型文件存在: `data_btc/models/meta_stage2_lightgbm_btc_v2.txt` (210KB), `.meta.json`, `btc_micro_scaler.json`

**关键问题**: 代码修复是否已被运行中进程热加载？`live_intent_loop.py` 在启动时调用 `meta_signal_filter.load()`，如果进程在 FIX-107 提交后未重启，则旧代码仍在使用（会因 ImportError 静默失败）。

### XAU 排行榜过期真相

- XAU daily_ops 上次运行: 2026-06-23 22:00 UTC (12.9h 前) — 在 UTC 22:00 主窗口执行
- Watchdog 配置: 每 6h 检查，24h 后才自动触发
- **这是正常的 between-window staleness**，下一次 22:00 UTC 会自动刷新

---

## 3.2 诊断命令（只读，不修改任何文件）

### Step 1: 进程健康确认
```bash
# 检查所有 Python 进程
wmic process where "name='python.exe'" get processid,commandline /format:csv

# 检查锁文件新鲜度
cat data_btc/locks/live_intent_loop.lock
cat data/locks/live_intent_loop.lock

# 检查 bridge 健康
python -c "import json; d=json.load(open('data_btc/reports/mt5_bridge_health.json')); print(f'BTC bridge: connected={d.get(\"mt5_connected\")}, heartbeat={d.get(\"last_heartbeat_utc\")}')"
python -c "import json; d=json.load(open('data/reports/mt5_bridge_health.json')); print(f'XAU bridge: connected={d.get(\"mt5_connected\")}, heartbeat={d.get(\"last_heartbeat_utc\")}')"
```

### Step 2: MetaFilter 功能验证
```bash
# 检查 MetaFilter state 文件中的实际预测值
python -c "
import json
with open('data_btc/meta_filter_state.json') as f:
    state = json.load(f)
print(f'pred_history entries: {len(state.get(\"pred_history\",[]))}')
print(f'pred_buffer entries: {len(state.get(\"pred_buffer\",[]))}')
print(f'atr_buffer entries: {len(state.get(\"atr_buffer\",[]))}')
print(f'atr_frozen: {state.get(\"atr_frozen\", False)}')
if state.get('pred_history'):
    recent = state['pred_history'][-5:]
    print(f'Recent predictions (timestamp, p_win): {recent}')
"

# 检查 live_intent_loop 日志中 MetaFilter 加载状态
grep -i "meta.*filter\|meta.*pipeline" data_btc/logs/*.log 2>/dev/null | tail -20
```

### Step 3: daily_ops 心跳验证
```bash
# XAU
python -c "import json; d=json.load(open('data/state/daily_ops_state.json')); print(f'XAU last daily_ops: {d.get(\"last_run_utc\")}')"
# BTC
python -c "import json; d=json.load(open('data_btc/state/daily_ops_state.json')); print(f'BTC last daily_ops: {d.get(\"last_run_utc\")}')"
```

### Step 4: 如果 MetaFilter 加载确实失败 — 热重载方案
```bash
# 方案 A: 如果进程需要重启以加载 FIX-107 代码
# (重启 launcher 会自动执行 cold-start daily_ops + orphan repair + stale lock cleaning)
# 注意: 先确认当前无活跃持仓!
python scripts/live_launcher.py configs/live_btc.yaml

# 方案 B: 如果只需触发 daily_ops 刷新排行榜
python scripts/daily_ops.py --base-dir data_btc
```

---

## 3.3 验证步骤

```bash
# 验证 MetaFilter 功能
python scripts/test_meta_pipeline.py --base-dir data_btc
# 预期: "MetaFilter loaded successfully" 而非 "FATAL"

# 验证排行榜新鲜度
python scripts/system_trust_report.py --data-dir data_btc
# 预期: XAU leaderboard 不再显示 STALE

# 验证大脑输出
python scripts/run_data_health.py --base-dir data_btc --symbol BTC --mode full
# 预期: brain_output_health 从 BRAIN_SILENCE_LOW 变为 OK
```

---

---

# Module 4 (P2): 战略性搁置登记 + 收口

## [Ω-Routing: Scene E → #6 → #0]

---

## 4.1 B 类问题搁置清单

以下问题经审计确认均为已知推迟/未完成待办，**不属于回归**。Module 4 负责将它们正式登记入册并设置触发条件。

### 4.1.1 Event Stream ↔ Old JSON 分歧

| 属性 | 值 |
|------|-----|
| **推迟文件** | `deferred_old_json_retirement.md` (已存在, 更新复查日期) |
| **当前状态** | verify_event_stream: 0 PASS, 12 FAIL |
| **触发条件** | verify_event_stream 连续 7 天无新 MISMATCH |
| **下一步复查** | 2026-07-01 |
| **操作** | 更新 memory file 的复查日期，不修改代码 |

### 4.1.2 Alpha 管线空转 (BTC)

| 属性 | 值 |
|------|-----|
| **推迟文件** | `deferred_btc_governance_0_live_brains.md` (已存在) |
| **当前状态** | BTC: 3 brains, 0 live |
| **触发条件** | BTC ≥200 笔实盘交易 (Path B) 或任意 brain 达到 50 笔 (晋升门槛) |
| **下一步复查** | 2026-07-01 |
| **操作** | 更新 memory file，不修改代码 |

### 4.1.3 Position Snapshots 39.1% 覆盖率

| 属性 | 值 |
|------|-----|
| **新推迟文件** | `deferred_snapshot_coverage_gap.md` (新建) |
| **当前状态** | BTC: 181/463 (39.1%), TP 字段缺失 |
| **触发条件** | 下次修改 position_registration.py 或 reconciliation.py 时顺带修复 |
| **下一步复查** | 2026-07-08 |
| **操作** | 创建 memory file，登记到 deferred 索引 |

### 4.1.4 Data Quality 4,157 issues

| 属性 | 值 |
|------|-----|
| **推迟文件** | `deferred_dqaf057_phase3b_journal_writer.md` + `deferred_dqaf057_phase3c_mt5_backfill.md` (已存在) |
| **当前状态** | DQAF-057 Phase 3b/3c 推迟 |
| **触发条件** | Phase 3b: 7/11 idempotency 复审; Phase 3c: label_coverage < 90% 持续 3 天 |
| **操作** | 确认触发条件未满足，不修改代码 |

### 4.1.5 XAU training_readiness.json 缺失

| 属性 | 值 |
|------|-----|
| **推迟文件** | REM-20260622-001 AF-1 (已存在) |
| **当前状态** | XAU 无匹配 training contract, conditional_existence 标志未实施 |
| **触发条件** | REM-20260622-001 Phase 3 实施时 |
| **操作** | 不修改代码 |

---

## 4.2 待创建的新 Memory Files

```bash
# Module 4 执行者需创建的 memory files:

# 1. 更新 deferred_old_json_retirement.md — 复查日 2026-07-01
# 2. 更新 deferred_btc_governance_0_live_brains.md — 复查日 2026-07-01
# 3. 新建 deferred_snapshot_coverage_gap.md
# 4. 新建 deferred_consensus_api_ambiguity.md (L3 架构修复: brain_ids 重命名)
```

---

## 4.3 FIX_REGISTRY 更新

Module 1+2 完成后，在 FIX_REGISTRY.md 中登记:

| Fix ID | Date | Module | Summary | Root Cause |
|--------|------|--------|---------|------------|
| FIX-20260624-XXX | 2026-06-24 | execution | **Contamination Root Cause Fix — brain_ids→supporting_brains**. FIX-20260527-002补丁: strategy_line.py:1569/1628/1648 使用 supporting_brains 替代 brain_ids。 | L1 — field selection error; L2 — FIX-027-002 only fixed downstream |
| FIX-20260624-YYY | 2026-06-24 | runtime | **Journal Schema Fix — symbol/side mandatory on eq_btc_swing entries**. [待 Module 2 诊断后填写] | [待定] |

---

## 4.4 收口检查清单 (Iron Law #7.1)

Module 4 执行者需确认:

```
[ ] 蓝图已更新: FIX_REGISTRY.md + 相关模块 blueprint Fix History
[ ] Memory files 已创建/更新: MEMORY.md 索引已刷新
[ ] Git: 所有 .py 修改已提交 (Scene B 产出), .md 修改已提交 (Scene E 产出)
[ ] Git: git status 确认无遗漏的未提交源文件
[ ] 本轮 commit 已 push
[ ] 四维闸门评估已写入 commit message
```

---

---

# 执行顺序与依赖关系

```
Module 1 (P0) ──── 无依赖，可最先执行 ────┐
Module 2 (P1) ──── 无依赖，可并行执行 ────┤
Module 3 (P1) ──── 无依赖，可并行执行 ────┤ 完成后 → Module 4 (P2) 收口
                                              │
                                              └── 依赖 M1+M2 的 FIX ID 和修改细节
```

**推荐执行顺序**:
1. **同时启动** Module 1 (污染修复)、Module 2 (Schema 诊断)、Module 3 (MetaFilter 验证) — 三个独立窗口
2. Module 1 先完成 (3 行修改最快) → 立即 run verify.py
3. Module 2 完成诊断 → 输出 DQAF_LITE_REPORT → 获批准 → 实施修复
4. Module 3 完成进程/MetaFilter 验证 → 输出诊断报告 → 如需重启进程则执行
5. 三个 Module 全部完成后 → 启动 Module 4 (收口+登记)

---

# Iron Law 合规声明

本方案遵循以下铁律:

| Iron Law | 适用场景 | 说明 |
|----------|----------|------|
| `#-1` | 全部 Module | 编写代码前必须加载 6 条机构级约束 |
| `#0` | M1, M2 | 编辑 .py 前强制安检 (PRE-EDIT CHECKLIST) |
| `#5` | M1, M2 | 修改前搜索同类模式 |
| `#6` | 全部 Module | 修改前查阅蓝图 + FIX_REGISTRY |
| `#7` | M1, M2 | 修改后注册 FIX ID |
| `#7.1` | M4 | 收口检查清单 |
| `#8` | M2 | 根因诊疗协议 (STOP→LOOKUP→DIG→MAP→PLAN) |
| `#9` | M2 | DQAF 双轨诊断 (ECoL + AR + CCT) |
| `#11` | M3 | 脚本先行，严禁口算 |
| `#12` | M1 | 根因分层 L1/L2/L3 + 架构修复层级匹配 |
| `#13` | M4 | 全自动收口协议 |

---

**文档版本**: v1.0
**状态**: ⏳ 待首席架构师审核批准
