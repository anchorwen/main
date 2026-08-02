# Active Tasks Registry (精确待办清册)

> 每个任务含可度量前置条件 + 当前实际值 + 采集命令 + 预计达标日 + 检查频率

---

## T19: BTC rr_below_minimum 监控 (FIX-20260704-007)

| 字段 | 值 |
|:---|:---|
| **状态** | 🟡 MONITORING (观察中) |
| **创建时间** | 2026-07-04T09:55Z |
| **关联 Fix** | FIX-20260704-007 (IEEE 754 floating-point guard band) |
| **前置条件** | 系统重启激活 FIX-007 后，累计 >=50 个实盘周期 |
| **当前值** | 6 周期 (2026-07-04T09:25 ~ 09:50), rr_below_minimum=0 |
| **目标值** | rr_below_minimum 率 < 1% (重启前 ~100%，FIX-005 前 ~47.5%) |
| **采集命令** | `python -c "import json; from pathlib import Path; ..."` 统计 post-restart golden_master 中 rr_below_minimum |
| **预计达标日** | 2026-07-04T15:00Z (需 ~6h 累积 50 周期，M5=12/h × 2 策略 = 24/h) |
| **检查频率** | 每日 1 次，至达标 |
| **达标后动作** | 关闭此 task，记录最终 rr_below_minimum 率 |
| **异常阈值** | rr_below_minimum > 2% 持续 3h → 升级 Sev 2 DQAF |

## T20: BTC V12_H1_15 calibration_offset 激活决策 (FIX-20260704-002)

| 字段 | 值 |
|:---|:---|
| **状态** | 🟢 CLOSED (2026-07-06) — 方向分布平衡, 校准不需要 |
| **创建时间** | 2026-07-04T09:55Z |
| **关联 Fix** | FIX-20260704-002 (calibration_offset +0.101, 当前设为 0.0 — 永久保持) |
| **前置条件** | 重启后累计 >=24h 实盘 golden_master 数据 (含 direction/direction_pred 配对) |
| **当前值** | **1,030 records (7/2–7/6), LONG=57.9% SHORT=37.4% NEUTRAL=4.8% — BALANCED** |
| **决策** | 方向分布在 25-75% 范围 → **calibration_offset=0.0 永久保持, 不激活校准** |
| **证据** | 模型在 7/3 20:00 完成 SHORT→LONG 干净切换, 证明对市场方向变化有响应能力 |
| **采集命令** | `python scripts/analyze_shadow_predictions.py --data-dir data_btc --days 3 --brain-id BTC_Swing_V12_H1_15` |
| **达标后动作** | ✅ 已完成 — calibration_offset 保持 0.0, T20 关闭 |
| **异常阈值** | N/A (已达标) |

## T21: 46-dim OFI Flow 数据积累 (FIX-20260707-005/006)

| 字段 | 值 |
|:---|:---|
| **状态** | 🟢 GATE1 PASSED (2026-08-03) — Wasserstein 扫描 PROCEED, 等闸门2 |
| **创建时间** | 2026-07-07T14:41Z |
| **关联 Fix** | FIX-004 (OFICollector +3 特征) · FIX-005 (ofi_history 记录器) · **FIX-006 (双计数闸门)** |
| **⚙️ 双闸门口径 (FIX-006)** | monitor 分别计 raw settle 与 distinct H1 window, 已消除 "raw=可重训" 误导 |
| **闸门1 (筛查)** | ≥2,000 raw settles **且** 跨度 ≥7 天 → Wasserstein 特征判别力扫描 (便宜 GO/NO-GO) |
| **闸门2 (重训)** | ≥1,000 distinct H1 windows → H1 粒度迁移重训 (冻结 41-dim 学 OFI 增量) |
| **当前值** | **74,264 settles / 627 H1 windows / 存活 3/5 → 有效 44-dim** (2026-08-03) |
| **实测节奏** | **~30s/settle ≈ 2,880/day**; H1 窗口 = **24/day** (二者差 120×, 已核实训练集 timestamps 恒定 3600s) |
| **特征存活实测** | OFI_M5=100% · OFI_ZScore_20=89% · OFI_Cumulative_Delta=100% · **Delta_Divergence=0%** (稀疏) · **Volume_Real_Ratio=0%** (BTC 结构性死特征) |
| **采集命令** | `python scripts/inspect_ofi_history.py --data-dir data_btc` |
| **预计达标 (闸门1)** | ✅ **PASSED 2026-08-03** — `scripts/scan_ofi_wasserstein.py` (19,231 前向1H标签, 18.03d): **BEST W1=0.1701 (OFI_Cumulative_Delta) ≥ 0.02 → PROCEED** (41-dim 基线 0.0084 的 20×; 非重叠子样本 0.2368 排除自相关) |
| **预计达标 (闸门2)** | **~2026-08-19** (627/1,000 H1 窗口, 373 ÷ 24/day ≈ 15.5 天) |
| **检查频率** | 每日 1 次 |
| **裁决路径** | 闸门1: 最优 OFI 特征 Wasserstein <0.01 → 停(转 liquidation/funding); ≥0.02 → 等闸门2 → 迁移重训 → 影子部署 |
| **异常阈值** | 积累 48h 后 live_flow_features 仍 <3 → 重评 OFI 方案有效性 |

> 关联记忆: [[deferred_46dim_flow_retrain]] · [[diagnostics_20260628_btc_all_long_bias]]

## T22: BTC V4 confidence 校准影子监控 (FIX-20260708-002)

| 字段 | 值 |
|:---|:---|
| **状态** | 🟡 MONITORING (观察中) |
| **创建时间** | 2026-07-08 |
| **关联 Fix** | FIX-20260708-002 (V4 confidence_params = quantile_gaussian, n=128 bootstrap) |
| **背景** | DQAF-20260707-003 唯一 live 行为变更: V4 从 tanh fallback 改用 quantile_gaussian 校准 (机制已 live 于 8 XAU brain)。参数为 GM back-calc bootstrap, 待实盘 brain_votes 重校准。 |
| **前置条件** | V4 采用新校准后累计 ≥500 golden_master V4 周期 (含 brain_votes raw_score) |
| **当前值** | 0 周期 (校准刚生效 2026-07-08) |
| **目标值** | (1) V4 PF 不低于基线 1.36; (2) confidence 分布不塌缩 (std 不回退至 ~0.01) |
| **采集命令** | `python scripts/analyze_live_journal.py --data-dir data_btc` (V4 PF) + golden_master brain_votes/confidence std 统计 |
| **达标后动作** | 从 brain_votes raw_score 重新校准 p95/peak_conf/lambda_decay (替换 n=128 bootstrap) |
| **异常阈值** | V4 PF < 1.0 持续 5 日 OR confidence std < 0.02 → 回滚 confidence_params 至 tanh fallback (删除该 block) |

> 关联记忆: [[deferred_brain_governance_rectification_20260628]] · DQAF-20260707-003 (CCT-20260708-002)

---

## 快速状态

```
2026-07-07: 46-dim OFI 前向积累启动 (T21)
  第二次 bridge 重启 ✅ — ofi_history.jsonl 记录激活 (55 records)
  实测: ~30s 节奏, 3/5 特征存活 (44-dim 有效), Volume_Real_Ratio 结构性=0
  ✅ 双计数闸门就位 (FIX-006): 闸门1 raw+span(~7/15) / 闸门2 H1窗口(~8/18)

2026-07-06 终审: V12_H1_15 方向平衡, 校准不激活
  FIX-005 (spread pre-comp): ACTIVE
  FIX-006 (SL/TP observability): VERIFIED ✅
  FIX-007 (IEEE 754 guard): MONITORING (T19 继续)
  FIX-20260704-001 (V12_H1_15 retrain): DONE ✅
  FIX-20260704-002 (calibration_offset): CLOSED ✅ — 方向 BALANCED (LONG 57.9%/SHORT 37.4%), 校准不需要. 1,030 records 终审.
```
