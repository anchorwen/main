# DQAF/Audit 战报 — P3 XAU Micro Scaler 进攻 (战前弹药质检 + 火力规划)

- **Docket ID**: DQAF-AUDIT-20260823-P3
- **日期**: 2026-08-23 (脚本运行 UTC 2026-08-22 17:03Z)
- **触发**: IC 进攻指令 (2026-08-23) — "第一步数据质检 → 第二步火力规划 → 第三步等待开火授权。先让数据开口说话！"
- **证据源**: `scripts/_audit_xau_micro_scaler_20260823.py` (read-only, `_audit_` 前缀豁免) — **脚本 stdout 是唯一合法证据源 (Iron Law #11)。本报告所有统计数字均来自该脚本输出，零补算。**
- **复现**: `python scripts/_audit_xau_micro_scaler_20260823.py` (默认 `--data-dir data --symbol XAUUSDc`)

---

## 一、战前普查结论 (Data Quality Census — 数据开口说话了)

### 1.1 特征库盘点

| 指标 | 数值 | 判读 |
|---|---|---|
| raw lines | **43,740** | 与 IC 引用的 43,580 同量级 (差 = micro schema 行) |
| bad JSON | 0 | 文件完整性 ✅ |
| v9_institutional_40 | **29,051** (dedup 后全可用) | `len(values)>=40` 全过 |
| v4.3_microstructure_9 | 14,689 | 不在 v9 builder 合同内, 跳过 |
| 时域跨度 | 2025-11-06 → 2026-08-22 (6,948h) | 289 天 |
| 期望 M5 bars | 83,377 | 5min × 6,948h |
| **缺失 bar 缺口** | **54,326 (65%)** | 时间连续性差 → 序列模型致命, tabular 尚可 |

### 1.2 ⛔ 关键架构发现 — Schema 世代分叉 (最大数据债)

**同一文件 `features.jsonl` 混有两种不同列 schema 的特征向量，却共用一个 `schema_name: v9_institutional_40`。**

| 世代 | 行数 | 时域 | 含 `*_Price_ZScore` | 含 `*_Macro_Gold_Silver_Spread` |
|---|---|---|---|---|
| **current (新)** | **8,572** | 2026-05-24 → 08-22 (81 天) | ✅ | ❌ |
| **old (旧)** | 20,479 | 2025-11-06 → 2026-05-18 (168 天) | ❌ | ✅ |

- **每行都恰好 40/44 键** — 特征引擎在 ~2026-05-24 把 `Macro_Gold_Silver_Spread` 替换为 `Price_ZScore` (4 个时间框架各一列)。无 mixed 日, 世代切换清晰。
- builder 的 `len(values)>=40` 门槛对两世代**同时通过** → 若直接全量训练, 旧世代向量与当前实盘特征列**不对齐 = train/serve skew 硬伤**。
- **Vol_ZScore 零填充是时域伪象, 非随机数据病**:
  - current-gen: **0.1%** 零 (9/8,572)
  - old-gen: **100%** 零 (20,479/20,479)
  - → 全库表面 70.52% 零填充 = 旧世代引擎不产 volume 数据的伪象。新世代数据干净。
- **mean_missing_pct 9.09%/TF 的解释**: 4/44 键缺席 (即被替换的 4 列), 非随机缺失。**当前世代真 None/键缺失 = 0 条。**

### 1.3 Label 盘点 (journal 派生, 1,261 closed XAU trades)

| 指标 | 数值 |
|---|---|
| 二分类 (pnl>0) | win=491 / loss+be=770 → **WR 38.94%** |
| 类别不平衡 | **1.57:1** (mild) |
| 三分类 | win=491 / breakeven=137 / loss=633 |
| pnl | min=−140.9 / max=115.9 / mean=−0.48 / median=−0.01 |
| close-label 分布 | sl_hit_first=475, loss=266, win=187, tp_hit_first=149, breakeven=117, broker:signal_close=41, manual_close=24, sl_hit_trailed=2 |
| 策略 WR | barrier_12bar 40.1% / m30_swing 40.5% / m15_swing 42.9% / h1_swing 41.4% / statarb_dynamic 35.5% / structural_swing_v1 100%(n=7) |

### 1.4 可训练覆盖 (ASOF join, builder 语义: 900s + knowledge-time)

| 口径 | matched | 判读 |
|---|---|---|
| 全世代 ASOF | **1,045 / 29,051 (3.6%)** | 43,580 是特征行数, 非可训练标签数 |
| **current-gen (列对齐 live)** | **576** (win=236, **WR 41.0%**) | **唯一可上线服役的可训练集** |
| old-gen | 496 | 列不对齐 → 需 schema 桥接才有资格 |

---

## 二、核心诚实结论

> **IC 战报基数是 43,580 条"高质量记录" — 数据说话后真相是:**
> **43,740 raw → 29,051 v9 → 8,572 current-gen → 576 可标注训练样本。**
> 真正与当前实盘引擎列对齐、可获真实交易标签的训练样本是 **576 条**, 不是 43,580。
> 且其中 65% 的 M5 bar 时段整体缺失 → 时间连续性不足。

这条结论**改变 P3 的预期形状**: 不是"坐拥 43,580 条喂饱模型", 而是"576 条真实标注 + 8,572 行新世代特征, 需要纪律化的小样本训练"。

---

## 三、火力规划 (Training Blueprint)

### 决策 1 — 样本集: **Option A (推荐) current-gen 576 条**

| Option | 样本 | 列对齐 | 标签完整性 | 风险 |
|---|---|---|---|---|
| **A. current-gen 真实标签** ✅ | 576 | ✅ live | ✅ ground-truth | 样本少, OOS 可能弱 |
| B. current-gen + old-gen 桥接 | ~1,072 | ⚠️ 需 impute/bridge | ⚠️ 桥接污染 | 违反 GIGO 纪律 |
| C. current-gen 前向收益伪标签 | 8,572 | ✅ | ❌ 合成标签 | M5 前向收益≈噪声, 任务漂移 |

**推荐 A 为主轨道** (Phase 3 GIGO 纪律 + MetaExit 实证路径先例)。C 仅作敏感性诊断臂 (不下场)。B 不推荐。

### 决策 2 — 算法: **LightGBM (推荐)**

| 维度 | LightGBM ✅ | PPO | Transformer |
|---|---|---|---|
| 样本需求 | ~576 即可起步 (深度=3, min_child_samples≥20, leaves≤~28) | ≥10⁴-10⁵ 交互 | ≥10⁴ 序列 |
| 基础设施 | 现有 (MetaExit v3 / swing / expected_r 全 LightGBM txt) | ❌ 无 gym 环境 | ❌ 无序列管线 |
| 序列连续性 | 不需要 | 需要 | **65% bar 缺口致命** |
| 类别不平衡 | scale_pos_weight ≈1.6 原生支持 | 奖励稀疏 | 需类加权 |
| 过拟合防御 | 正则化 + 早停 + feature_importance 剪枝 | — | 576 样本必过拟合 |

**结论**: 576 样本 + 40 特征 (比 14.4:1), 只有 LightGBM 系可以纪律化训练。PPO/Transformer 在此弹药规模下是被否决的投机。

### 决策 3 — 评价指标 (类别不平衡 1.57:1 → accuracy 平凡基线 61% 误导)

| 优先级 | 指标 | 作用 |
|---|---|---|
| 主 | **PR-AUC** (win 类) | 度量相对 39% 基线的真实提升 |
| 次 | **MCC** | 平衡度量, 抗不平衡 |
| **机构闸门** | **OOS Spearman ρ + sign-match** | Flow46/MetaExit 门禁惯例 — 防过拟合幻觉 |
| 校准 | Brier score + 校准斜率 | 为后续 GLOBAL_LOT_SCALE 实证提供可靠 p(win) |

### 决策 4 — 验证集划分: **Time-Series Split (walk-forward, forward-chained, purged+embargoed)**

- **禁止 shuffle / 禁止随机 K-Fold** — 576 条按 open_time 严格时序。
- 划分: 60% train → 20% val → 20% test (**test = 最后 20% 时序 = 真 OOS**)。
- **Purge + Embargo**: 分割边界两侧剔除 ±60 bars (5h) — 特征自相关防泄漏。
- **每 bar 至多一条样本** (同时刻多条交易 → 聚合取一)。
- **knowledge-time 硬约束**: `ingested_at ≤ open_time` (builder 语义已内置)。
- 可扩展: expanding-window 多折 walk-forward (train 60%→70%→80%…, test 恒为尾部), 每折输出 OOS 指标。

### 决策 5 — 特征处理 (锚定 current-gen 40 列)

- 仅用 current-gen 40 列 (旧世代 4 列被替换特征直接剔除, 不参与任何 impute)。
- 当前世代真缺失 = 0 条 → 无需 impute。
- M5/M15/M30/H1 跨 TF 高度相关 → LightGBM 原生处理, 先不 PCA。
- 依赖 feature_importance 剪枝 + L2 正则; 不引入手工特征工程。

### 决策 6 — 标签定义

- 二分类主标签: `y=1 iff pnl > 0` (XAU metafilter 合同 `--spread-cost-usd 0.0`, 基线 WR 38.94%)。
- scale_pos_weight ≈ 1.57 (类别比); **不做过采样** (576 条上过采样=过拟合放大器)。
- 三分类 (win/be/loss) 仅作诊断视图。

### 决策 7 — 部署闸门 (Shadow Mode 红线维持)

候选模型需**全部**通过才获 Shadow 部署资格:

1. **OOS Spearman ρ > 0.05** (Flow46 门禁惯例)
2. **OOS PR-AUC > 0.42** (相对 39% 基线的非平凡提升)
3. **校准斜率 ∈ [0.5, 1.5]** (Brier 合理)
4. Shadow 部署 = **零实盘风险** (MetaExit v3 同款 Shadow Mode 红线)

**硬约束 (不变)**: 勿改 `BASE_LOT`; `GLOBAL_LOT_SCALE` 关系后续实证阶段接线 (roadmap §6), 本战役不触碰。

---

## 四、提交给投委会的决策请求

**数据已开口 — 它说的是: "我没有 43,580, 我有 576。"**

请 IC 就以下作战选择作出裁决:

- **A (推荐)**: 按 Option A 启动 **LightGBM current-gen 576 条**纪律化训练 (walk-forward OOS, 全指标门禁)。预期: OOS ρ 弱 (576 条天花板), 但这是唯一列对齐+真实标签的诚实路径, 建立可信基线 + 校准曲线, 随实盘增长滚动扩充。
- **B (追加诊断臂)**: 并行跑 Option C 前向收益伪标签 (8,572 行) 作为敏感性对照 — 判断 M5 特征在无标签数据上的信号存在性。仅诊断, 不下场。
- **C (推迟)**: 判定 576 条不足以支撑, P3 改为等待新世代实盘数据积累至 ~1,000 条 (约 2 个月) 再训。

任何选择下, 本轮**不启动重度训练进程**。本次仅交付质检报告 + 火力规划, 等候开火授权。

---

## 五、四维质量闸门 (提交物本身)

```
Stability: → (只读审计脚本 + 文档, 零生产代码改动, 零新 I/O 路径)
Repairability: ↑ (单一可复现审计脚本, schema 世代分叉/零填充伪象/可训练覆盖三层诊断一步定位)
Decoupling: → (scripts/_audit_ 独立探针, 不新增 import 链, 不改任何模块接口)
Iterability: ↑ (审计口径集中于单一脚本; 火力规划统一锚定 current-gen 40 列, 未来世代切换有明确裁决点)
```

---

[Ω-Routing: Scene D → #11]
=== IRON LAW #11: SCRIPT OUTPUT (唯一合法证据源) ===
见 `scripts/_audit_xau_micro_scaler_20260823.py` 全量 stdout (Section 1-4, [DONE] 行)。所有统计数字均出自该输出, 本报告零补算。
[DONE] All statistics above are the sole source of truth.

---

[AWAITING_IC_APPROVAL]
