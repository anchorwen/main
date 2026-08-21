# 📋 Phase 3 路线图 (The Data Blueprint) — 数据与特征管线世代

> **依据**: IC 战略裁决 2026-08-21 (投委会): 主战场从「交易引擎与底层架构 (Execution & Architecture)」正式移交「数据与特征管线 (Data Engineering & ML Pipeline)」。Phase 2 (ROADMAP_20260819_POST_CLEARANCE.md, P1-P9) 已全绿收口, 本路线图为 **Phase 3 开放窗口** 唯一纲领。
> **状态**: 🟢 **定稿** — IC 雷霆裁决 2026-08-21 批准, 本文件为 **Phase 3 唯一纲领**。
> **地位**: 与 Phase 2 路线图并列存于 references/, 一世代一纲领。

---

## 0. 世代定调 — GIGO 第一定律

> 量化模型的第一定律是 **Garbage In, Garbage Out**。
> Phase 2 清偿的是「交易引擎的骨架」, Phase 3 守护的是「喂给引擎的血肉」。若特征工程 / 数据集构建 / 标签 / 校准产物任一处是坏的, 下游所有模型训练、特征筛选、OOS 验证都是建在沙滩上的废墟。

**战场转移声明**: 本世代唯一首优战役 (P1) 即 TECH_DEBT-020 —— 它揭示的不仅是「空 npz 崩一个检查器」, 而是 **训练就绪评估链路从未真正验证过 XAU 数据集** (详见 P1 根因修正)。

---

## 1. 执行纪律 (承继 Phase 2 + 新增)

| # | 纪律 | 说明 |
|:--|:--|:--|
| 1 | **同模块聚批** | 同一文件域一次改动到位 (Iron Law Iterability) |
| 2 | **一债一闭环** | DQAF → 蓝图 → FIX 注册 → 回归锁 → 三步归档 |
| 3 | **零行为变化门禁** | 未触碰的路径逐分支零变化 (min_rr=0 式门禁复用) |
| 4 | **触发器纪律 (新增)** | Parked 项**前置条件未满足绝不强行拉起**。禁止为"凑单"提前触发未达标战役。条件满足 → 编入战术序列; 未满足 → 留 Parked, 文档记录实测值 |
| 5 | **脚本先行** | 每项开工前以 `_audit_` 脚本实测前置值, stdout 为唯一合法证据源 (Iron Law #11) |
| 6 | **核心路径带休市期回放** | 涉 live_cycle / intent_loop / watchdog 的改动回归锁含休市期场景 (本世代以特征/训练脚本域为主, 预计轻) |

---

## 2. 战场实况取证 (2026-08-21, 只读探测)

> 以下为草案定稿时点的实测值, 开工前需以脚本重采复核 (纪律 #5)。

| 战场项 | 前置条件 | 当前实测值 | 判定 |
|:--|:--|:--|:--|
| **TECH_DEBT-020** | 根因待查 | ✅ **根因已实证锁定** (P1 详述) | 🔴 **立即编入 P1** |
| **MetaExit 门禁** | 500+ ExitFeatureSnapshot | ✅ **12,512** (XAU 6,473 + BTC 6,039) | 🟢 **达标 → 升入 P2** |
| **XAU 实证 Micro Scaler** | XAU Feature Store ≥ 5,000 M5 | ✅ **43,580** (v9: 29,220) | 🟢 **达标 → 升入 P3** |
| **V6 schema 剪枝 37→31** | 影子 ≥7 天 + Phase D 后 | 🔴 影子 0 产出 (v6_shadow_exits 无记录) | ⚪ **留 Parked** (未到 Phase D) |
| **DQAF-057 3b** | Event Sourcing Phase 2 批准 / 记账完整性事故 | 未触发 (无事故, 无批准) | ⚪ **留 Parked** |
| **DQAF-057 3c** | label_coverage_pct <90% 连续 3 天 | ⏳ **官方口径待核** (PENDING 项, 见 §6) | ⏳ **待核验** |
| **P2 进场点差摩擦** | 下次触碰门禁链时 | 未触碰 | ⚪ **留 Parked** |
| **TECH_DEBT-002** | journal > 10,000 行 | data/ = 8,648 (86.5%), data_btc/ = 3,501 | ⚪ **留 Parked + 监控** (近临界) |
| **TECH_DEBT-001/003** | 新增调用方未传 symbol / MIA 大重构 | 未触发 | ⚪ **留 Parked** |
| **bars_held / breakeven / brain_pnl_ledger 退役** | 触发条件式 | 未触发 | ⚪ **留 Parked** |
| **T19 rr_below_minimum / T22 V4 校准** | MONITORING | MONITORING (30/50, 未达标不动作) | 🟡 **监控** |

---

## 3. 战术序列总览 (建议执行顺序)

| 序 | 项 | 模块域 | 根因层级 | 规模 | 用户可感知影响 | 前置状态 |
|:--|:--|:--|:--|:--|:--|:--|
| **P1** | TECH_DEBT-020 (The Vanguard) | scripts/data-pipeline | L3 配置 + L1 读取容错 | S-M | **每 XAU daily_ops 确定性 EOFError 噪音** → 消除; **训练就绪评估首次真实生效** | ✅ 根因已锁 |
| **P2** | MetaExit 门禁复查 + 重训 | brains/exit | TBD (开工取证) | M | 出场模型质量门禁诚实化 | ✅ 12,512 快照 |
| **P3** | XAU 实证 Micro Scaler | sizing/calibrate | TBD (开工取证) | M | XAU 手数校准实证化 | ✅ 43,580 记录 |
| **PENDING** | DQAF-057 3c label 覆盖率核验 | scripts/audit | — | S | 若 <90% → 触发 MT5 回填 | ⏳ 官方口径待采 |

> **P2/P3 依赖关系**: P1 (数据管线地基) 先行 — 数据构建/读取链路不诚实则 P2/P3 的「数据 → 模型」结论同样失信。P1 收口后 P2、P3 可并行立项 (不同文件域)。

---

## 4. P1 — TECH_DEBT-020 三位一体修复 (The Vanguard) 🔴 首优

> **状态**: ✅ **已清偿 (FIX-20260821-006, 2026-08-21)** — IC 雷霆裁决「三位一体防御」全量落地: ① 契约修正 ② Builder 熔断 ③ Reader 容错。回归锁 7 + 全量 verify --full 通过。实盘复现: EOFError 消除, XAU 训练就绪评估首次真实生效 (builder 实跑 1046 样本/40 维), 残余 FAIL = asof_join_rate 22.3% + pnl_null 14.4% 诚实暴露。注册: TECH_DEBT_REGISTRY 020 ✅ CLOSED / FIX_REGISTRY / ReB `EMPTY_NPZ_EOF_READINESS_HARNESS`。

### 4.1 定性 (IC 裁决承继)

> 量化模型的第一定律是 Garbage In, Garbage Out (GIGO)。若特征工程写入侧在生产空/损坏的 stage-3 npz, 下游所有模型训练、特征筛选、OOS 验证都建立在沙滩上的废墟。

### 4.2 根因修正 (2026-08-21 实证, 推翻注册表推测)

注册表原推测: 「stage-3 数据集构建器在 XAU 侧某环节产出空/损坏 npz — **数据管道写入侧失效**」。

**实证结果 (复现 + 源码链闭合)**: **写入侧健康, 是「契约配置缺口 × validator 空文件模式 × 读取侧零容错」三重叠加**。

```
check_training_readiness.py:674  NamedTemporaryFile(suffix=".npz") 预建空文件
        │  (:659-661 builder_args 为空 → fallback ["--data-dir", data_dir], 未传 --symbol)
        ▼
build_btc_metafilter_v2_dataset.py:458  symbol = args.symbol   # 默认 "BTCUSDc"
        │  (:463 load_feature_store(data, "BTCUSDc") → data/feature_store/records/symbol=BTCUSDc/ 不存在)
        ▼
main() :464-465  if not features: return   # 静默早退, rc=0, 不写输出文件
        ▼
check_training_readiness.py:722  np.load(空文件) → EOFError: No data left in file
```

**证据链 (每步有实测/源码锚点)**:
- `training_pipeline_xau_metafilter_v1.json` `stage_3_dataset_builder` **无 `builder_args` 字段** — 仅 description 文本记载正确调用 (`--symbol XAUUSDc --spread-cost-usd 0.0`), 结构化字段缺失。
- `training_pipeline_btc_metafilter_v3.json` **同样无 `builder_args`**, 但 BTC 运行时 data_dir=data_btc 含 `symbol=BTCUSDc` → 默认 symbol 恰好命中 → BTC 不炸。**对照组成立**。
- `training_pipeline_xau_swing_v3.json` **含完整 `builder_args`** (`--symbol xauusdc --output-dir ...`) → 走 swing 专用 builder, 不受影响。**对照组成立**。
- 复现: `python scripts/check_training_readiness.py --contract configs/contracts/training_pipeline_xau_metafilter_v1.json --data-dir data` → **EOFError 确定性复现** (L722)。
- 特征存储健康: `data/feature_store/records/symbol=XAUUSDc/timeframe=M5/features.jsonl` = 43,580 行, v9_institutional_40 = 29,220。**写入侧无污染**。

**结论**: 修正后的根因是 **L3 配置缺陷 (契约缺失 builder_args → builder 以错误 symbol 空转) + L1 读取容错缺失 (validator 对空文件零防护)**。修复层级 ≥ 根因层级。

### 4.3 双腿修复方案

**左腿 — 写入侧/契约侧 (根因修复)**:
1. `training_pipeline_xau_metafilter_v1.json` `stage_3_dataset_builder` 补结构化字段:
   ```json
   "builder_script": "scripts/build_btc_metafilter_v2_dataset.py",
   "builder_args": ["--data-dir", "data", "--symbol", "XAUUSDc", "--spread-cost-usd", "0.0"],
   "builder_output_arg": "--output"
   ```
2. 修复后 builder 以正确 symbol 运行 → 真实产出 XAU 数据集 → **训练就绪评估首次对 XAU 真实生效** (回归锁断言: 修复后 stage_3 返回真实维度 PASS 而非 EOFError, 或如实报告数据缺口 — 不再伪装成功)。
3. 全契约族检漏: 扫描 `configs/contracts/training_pipeline_*.json`, 凡 `stage_3_dataset_builder` 有 `builder_script` 默认命中但缺 `builder_args` 的, 一律显式声明 (防同类空转)。

**右腿 — 读取侧容错 (防御)**:
1. `check_training_readiness.py:722` `np.load` 包 `(EOFError, ValueError)` 捕获 → 返回 `verdict="degraded"` + 明确诊断 ("builder 未产出有效 NPZ / 文件为空或损坏"), **而非裸 traceback 污染 stderr**。
2. builder 早退路径补非零退出码或结构化失败信号: `if not trades / not features / len(X)==0` → 打印明确错误 + `sys.exit(1)`, 使 validator 能区分「builder 未运行」与「builder 正常产出数据集」。
3. validator 预建临时文件时机后移: 待 builder 成功退出后再创建输出路径, 结构性消除「空文件先于 builder 存在」的模式。

### 4.4 回归锁 & 验收

- 新增: `tests/scripts/test_training_readiness_xau_metafilter.py` — 契约含 builder_args → 走 XAU 分支; 空文件 → `degraded` 非异常; builder 早退 → 非零退出信号。
- 对照组回归: BTC v3 (默认 symbol 命中) + swing_v3 (已有 args) 行为零变化。
- **验收断言**: 修复后 `check_training_readiness --contract ...xau_metafilter_v1 --data-dir data` **不抛 EOFError**; 实跑 builder 能产出有效 npz 或如实报告数据缺口 (min_matched_samples=500 未达 → 如实 FAIL, 不再伪 PASS)。

### 4.5 三步归档 ✅ (2026-08-21)

- DQAF-20260821-020 (Sev 3) → **FIX-20260821-006** → TECH_DEBT_REGISTRY 020 ✅ CLOSED → FIX_REGISTRY 行 + training_pipeline.md Fix History → ReB `EMPTY_NPZ_EOF_READINESS_HARNESS` (新入 ReB_PATTERN_INDEX)。

---

## 5. P2 — MetaExit 门禁复查 + 重训 (The Calibrated Exit) 🟢 前置达标

### 5.1 前置核验 (触发器纪律)

- 阈值: 500+ ExitFeatureSnapshot (deferred_metaexit_reenable)。
- **实测: 12,512 快照 (XAU 6,473 + BTC 6,039)** — 超阈值 25×。✅ **触发条件满足**。

### 5.2 战役内容 (开工取证后细化)

1. `data/meta_exit_snapshots.jsonl` + `data_btc/meta_exit_snapshots.jsonl` 审计 (脚本先行): 去重 position_ticket、feature 完备率、label 覆盖率。
2. 统一特征重训 (门禁: ≥15 wins AND ≥20% WR; 上次 2026-06-28: 32 paired / 7 wins / 21.88%)。
3. 门禁通过 → 切换 shadow→live 评估; 未过 → 如实记录缺口, 不硬凑。

### 5.3 关联

`[[todo_metaexit_reenable_after_retrain]]` / `[[todo_metaexit_calibrator_coldstart]]` (Calibrator HOT ✅ 500, TODO-1 待复查)。

---

## 6. P3 — XAU 实证 Micro Scaler (The Empirical Sizing) 🟢 前置达标

### 6.1 前置核验

- 阈值: XAU Feature Store ≥ 5,000 M5 (deferred_xau_empirical_scaler)。
- **实测: 43,580 记录 (v9: 29,220)** — 超阈值 8.7×。✅ **触发条件满足**。

### 6.2 战役内容 (开工取证后细化)

1. 读取 XAU feature store 构建实证分布 (脚本先行, stdout 唯一证据源)。
2. 设计 Micro Scaler 校准曲线 + 与现有 `GLOBAL_LOT_SCALE` 旋钮的关系 (勿改 BASE_LOT)。
3. 门禁: 样本量 / 稳定性达标 → 实证 scaler 接入; 否则记录缺口留 Parked。

---

## 7. PENDING — DQAF-057 Phase 3c label 覆盖率核验 (The Coverage Verdict)

- **触发条件**: `label_coverage_pct` < 90% 连续 3 天 (XAU 或 BTC), 官方口径 = daily_ops label_builder `_coverage_pct` (open tickets ∩ labeled / open tickets)。
- **采集命令**: `python scripts/daily_ops.py --base-dir data --dry-run --skip-shadow --skip-feedback --skip-paper` 输出 `label_coverage_pct`。
- **2026-08-21 实测**: 原始 close_price+pnl 口径 XAU 72.4% / BTC 71.0% (**非官方口径, 仅供参考**) — 官方口径因 barrier 模拟耗时未采到, 标记 PENDING。
- **判定**: 开工时先采官方值。若 <90% 连续 3 天 → 触发 `backfill_journal_pnl.py` (Strategy B, MT5 deal history 回填); 否则留 Parked。

---

## 8. Parked 复核表 (触发器纪律落地)

| Parked 项 | 触发条件 | 2026-08-21 实测 | 判定 |
|:--|:--|:--|:--|
| V6 schema 剪枝 37→31 | 影子 ≥7 天 + Phase D 后 | 影子 0 产出 | ⚪ 留 Parked |
| DQAF-057 3b Journal Strangler | Event Sourcing Phase 2 批准 / 记账事故 | 未触发 | ⚪ 留 Parked |
| DQAF-057 3c close_price 回填 | label_coverage <90% × 3 天 | ⏳ 官方口径待核 (§7) | ⏳ PENDING |
| P2 进场点差摩擦 | 下次触碰门禁链时 | 未触碰 | ⚪ 留 Parked |
| TECH_DEBT-001 MIA 幽灵默认值 | 新增调用方未传 symbol | 未触发 | ⚪ 留 Parked |
| TECH_DEBT-002 Journal O(N) | journal > 10,000 行 | data/ 8,648 (86.5%) | ⚪ 留 Parked + 🟡 监控 |
| TECH_DEBT-003 三层去重 | MIA 大版本重构 | 未触发 | ⚪ 留 Parked |
| bars_held 重启连续性 | 动 hesitation/重启重建路径 | 未触发 | ⚪ 留 Parked |
| breakeven 意图锁→成交锁 | 触发条件式 | 未触发 | ⚪ 留 Parked |
| 退役 brain_pnl_ledger.json | 事件流稳定后 | 未触发 | ⚪ 留 Parked |
| XAU 实证 Micro Scaler | ≥5,000 M5 | **43,580 ✅** | 🟢 **升入 P3** |
| T19 rr_below_minimum | MONITORING | 30/50, rr=0 | 🟡 监控 |
| T22 V4 confidence 校准 | MONITORING | MONITORING | 🟡 监控 |

---

## 9. 监控门槛 (新增值守项)

- 🟡 **TECH_DEBT-002 行数逼近**: `data/live_trade_journal.jsonl` 8,648/10,000 (86.5%) — 达 95% 预警, 达阈值升入战术序列。
- 🟡 **MetaExit wins/WR**: 重训前复查胜数/WR 门禁。
- 🟡 **label_coverage 官方口径**: 每周以 daily_ops 采集。

---

## 10. 纪律约束 (承继 Phase 2)

- **每项开工前**: DQAF 握手 (P1 预计 Sev 3) → IC 批准 → 蓝图 + FIX_REGISTRY 检索 → verify --full 全绿 → FIX 注册 → 三步归档。
- **禁止**: 跨项并行触碰同一文件域; 一债未收口不并开下一债。
- **触发器纪律**: Parked 项前置未满足, 绝不强行拉起; 文档记录实测值。
- **8/19 已终审项** (Flow46 双塔 OOS / SELL 冻结 / XAU swing 冻结) 不得借本路线图回卷。
- 涉 E 盘实盘 (Dual_Assassin) 与 F 盘工作区事项不在本路线图范围。

---

**预估节奏**: P1 (S-M, 数据管线地基, 半日级) → P2/P3 (并行立项, M, 各半日-日级) → PENDING 核验随 P1 同步采数。

**定稿状态**: 2026-08-21 IC 雷霆裁决正式批准定稿 — 本文件为 Phase 3 唯一纲领。memory 已落索引 (dqaf_20260821_phase3_data_ml_roadmap.md)。P1 已开火, FIX ID 见 [TECH_DEBT_REGISTRY.md](../blueprints/system/TECH_DEBT_REGISTRY.md) 020 条目。
