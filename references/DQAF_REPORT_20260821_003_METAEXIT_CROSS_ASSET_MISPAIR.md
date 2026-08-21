# DQAF-20260821-003 取证战报 — P2 MetaExit 战役收官 (The Default Trap)

> **性质**: IC 雷霆裁决 (2026-08-21) 之 P2 MetaExit 战役闭环取证报告
> **Docket**: DQAF-20260821-003 (Sev 3) → **CLOSED** — FIX-20260821-008 (commit `cf0b92c0`)
> **CCT**: CCT-20260821-004 · **ReB**: `CROSS_ASSET_DEFAULT_SILENT_MISPAIR`
> **收口令**: 执行 P2 修复 → 部署 v3 暗影 → 战役封存 → **强制休整**

---

## 1. 战役摘要

P2 MetaExit 战役的谜面是「v2 retrain 被质量门禁以 `insufficient samples` (7 wins < 15) 拒签」——
看起来像数据不足，实际是**路由语义缺陷**：训练脚本的默认输入路径跨品种静默错配。

| 项目 | 结论 |
|:---|:---|
| **根因** | L2 逻辑缺陷 (RC-06 contract-violation) — 训练脚本默认 snapshots=XAU / journal=BTC 跨品种错配 + 加载后无 join 保留率守卫 + 运行时模型路径硬编码单品种 (H2) |
| **真因分层** | 非数据不足 (XAU 期刊下 86.6% 保留, 134 wins ≥15 本可 PASS) |
| **Blast Radius** | XAU v2 retrain 假拒 + v3 未上线; BTC 同脚本潜在同坑 + BTC 进程曾加载 XAU 退出模型; 零资金风险 (MetaExit 恒 shadow telemetry) |
| **修复** | 裁决一 The Consistency Guard (绝对批准) + 裁决二 The Shadow Deployment (批准) |
| **验收** | 负对照 EXIT=1 (CONSISTENCY GUARD HALT) / 正对照双重训 EXIT=0 / 34 测试 / verify --quick 全绿 |

---

## 2. 真因链 (CCT-20260821-004)

- **[Layer 1 — 症状]**: v2 MetaExit retrain 被质量门禁拒签 — `insufficient samples` (7 wins < 15 下限), v3 引擎无法上线, MetaExit 滞留 shadow telemetry 旧模型。
- **[Layer 2 — 中间异常]**: 训练脚本默认 `--journal data_btc/live_trade_journal.jsonl` (BTC) 与默认 snapshots `data/meta_exit_snapshots.jsonl` (XAU) 跨品种错配 → 311 干净 XAU ticket 无 BTC close → 配对仅 31 碎片 (保留率 8.6%) → 训练集结构性退化 → 门禁诚实拒签。
- **[Layer 3 — 根因]**: L2 逻辑缺陷 (RC-06 contract-violation) — 训练脚本默认输入路径无品种感知 + 加载后无 join 保留率一致性断言; 运行时同构 (CROSS_ASSET_CONTAMINATION_AUDIT H2): `live_intent_loop.py` MetaExit 模型路径硬编码 XAU, BTC 进程加载 XAU 退出模型。

**AR 对抗反驳**:
- 「门禁阈值不合理」→ 被 XAU 期刊 86.6% 保留率 / 134 wins ≥15 推翻 — 数据本就足够。
- 「数据确实不足」→ 被同一快照双期刊对照 (86.6% vs 8.6%) 推翻 — 路由缺陷, 非数据量。

---

## 3. 取证证据 (Iron Law #11 — script stdout 唯一合法证据源)

`python scripts/_audit_metaexit_gate_20260821.py` gate survey (复刻训练脚本配对口径):

```
[1] 快照宇宙普查 (Snapshot Universe Census)
    raw records        : 6473
    distinct tickets   : 359
    records/ticket     : max=204 p50-ish top: [204, 171, 165, 157, 147]
    time span          : 2026-06-08 08:49:42 UTC → 2026-08-17 10:05:05 UTC
[2] Journal 归属普查 (Journal Membership)
    in XAU journal only  : 311
    in BTC journal only  : 32
    in both journals     : 0
    in neither journal   : 16
[3] 配对口径对比 (Pairing Cross-caliber)
    [raw @ XAU journal (data/live_trade_journal.jsonl)]
      paired=311  wins=134  losses=177  WR=43.09%  gate=✅ PASS  (需 ≥15 wins 且 ≥20% WR)
      skipped_no_label=48  close_pnl_null_or_zero=31
    [raw @ BTC journal (data_btc/live_trade_journal.jsonl)]
      paired=31  wins=7  losses=24  WR=22.58%  gate=❌ FAIL  ← v2 retrain 假拒签现场
      skipped_no_label=328  close_pnl_null_or_zero=1
    [clean @ XAU journal (exclude manual_close/orphan, FINAL_CLOSE_ACTIONS)]
      paired=311  wins=134  losses=177  WR=43.09%  gate=✅ PASS
      skipped_no_label=48  skipped_manual_only=0  close_pnl_null_or_zero=31
```

**同一快照宇宙, 双期刊对照** — 保留率 86.6% (311/359, XAU) vs 8.6% (31/359, BTC) 即铁证。XAU 期刊下 134 wins ≥15 门禁本可 PASS → 「数据不足」是假象。

---

## 4. 修复 — 裁决一: The Consistency Guard (绝对批准)

文件: `scripts/training/train_exit_metamodel.py`

1. **per-asset path SSOT** — `_SYMBOL_PATHS` 单点定义 (xau/btc → snapshots/journal/output):
   ```python
   _SYMBOL_PATHS = {
       "xau": {"snapshots": "data/meta_exit_snapshots.jsonl",
               "journal": "data/live_trade_journal.jsonl",
               "output": "data/models/meta_exit_model_v3_xau.txt"},
       "btc": {"snapshots": "data_btc/meta_exit_snapshots.jsonl",
               "journal": "data_btc/live_trade_journal.jsonl",
               "output": "data_btc/models/meta_exit_model_v3_btc.txt"},
   }
   ```
   `--symbol` (xau|btc) 动态派生; 取消跨品种硬编码默认; snapshots 模式无 symbol/无显式路径 → 硬错。
2. **Join-Retention 硬断言** — `_assert_join_retention`: snapshot/journal ticket 交集保留率 < 阈值 (默认 50%) → `sys.exit(1)`, 附 `CONSISTENCY GUARD HALT: cross-asset pairing suspected` 诊断。
3. **data_source 标签修正** — 19-dim = `ExitFeatureSnapshot` (原 `len==20` 误标 journal)。

---

## 5. 部署 — 裁决二: The Shadow Deployment (批准, 绝对红线维持)

| 资产 | v3 模型 | 重训口径 | 门禁 |
|:---|:---|:---|:---|
| XAU | `data/models/meta_exit_model_v3_xau.txt` | 311 配对 / 134 wins / **43.09%** WR / 19-dim | ✅ PASS |
| BTC | `data_btc/models/meta_exit_model_v3_btc.txt` | 314 配对 / 100 wins / **31.85%** WR / 19-dim | ✅ PASS |
| legacy | `data/models/meta_exit_model.txt` (v1 8-dim) | — | 向后兼容 LOADED |

- `core/deployment/path_defaults.py`: `META_EXIT_MODEL_XAU_PATH` / `META_EXIT_MODEL_BTC_PATH` (per-asset SSOT)。
- `scripts/live_intent_loop.py`: 按 `args.base_dir` 品种分派 (`"btc" in base_dir` → BTC 模型, 否则 XAU) — **CROSS_ASSET_CONTAMINATION_AUDIT H2 根治**。
- **绝对红线: Shadow Mode 维持** — `core/runtime/management_phase.py` L2110-2134 `"BLOCKED — telemetry only, close NOT dispatched"` **零改动**。v3 在真实实盘时间流跑满 **72 小时**并产生置信遥测数据前, 绝不允许拥有真正「物理斩杀 (Force Exit)」权力。

---

## 6. 验收控制 (正/负对照)

| 对照 | 场景 | 结果 |
|:---|:---|:---|
| **负对照** | XAU snapshots × BTC journal (修复前默认静默场景) | 保留率 8.6% < 50% → **CONSISTENCY GUARD HALT EXIT=1** ✅ |
| **正对照 1** | XAU 重训 `--symbol xau` | EXIT=0, 134w/43.09%, data_source=ExitFeatureSnapshot ✅ |
| **正对照 2** | BTC 重训 `--symbol btc` | EXIT=0, 100w/31.85%, data_source=ExitFeatureSnapshot ✅ |
| **加载兼容** | v3_xau / v3_btc / v1_legacy load_model | 双 19-dim LOADED + v1 8-dim LOADED ✅ |
| **回归锁** | tests/scripts/test_train_exit_metamodel_guard.py | 9 测试 (路径派生/覆盖/无 symbol 硬错/保留率/跨品种 HALT/空宇宙 HALT/精确阈值) ✅ |

全量: 34 passed (9 新增 + 25 meta_exit_engine) + `verify.py --quick` 全绿 (mypy/ruff/import-boundaries/artifact/FIX_REGISTRY gate; 13 config 一致性 WARN 为既有 shadow-brain 债, 非本修复引入)。

---

## 7. 归档 (DQAF Terminal Closure Lock 三步)

1. **DQAF_DOCKET_REGISTRY.md** — DQAF-20260821-003 → Status **CLOSED** (FIX-20260821-008)。
2. **CCT_LEDGER.md** — CCT-20260821-004 (Layer 1-3 因果链 + 证据引用 + AR 是否被推翻 + 关联 ReB)。
3. **ReB_PATTERN_INDEX.md** — `CROSS_ASSET_DEFAULT_SILENT_MISPAIR` 登记 (签名/预防/检测)。
4. **FIX_REGISTRY.md** + **training.md** + **runtime_live.md** + **deployment_config.md** Fix History 全更新。

---

## 8. 战役收口状态

- **FIX-20260821-008** — commit `cf0b92c0` (14 文件, 380+/27-), pre-push Omega scan PASSED, pushed `18816af2..cf0b92c0`。
- **P2 重启 4 条件**: ① ≥500 笔 ✅ (625 paired) ② 统一特征重训 ✅ (v3 双模型) ③ backtest p_win 相关性 >0.3 ⏳ ④ shadow 72h 遥测 ⏳。
- **③④ 未达前 MetaExit 保持 shadow — 勿强行解禁。**

---

## 9. 强制休整声明

> **[FORCED_REST] P2 战役已封存。无论 P3 (XAU 实证 Micro Scaler, 43,580 达标) 有多诱人, 今夜绝不拉起新战线。**
> **完成 P2 后, 彻底脱离接触。把 v3 投入暗影, 然后去休息。**
