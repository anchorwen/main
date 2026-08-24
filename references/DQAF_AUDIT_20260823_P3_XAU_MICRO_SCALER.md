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

[AWAITING_IC_APPROVAL] — (2026-08-23 后置: 该审批仅针对原 Option A 主轨道, 已被 §6 战略重定向取代)

---

# 六、IC 战略重定向 (2026-08-23) — The Architectural Epiphany: Option B 升格为正规军轨道

## 6.1 IC 裁决 (逐字要点)

- **绝对批准**: 将 Option B (前向收益预测) 全面升格为 Micro Scaler 的正规军轨道。
- **废弃 Option A**: 彻底放弃用 Swing 交易的实盘 Label 训练 Micro Scaler。**视野错位**: 用持仓数小时的"重型巡洋舰"战果, 训练持仓仅几个 M5 bar 的"微观冲锋舟", 预测周期 (Horizon) 根本错位。
- **升格 Option B**: current-gen 8,572 行干净特征 + Forward Return 为目标变量, 重新规划训练管线 (可回归预测收益率, 或多分类预测涨跌阈值)。
- **真理重构**: 微高频领域, 前向 3-bar 收益本身即最纯粹的 Ground Truth — 剥离 SL/TP 噪音, 直接度量特征与微观动量间的信息熵 (ρ=0.466 已证明)。
- **部署红线**: Shadow Mode 强制。后续核心战役 = **如何将这种信号安全转化为实盘的 Trigger** (连续序列预测更易产生极端校准偏置, 如 slope 1.749)。
- **今日不执行**: 战术规划记录在案, 不启动训练进程。

## 6.2 架构顿悟的实证锚定 (新探针 `_audit_xau_hold_time_horizon_20260823.py`, Iron Law #11 唯一证据源)

> IC 顿悟是战略判断; 本探针用 1,259/1,261 笔可标注 XAU 交易的真实持仓时长把它钉成数据事实。

**全体 1,259 笔持仓时长 (分钟)**: median **21** / p90 146 / max 3306 (55h) / **≤15min 仅 41.2%**

**按策略 (n≥7)**:

| 策略 | n | median | p90 | ≤15min | ≤60min |
|---|---|---|---|---|---|
| statarb_dynamic | 299 | 10 | 39 | 64.9% | 95.0% |
| m30_swing | 264 | 36 | 179 | 21.2% | 61.4% |
| h1_swing | 174 | 66 | 454 | 17.8% | 46.0% |
| barrier_12bar | 161 | 10 | 55 | 63.4% | 91.3% |
| m15_swing | 161 | 25 | 170 | 39.1% | 67.1% |
| h4_swing | 58 | 55 | 681 | 15.5% | 55.2% |
| **micro_3bar** | **56** | **5** | 72 | **76.8%** | 89.3% |
| structural_swing_v1 | 7 | 20 | 30 | 28.6% | 100.0% |

**⚠️ 关键 — current-gen 窗口 (open ≥ 2026-05-24, n=582)**: median **55** / p90 **292** (≈4.9h) / **≤15min 仅 13.7%** / ≤60min 52.9%

**判读 (数据事实, 非推断)**:
1. **Option A 的 576 个标签, 中位持仓 55 分钟 ≈ 11 个 M5 bar, p90 近 5 小时 ≈ 58 bars** — 这些是 Swing 策略 (m30/h1/m15/h4_swing) 的收盘战果, 与 Micro Scaler 3-bar (15min) 周期**根本错位**。IC 顿悟被数据确认。
2. **真微周期标签近乎不存在**: 历史 `micro_3bar` (median 5min, n=56, 76.8% ≤15min) 是唯一接近目标周期的标签, 但 n=56 远不够训练, 且是否落 current-gen 世代未核。→ **Forward Return 是唯一可扩展的正确 Ground Truth** (8,572 行全覆盖, 零 SL/TP 噪音)。
3. statarb/barrier 等短持仓策略全在早期 (current-gen 窗口外), 进一步压缩了可用真微标签。

## 6.3 校准偏置的根因假设 (slope 1.749, 待 v2 验证)

v1 pseudo-diag 实为**方向二分类** (base_rate 0.5156, PR-AUC 0.7573, ρ=0.4661, calib_slope 1.7491, pred_mean_pos 0.573 vs neg 0.452)。**注意: 该 OOS 为 70/30 划分 (OOS n=2351), 切分纪律未在 v1 严格复验 — v2 须在 60/20/20 ts_purged_split 下重跑, 数字才能横向可比。**

slope>1 (概率过度外展) 三个待证假设:
- (a) 树集成拟合幅度噪音 → 叶子分裂过深产生极端预测。缓解: leaves 下调, min_child 上调, L2 加码, feature_fraction 收紧。
- (b) XAU M5 肥尾 → 极值收益主导斜率。缓解: 目标 Winsorize/clip ±3σ, 或 Huber loss。
- (c) 分类校准天生过度 → **Isotonic Regression** (val fold 拟合映射 → OOS fold 验证, 防自偏), post-isotonic 目标斜率 ∈ [0.9, 1.1]。

## 6.4 Micro Scaler v2 训练蓝图 (Forward Return Track, 待 IC 开火令)

| 维度 | 设计 | 备注 |
|---|---|---|
| **标签** | 主: 回归 forward-3bar return (连续性, ρ 直接可测, isotonic 可校准); 诊断臂: 方向二分类 (涨跌阈值) | IC 允许回归/多分类, 推荐回归为主 |
| **数据** | current-gen 8,572 行 (filter_current_gen, TECH_DEBT-023 守卫), 40 列, 真缺失=0 | v1 已验证 |
| **切分** | 60/20/20 ts_purged_split (purge+embargo ±60 bars, 禁 shuffle), 复用 v1 代码 | 与 Option A 同构 |
| **模型** | LightGBM regression (huber/asymmetric, depth≤3, min_child≥20, 强 L2) | expected-r 同哲学 |
| **指标** | OOS ρ / 分位 IC / post-isotonic 校准斜率 / **net-of-cost top-decile 期望收益 > 盈亏平衡** / trigger rate ∈ [1%, 50%] | cost 门禁为新增 |
| **数据缺口 (已清偿, 2026-08-24)** | v9 current-gen 无 spread 列 → cost model `scripts/build_micro_cost_model.py` ASOF 接 `v4.3_microstructure_9` avg_spread (14,839 行, 部署自 2026-06-14) + MT5 探针锚点 | ✅ 周一开火序列 Step2 完成 |
| **成本模型工具** | `scripts/build_micro_cost_model.py` — pd.merge_asof `direction='backward'` (防 1ms 前视) + 逐 M5 bar 盈亏平衡线 (动态 \|avg_spread\| + 佣金) | 门禁凭证唯一来源 (Iron Law #11) |
| **部署** | Shadow Mode 强制, 零 live 风险; 信号写入 golden_master, 不触任何实盘执行代码 | MetaExit 红线维持 |

## 6.5 状态

[RECORDED — 2026-08-23] 战术规划已落案。**今日不执行训练进程**。待 IC 开火令启动 v2。核心战役优先级: ① 严格切分复验信号 → ② Isotonic 校准 → ③ cost model + Trigger 转化。

[UPDATE — 2026-08-24 周一开火序列 Step2/3 门禁凭证] `scripts/build_micro_cost_model.py` stdout (唯一证据源):
- **样本**: current-gen 8,280 记录 → 4,567 unique bar → **4,010 具 3-bar 前向上下文** (Option B 训练池)。
- **v4.3 avg_spread 符号翻转 bug (材料发现 #1)**: 全世代 14,839 行 100% 负号 (逐周实证 W24-W34 零正值) — `microstructure_computer._compute_tick_features` 将 MT5 `t[1]=bid/t[2]=ask` 错位写入 `bids=t[2]/asks=t[1]` → `asks-bids=-(真实spread)`。**消费端取 |avg_spread| 还原** (median 0.2402 / MT5 探针 p50=0.26 交叉验证一致)。生产侧修复列 TECH_DEBT 登记, 勿动核心代码。
- **ASOF 覆盖瓶颈**: v4.3 部署 2026-06-14 起 → 命中 587/4,010 (14.6%), 其余回退实测锚点 0.26 (fallback_rate 85.5%)。
- **Net-of-Cost Alpha (基准 = 实测动态 spread)**: be_mean 0.00595%; coverage P(|fwd3|>be)=**95.99%**; top-decile mean_net=**+0.381%**; net-of-cost alpha mean +0.107% / median +0.073%; 方向平衡 (up_share 47.9%, fwd_mean −0.005%)。
- **压力情景**: 0.60 USD spread → coverage 90.85%, top-decile net +0.373%。
- **Toll-gate verdict: PASS** — 扣除真实动态 spread 后 4,010 切片仍有 96% 净胜率空间。**fit() 授权待 IC 裁决**。

[RECORDED — 2026-08-24] 附带材料发现 #2: **M5_Ret_1 不可靠** (median |M5_Ret_1−真实1bar|=0.0447%, ρ=0.11) → forward return 必须来自 MT5 真实价格 (本模型已采用), v1 Option B ρ=0.466 证据存疑。材料发现 #3: current-gen 8,572 记录说法修正为 4,567 unique bar。**未接 fit() 授权令, 严禁任何训练。**

[RECORDED — 2026-08-24 fit() 执行 — V2 Forward Return Track 训练战役] **IC 解除 fit() 禁令 (门禁 PASS 后) → `scripts/training/train_micro_scaler_v2.py` stdout (Iron Law #11 唯一证据源, FIX-20260824-002)**:
- **数据矩阵**: 4,010 个去重 current-gen bar × 40 列 (零缺失零 NaN), 标签 = MT5 真实 forward-3bar return (材料发现 #2 强制)。
- **切分**: 60/20/20 ts_purged_split → train 2347 / val 728 / test 748 / purged 187 (purge±300min, 禁 shuffle, V1 同款 SSOT 复用)。
- **模型**: LightGBM **huber** (depth 3/min_child 20/leaves 8/L2 2.0/lr 0.03), early_stop best_iteration=25。
- **OOS ρ**: raw **0.0984** → post-isotonic **0.1101** (qIC 0.176→0.552) — **真实排序信号, > Flow46 0.05 门禁**。
- **net-of-cost top-decile (OOS)**: **+0.0176% PASS** (trigger 9.89% ∈ [1%,50%], net_pos_share 59.5%), full-OOS mean_net +0.0057%。
- **⚠️ 校准斜率门禁 FAIL**: post-isotonic OOS slope=**0.5048** ∉ [0.9,1.1] — raw 过度外展 (2.40) → isotonic 在噪声 val (n=728, ρ=0.079) 上过冲过度压缩。M5 3-bar σ=0.14% 信噪比下 [0.9,1.1] 精度不可达 (Flow46 纪律: 勿调参追 OOS)。
- **方向诊断臂** (Blueprint §6.4): OOS ρ=0.035 / PR-AUC 0.502≈base 0.483 → 方向信号弱, 幅度排序 > 方向。
- **V2 门禁判词: FAIL_SLOPE — 模型未获 Shadow 部署资格, 诚实 FAIL 不粉饰。** 工件 data/training/micro_scaler_v2/。**零实盘代码触碰 (Shadow Mandate)。**

---

[Ω-Routing: Scene D → #11]
=== IRON LAW #11: SCRIPT OUTPUT (唯一合法证据源) ===
§6.2 全部持仓时长统计出自 `scripts/_audit_xau_hold_time_horizon_20260823.py` stdout (2026-08-23 运行); §6.3 出自 `data/training/micro_scaler_v1/micro_scaler_v1_pseudo_diag.json` (v1 输出). 零补算.
[DONE]
