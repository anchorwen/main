# ⚔️ 8/19 Flow46 决战 Runbook（标准作业程序）

> **状态**: ✅ ACTIVE (2026-08-05) — 投委会全线放行 + 前置清偿完成（FIX-20260805-003）
> **场景路由**: Scene B/E → #0 → #6 → #5 → 编码 → #1 验证 → #1.1 四维 → #7 注册 → #7.1 收口
> **战役目标**: Gate 2（≥1000 distinct H1 windows）触发 → 基集刷新 → 46-dim 数据集重建 → OFI 迁移重训（SHORT 刷新 + LONG 重试）→ OOS 盲测 → 血缘铁闸 → 影子脑注册
> **全部统计以脚本 stdout 为准（Iron Law #11），禁止口算/抽样推断**

---

## 0. 战役地图

```
MT5 手工导出 ──► data/raw/*_m5_merged.csv
     │  (阶段 0.1)
     ▼
_merge_aligned_multitf_data.py ──► btc_m5_aligned_multitf.csv
     │  (阶段 0.2, ⚠️ RBI-1 先清偿)
     ▼
build_btc_dataset_from_ssot.py ──► data_btc/training/btc_ssot_v2/  (基集 41 维)
     │  (阶段 1, 覆盖旧 split, 先备份)
     ▼
build_btc_flow46_dataset.py ──► btc_flow46_v1/btc_flow46_aligned.npz  (46 维, leak-free)
     │  (阶段 2, 覆盖旧 npz, 先备份)
     ▼
inspect_ofi_history.py ──► Gate 2 终确认 (≥1000 H1)
     │  (阶段 3)
     ▼
train_btc_flow_46_transfer.py ──► residual_short_best + (LONG 重试)
     │  (阶段 4, hash-lock 强制, 禁止 --allow-dirty)
     ▼
verify_lineage.py ──► 血缘铁闸 PASS → 收口 (FIX-20260819-XXX)
     (阶段 5)
```

**当前基线**（2026-08-05 实证）:
- Gate 2: 688/1,000 H1 windows，积累速率 ~24/天 → ETA ~08-18 04:29 UTC（**仅 ~1 天余量**）
  - 每日进度由 `Future\Gate2Sentinel` 计划任务轮询保障（08-05 起, 见 gate2_sentinel_deployment.md）
- flow46 数据集: `n_aligned_rows=6874`（07-07 14:40 → 07-31 11:30）— **8/01+ OFI 数据无基集行可对齐**
- 基集 test 分片: 7,494 rows（07-05 11:00 → 07-31 11:30）

---

## 1. 不可破坏约束（红线）

| # | 红线 | 违反后果 |
|:---:|:---|:---|
| R1 | **绝不 `--allow-dirty`** | hash-lock 拦截训练，杜绝脏树产出 |
| R2 | **重建基集/flow46 前必须备份旧目录** | 回滚失去锚点 |
| R3 | **OOS ρ < 0.05 一律硬否决**（Phase 3 门），无人工放行 | 重演 ρ=0.0445 靠人工裁决上线的历史错误 |
| R4 | **产出的脑一律 shadow / vote_weight 0.0 / registry enabled=False** | 影子验证期未过，禁止投实盘 |
| R5 | **工作树洁癖**: 8/19 前 `M configs/*.yaml` 等跟踪文件必须收口 | hash-lock + 污染 commit |
| R6 | **数据补给仪式不完成，禁止进入阶段 1** | 基集时间窗不变 → 8/19 = 空转 |

---

## 2. 前置阻断项 Pre-flight（8/17 前必须清偿）

### ✅ RBI-1 — merge 脚本 ROOT 错位（**已清偿 2026-08-05, FIX-20260805-003**）

**实证**（2026-08-05 脚本检查，修复前）:
```
SCRIPT LOCATION:  scripts/archive/_merge_aligned_multitf_data.py
ROOT (= __file__.parent.parent):  D:\future\scripts   ← 错位!
=> DATA_RAW 解析为 D:\future\scripts\data\raw  (不存在)
=> [FATAL] BTC backbone not found  → 补给仪式首步即崩溃
```
**根因**: 脚本原位于 `scripts/`（ROOT=parent.parent=仓库根，正确）；08-01 归档批量移入 `scripts/archive/` 后 ROOT 少退一层。docstring 仍引用 `scripts/_merge_aligned_multitf_data.py` 旧路径，加剧误导。

**修复执行（方案 A 已落地）**:
- ✅ 脚本回迁 `scripts/_merge_aligned_multitf_data.py`（`scripts/archive/` 为 gitignore 区 = "退役"语义，被依赖脚本不得驻留）
- ✅ 过时 "Next:" 提示改为 `build_btc_dataset_from_ssot.py`
- ✅ 预存 B007 (`sym` 未用) 一并清偿（archive 时代被 gitignore 豁免，回迁后过门禁）

**修复后验证（已实测通过）**:
```
ROOT -> D:\future                                            ✅ (修复前 D:\future\scripts)
干跑 EXIT=0, BTC 骨架 50,000 bars 加载, CSV 50,000×47 产出     ✅ ([FATAL] 消失)
```
> 附加坑已消: 脚本结尾 Next 提示现指向 `build_btc_dataset_from_ssot.py`（阶段 1）。

### RBI-2 — 工作树洁癖（hash-lock 前置）

```bash
git status --porcelain
# 期望: 仅剩法证探针 scripts/_audit_*_20260805.py (未跟踪, IC 裁决保留)
# 若出现 M configs/*.yaml 等跟踪文件改动 → 先收口 (裁决 4 已还原 live.yaml)
# 注 (FIX-20260805-007): 8/19 的 hash-lock 已是内容基 (git diff HEAD --name-only),
# 上述 stat 基 porcelain 出现的 mtime-only 幽灵 M 不会拦截训练 — 真正阻断的只有语义变更。
```

### RBI-3 — 关键文件在位清单（12 项）

```bash
python -c "
import os
for c in ['data/training/aligned_btc_multitf/btc_m5_aligned_multitf.csv',
'data/raw/btcusdc_m5_merged.csv','data/raw/audjpyc_m5_merged.csv',
'data/raw/eurusdc_m5_merged.csv','data/raw/usdjpyc_m5_merged.csv',
'configs/training/label_contracts/label-expected-r-btc-m15.json',
'configs/training/btc_flow_46_transfer.yaml','data_btc/reports/ofi_history.jsonl',
'data_btc/training/btc_ssot_v2/meta.json','data_btc/training/btc_flow46_v1/flow_alignment.json',
'data_btc/models/btc_expected_r_v5_m15/tower_long_best.txt',
'data_btc/models/btc_expected_r_v5_m15/tower_short_best.txt']:
    print(('OK  ' if os.path.exists(c) else 'MISS'), c)
"
```
> 2026-08-05 实测: 12/12 在位 ✅（`data/raw/` 三跨资产 M5 亦在，xauusdc/xagusdc 缺失会被 merge 脚本 SKIP，可容忍）

---

## 3. 执行序列

### 阶段 0 — 数据补给仪式（Data Ingestion Ritual, 8/17 或 8/18）

> **执行人**: 数据工程师 | **时长**: ~30-60 分钟 | **窗口**: 任一交易日收盘后

**Step 0.1 — MT5 手工导出**（唯一无法自动化环节，物理隔离）
- BTC 终端: `D:\MetaTrader 5\terminal64.exe`
- 导出品种 × M5 → 覆盖至导出当刻最后一根 bar
- 写入 `data/raw/`（命名规范 `{symbol}_m5_merged.csv`，time 列 UTC，含 OHLCV）:
  - `btcusdc_m5_merged.csv` — **必需**（骨架）
  - `audjpyc_m5_merged.csv` / `eurusdc_m5_merged.csv` / `usdjpyc_m5_merged.csv` — 建议（跨资产对齐）
  - `xauusdc_m5_merged.csv` / `xagusdc_m5_merged.csv` — 缺失可容忍（SKIP）
- **验收**: 各文件 mtime 更新至导出日，尾 bar ≈ 导出时刻

**Step 0.2 — merge 重建 aligned CSV**（RBI-1 清偿后）
```bash
python scripts/_merge_aligned_multitf_data.py --output-dir data/training/aligned_btc_multitf
```

**Step 0.3 — 验收**（Iron Law #11，脚本 stdout 为准）
```bash
python -c "
import pandas as pd
df = pd.read_csv('data/training/aligned_btc_multitf/btc_m5_aligned_multitf.csv', parse_dates=['time'])
print('rows:', len(df), '| last bar:', df['time'].max())
"
```
**GO 判定**: 尾 bar ≥ 8/18 **且** > 旧基集 test 尾点 `2026-07-31 11:30`（否则增量数据未流入，禁止进阶段 1）

### 阶段 1 — 基集重建（8/18, 数据工程师 + 架构师）

```bash
# ① 备份旧基集 (R2)
mv data_btc/training/btc_ssot_v2 data_btc/training/btc_ssot_v2_bak_20260818

# ② 重建 (覆盖写入)
python scripts/training/build_btc_dataset_from_ssot.py \
  --input data/training/aligned_btc_multitf/btc_m5_aligned_multitf.csv \
  --output-dir data_btc/training/btc_ssot_v2 \
  --schema btc_macro_enhanced_41_v2 \
  --tf-minutes 5 \
  --label-contract configs/training/label_contracts/label-expected-r-btc-m15.json \
  --strategy btc_expected_r_m15 \
  --live configs/live_btc.yaml
```

**验收**（对照 08-03 基线）:
- `meta.json` test 分片尾点 ≥ 8/18（旧: 07-31 11:30）
- `meta.json.labels.gate == "validate_label_vs_live.py PASSED"`（标签契约硬门）
- 总行数 > 旧 49,960（34,972+7,494+7,494）— 增量应使 n_train/n_val 亦扩大

### 阶段 2 — 46-dim 数据集重建（8/18）

```bash
# ① 备份旧 flow46 数据集 (R2)
mv data_btc/training/btc_flow46_v1 data_btc/training/btc_flow46_v1_bak_20260818

# ② 重建 (对齐 OFI 到新基集 test 分片)
python scripts/training/build_btc_flow46_dataset.py \
  --base data_btc/training/btc_ssot_v2 \
  --ofi data_btc/reports/ofi_history.jsonl \
  --output-dir data_btc/training/btc_flow46_v1 \
  --tf-minutes 5
```

**验收**（读 `flow_alignment.json`）:
- `leak_audit.verdict == "PASS"`（settle_wall ≤ bar_wall; p99 lag 无时区错位）
- `alignment.n_aligned_rows` > 旧 6,874，且 `base_dataset.feature_order_matches_schema == True`
- `flow_coverage`: OFI_M5/OFI_Cumulative_Delta 保持 ~0.99，`effective_flow_dim ≥ 2`（死特征剔除后硬门）

### 阶段 3 — Gate 2 终确认（8/19 晨会, 09:00 UTC）

```bash
python scripts/inspect_ofi_history.py --data-dir data_btc
```
**GO 判定**: `Gate 2 READY — ≥1000 distinct H1 windows`（哨兵方案落地后此值由每日轮询保障）
**NO-GO**: 若 <1000 → **立即中止，绝不强行重训**（数据不足时迁移残差模型无统计意义）

### 阶段 4 — 迁移重训（8/19, 架构师, 工作树必须干净）

```bash
git status --porcelain   # 期望: 仅未跟踪 _audit_* 探针 (hash-lock 内容基 FIX-20260805-007, 幽灵 M 不阻断)
python scripts/training/train_btc_flow_46_transfer.py \
  --contract configs/training/btc_flow_46_transfer.yaml \
  --dataset data_btc/training/btc_flow46_v1/btc_flow46_aligned.npz \
  --live-yaml configs/live_btc.yaml \
  --governance-path data_btc/governance_state.json
```

**断言**（脚本自动执行，非人工判断）:
- 双塔: `TOWERS = ("LONG","SHORT")` — SHORT 残差刷新 + LONG 残差重试
- hash-lock: 工作树脏 → 直接报错（R1）
- OOS 盲测: 任一塔失败 → `ModelQualityException`，**不产出脑**（R3）
- 产出: `residual_short_best.*` + `residual_long_best.*`（若 LONG 通过）→ shadow 脑 config（R4）

### 阶段 5 — 血缘铁闸 + 收口（8/19）

```bash
python scripts/training/verify_lineage.py \
  --live configs/live_btc.yaml \
  --brains-dir configs/brains_btc \
  --registry-db data_btc/training/registry.db
```
**验收**: 新产 Flow46 脑 FAIL=0（shadow 脑 live_yaml_enabled=False 应入豁免/在册路径，需当场确认）

**收口序列**（Iron Law #13）: 四维质量闸门评估 → `FIX-20260819-XXX` 注册（`scripts/register_fix.py`）→ 蓝图 Fix History 更新 → 约定式提交 → push。

### 3.5 预演基线与预期偏差（The Baseline Anchor, IC 2026-08-05 裁决锚定）

> **锚点来源**: 2026-08-05 全链预演（scratch 目录，零污染，不注册脑）— 全部脚本 stdout 实证。
> 预演在**同一 commit `ec938c69`** 上逐位复现 8/3 ⇒ **管线确定性已证**。
> 因此 8/19 真实训练相对基线的任何偏差 = **数据层面漂移**（8 月新增 OFI/基集），**非代码因素**。

**预演基线锚点**（2026-08-05）:

| 塔 | 预演 OOS ρ | 门槛 | 判定 | 锚点意义 |
|:---:|:---:|:---:|:---|:---|
| SHORT | **0.0721** | 0.05 | PASS（余量 1.44×） | 8/19 刷新后跌破 0.05 ⇒ 8 月订单流特征漂移 |
| LONG | **0.0087** | 0.05 | 硬否决（确定性） | 8/19 突破 0.05 ⇒ 1000 窗口多头残差拼图凑齐 |

**预期偏差判定表**（8/19 真实训练对照锚点）:

| 结果 | 归因 | 处置 |
|:---|:---|:---|
| SHORT ρ ≥ 0.05 | 无漂移 | 准予注册（shadow），正常收口 |
| SHORT ρ < 0.05 | **8 月新增订单流特征漂移 (Feature Drift)** | **硬否决 + 回滚预案 §5**（保旧 `residual_short_best.*`） |
| LONG ρ ≥ 0.05 | **多头残差拼图凑齐**（数据补齐所致，非管线变化） | **准予晋升**（LONG 塔首过，注册 shadow） |
| LONG ρ < 0.05 | 残差仍不足 | 正常科研否决，不注册（零风险），记研究风险 |

**方法论**: 8/19 与预演的唯一变量 = 数据窗口（8/01+ 对齐行 + 新基集）⇒ OOS ρ 的变化即特征漂移的**量化信号**，锚点给出硬性裁决基线，杜绝"手感裁决"。

---

## 4. GO/NO-GO 决策表

| 阶段 | GO 条件 | NO-GO 动作 |
|:---:|:---|:---|
| 0 | 尾 bar ≥ 8/18 且 > 07-31 11:30 | 重导 MT5 / 修 merge，不跳过 |
| 1 | meta 尾点 ≥ 8/18 + 标签门 PASS | 检查 merge 输出，修复后重跑 |
| 2 | leak PASS + n_aligned 增长 | 检查 OFI 数据完整，修复后重跑 |
| 3 | ≥1000 H1 窗口 | **中止决战**，转 OOS 回滚预案 §5，等数据 |
| 4 | 双塔盲测通过产出脑 | 未通过塔不注册，记录研究风险 |
| 5 | verify_lineage FAIL=0 | 补血缘字段，不 commit |

---

## 5. OOS 坍缩回滚预案（Rollback）

**触发条件**（任一）:
1. **LONG 残差 OOS ρ < 0.05** → 硬否决，不注册脑，记入研究风险（8/3 已否决 ρ=0.009，本次重试属正常科研流程）
2. **SHORT 残差刷新后 OOS ρ < 0.05**（旧值 0.0721，仅 1.44× 门槛余量）→ 硬否决，保留旧 `residual_short_best.*`
3. **数据集重建触发 base 特征漂移**（`test_feature_bit_identical.py` 失败）→ 全链回滚

**回滚命令**（原子执行，单次收口）:
```bash
# ① 恢复旧基集
rm -rf data_btc/training/btc_ssot_v2
mv data_btc/training/btc_ssot_v2_bak_20260818 data_btc/training/btc_ssot_v2

# ② 恢复旧 flow46 数据集
rm -rf data_btc/training/btc_flow46_v1
mv data_btc/training/btc_flow46_v1_bak_20260818 data_btc/training/btc_flow46_v1

# ③ 旧残差模型天然在位 (未通过盲测的塔不注册/不覆盖)
ls data_btc/models/btc_flow46_v1/   # 应仍为 residual_short_best.*

# ④ 确认运行时仍走旧脑 (registry enabled=False 保证零投盘面)
python scripts/_audit_live_shadow_inventory_20260805.py
```

**回滚后**:
- 8/19 决战略夺 → 改口为"数据续积累"，Gate 2 计数继续滚动（哨兵持续监控）
- 8/20+ 择机重试，或等待下一触发窗口
- 不删除任何影子脑/残差，保留科研证据链

---

## 6. 责任矩阵

| 角色 | 时间窗 | 责任 |
|:---|:---|:---|
| 数据工程师 | 8/17–8/18 | MT5 导出 + 阶段 0 仪式 + 阶段 1 |
| 架构师 | 8/17 前 | RBI-1 修复 + RBI-2/3 预检 |
| 架构师 | 8/18–8/19 | 阶段 2 + 阶段 4 + 阶段 5 收口 |
| 投委会 (IC) | 8/18 晚 | Gate 2 数据终审 + 阶段 4 GO/NO-GO 拍板 |
| 哨兵（自动化） | 每日 | Gate 2 轮询 + 停滞告警（见 gate2_sentinel_deployment.md） |

---

## 附：验证命令速查

| 命令 | 用途 |
|:---|:---|
| `python scripts/inspect_ofi_history.py --data-dir data_btc` | Gate 2 轮询 |
| `python scripts/training/build_btc_dataset_from_ssot.py --help` | 阶段 1 参数确认 |
| `python scripts/training/build_btc_flow46_dataset.py --help` | 阶段 2 参数确认 |
| `python scripts/training/train_btc_flow_46_transfer.py --help` | 阶段 4 参数确认 |
| `python -m pytest tests/training/test_flow46_alignment.py -x -q` | 对齐泄漏回归锁 |
| `python -m pytest tests/training/test_feature_bit_identical.py -x -q` | 特征一致性回归锁 |
| `python scripts/_audit_live_shadow_inventory_20260805.py` | 决战前后脑状态对账 |

---

*蓝图注册: ✅ 已登记 — `FIX-20260805-003` (blueprints/modules/deployment_config.md Fix History + monitoring.md)。*
