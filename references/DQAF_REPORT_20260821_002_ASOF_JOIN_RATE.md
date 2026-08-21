# DQAF-20260821-002 — XAU asof_join_rate 22.3% 过低 (The As-of Denominator)

> **状态**: ✅ **CLOSED — FIX-20260821-007 (IC 雷霆裁决 The Denominator Alignment, 2026-08-21)** — 82.9% 的真理已夺回, XAU 训练就绪评估转绿 (asof_join_rate 82.9% PASS + pnl_completeness 0.0% PASS). 三步归档: DQAF_DOCKET_REGISTRY CLOSED / CCT-20260821-003 / ReB `METRIC_DENOMINATOR_SEMANTIC_SHIFT`. 次生: TECH_DEBT-022 独立建档 (特征断供无强告警, Sev 3, 留待排期).
> **取证脚本**: `scripts/_audit_asof_join_miss_20260821.py` (Iron Law #11, stdout 唯一合法证据)
> **交叉验证**: `scripts/build_btc_metafilter_v2_dataset.py` 真实运行输出 (builder 自证)
> **未改任何业务逻辑代码** — 本次仅取证 + 建档 (TECH_DEBT-021)

---

## [DQAF_REPORT]

- **Docket ID**: DQAF-20260821-002
- **Severity**: Sev 2 (数据管道输出偏差 — readiness 评估对训练数据可用性给出确定性误报, 掩盖真实状态)
- **Trigger**: TECH_DEBT-020 (FIX-20260821-006) 清偿后, readiness 首次真实评估 XAU xau_metafilter_v1 显示 `asof_join_rate: 22.3% (1046/4697) < 80%` FAIL + `pnl_completeness: 677/4697 null PnL (14.4%)` FAIL → IC 行动令 P1.5: 取证先行, 数据自己说话.

### Evidence (硬证据, 全部脚本 stdout)

**E1 — builder 自身输出 (权威口径, 非复刻)**:
```
Journal: 1695 tickets, 1262 with PnL (406 with signal p_win)
ASOF join: 1046 matched, 0 no prior feature, 211 stale (gap > 15min), 5 not-yet-known, 0 missing data
```
→ 真实 asof_join_rate = **1046/1262 = 82.9% ≥ 80% 门槛**. 数据集本可构建 (40 维 / 1046 样本 / 标签 39.2% / LONG 53% SHORT 47%).

**E2 — 取证复刻逐字节吻合**: `_audit_asof_join_miss_20260821.py` 复刻 builder 逐行逻辑 → 同一 `1046 matched / 0 future / 211 stale / 5 not-known`, 1262 条含 PnL 交易. 结论: **join 逻辑零缺陷**.

**E3 — 22.3% 假象根因 (度量分母缺陷)**:
```
asof_rate = n_samples / journal_closed            (check_training_readiness.py:846)
journal_closed = count(ack_status == "closed")    (_count_journal_closed, :1031) = 4697
journal 结构: 8648 行 → closed 条目 4697 (677 空 PnL + 孤儿/无 ticket + 每票重复 close)
             → 1654 票有 close → 仅 1262 票含 PnL = builder 真实 join 宇宙
```
分母把"原始 closed 条目"当作"交易", 放大 4697/1262 = **3.72×** → 82.9% 被压成 22.3%.

**E4 — 假设检验 (IC 三候选逐一实测)**:
| 假设 | 裁决 | 证据 |
|:---|:---|:---|
| ①时区撕裂 (Label 本地时 vs Feature UTC 小时级偏移) | ❌ **否** | label `recorded_at` naive 秒精度; feature `event_time` `+00:00` 微秒; builder `[:26]` 截断恰丢弃 offset → naive → 补 UTC (巧合正确); 匹配 gap p50=**5s** max=634s, 全 ≤15min, 零小时级偏移 |
| ②精度不对齐 (ms/s, datetime64 截断) | ❌ **否** | 匹配 gap 秒级干净; feature 均 5-min bar 边界 (event_time `:00.061000`), label 对齐 bar 网格 |
| ③前瞻守卫过杀 (tolerance 过严 / backward 无前置) | ⚠️ **半真** | tolerance 15min 对**真实断供**正确拒签; FUTURE=0 (无"label 早于整个 feature store"); 但 211 STALE 中 5.5 天断供窗是真实数据缺口非守卫误杀 |

**E5 — 211 STALE (16.7%) 真实断供残留**:
```
feature 连续间隔: p50=300s (正常 M5) | p90=1500s | max=2326200s (~27 天, 03-05→04-01 建库前)
>6h 断供窗 ×59:  05-18T10:10→05-23T22:02 = 131.9h (5.5 天) + 每周五 24h 周末缺口 (05-23/05-29/05-30/06-05/06-06/06-12/06-13/06-19...)
STALE gap p50=158730s ≈ 1.8 天
```
→ 这些交易开仓时系统**真实无近期特征**, Iron Law #3 拒签按设计工作. 会减少样本 (1046 已达标), 是数据质量残留, 非 join bug.

**E6 — 5 NOT_KNOWN (0.4%)**: 05-04/05-05 开仓, 邻近 feature ingested 0.1-0.6 天**后**于 label (05-05 07:03 整库回填) → 知识时间过滤正确. 边界可忽略.

### DA Diagnosis (初始诊断)

症状 (readiness 报 22.3% FAIL) → 推断 (77.7% 标签丢在时间缝隙) → 假设 A: 时区错位; 假设 B: 精度截断; 假设 C: 守卫过杀. 取证脚本全量复刻 join 后实测: **假设 A/B 双双证伪, C 半真, 真根因在度量侧分母语义**, 而非 join 侧时间对齐.

### AR Adversarial Check (对抗反驳)

- **反向假设 1**: "我的 1262 复刻漏读了标签, 真标签是 4697". → **推翻**: builder 自身输出 `Journal: 1695 tickets, 1262 with PnL` 与复刻逐字节一致; 4697 是 `ack_status=="closed"` 原始条目 (含 677 空 PnL + 孤儿 + 重复), 非"可标签交易". builder 的 y 需要 pnl, 无 pnl 无标签 → 1262 才是合法 label 宇宙.
- **反向假设 2**: "82.9% 是宽松口径, 严格口径应含孤儿 close". → **部分成立但指向度量而非 join**: 若投委会想要"覆盖所有 closed 条目"的更严口径, 正确的分母也应是**去重交易** (1654 票), 而非原始条目 (4697); 更且孤儿/空 PnL close 根本不是 builder 的 join 对象 — 现实现实是度量实现错误, 非口径选择.
- **反向假设 3**: "STALE 是 tolerance 太严 (900s), 放宽即可回收". → **推翻**: STALE gap p50=1.8 天, 放宽到 60min 仅回收 ~5 条 (min 1201s), 主体是 5.5 天断供窗 — 宽容差只会灌入陈旧特征污染标签 (正是 Iron Law #3 要防的). 修复点在**数据供应断供**, 不在容差.

### Causal Chain (因果链)

- [Layer 1 — 症状]: readiness 评估 XAU 报 `asof_join_rate 22.3% (1046/4697) < 80%` FAIL + `pnl_completeness 677/4697 null (14.4%)` FAIL → 训练数据"看起来不可用".
- [Layer 2 — 中间异常]: 度量分母用了 `_count_journal_closed` 的**原始 closed 条目数 (4697)** — 混入 677 空 PnL close、孤儿/无 ticket close、每票重复 close; 而 builder 的 join 宇宙是**去重含 PnL 交易 (1262)**. 分母放大 3.72× → 确定性低值.
- [Layer 3 — 根因]: **L2 逻辑缺陷 (RC-06 metric-semantics)** — readiness 的 `asof_join_rate`/`pnl_completeness` 分母语义与 builder 实际 join 口径不一致, 度量未收敛到系统唯一的"交易"定义 (position_ticket 去重 + 含 PnL). 非数据时间错位.

### Blast Radius (影响半径 XAU/BTC)

| 维度 | XAU | BTC |
|:---|:---|:---|
| 度量假象 | readiness 恒 FAIL asof_join_rate 22.3% → 训练数据误判不可用 (训练闸门假阻断) | 同度量 (BTC v3 契约, data_btc) — 若 BTC 也有孤儿/空 PnL close 同被放大; 需同查 |
| 真实 join 率 | **82.9% ≥ 80%** — 数据集本可构建 | 未取证, 同口径待核 |
| 211 STALE | 真实断供 (05-18 131.9h + 周末 24h), 减样本不污标签 | 未知, BTC crypto_24_7 无周末缺口, 风险低 |
| 资金风险 | **零** — 纯度量/训练侧, 不触交易执行 | 零 |

### Proposed ReB Pattern

`METRIC_DENOMINATOR_SEMANTIC_SHIFT` — 度量若用与生成器/消费者**不同语义的基数** (原始事件条目 vs 去重业务实体), 会产生确定性比例失真, 把"达标"伪报成"灾难"或反之. 预防: 度量分母必须与业务实体定义 (position_ticket 去重 + 关键字段非空) 收敛; 生成器自报口径应作为对照. (待 IC 裁决后正式登记)

### 修复方向 (待裁决, 本次零改动)

1. **主修 (L2 修复, 最小面)**: `check_training_readiness.py` 的 `asof_join_rate`/`pnl_completeness` 分母从"原始 closed 条目数"改为"去重含 PnL 交易数" (与 builder join 宇宙一致) — 或同时暴露两个口径 (raw entries / distinct trades) 避免历史口径丢失.
2. **次修 (可选, 数据质量债)**: readiness 增加 feature 断供窗 (≥6h) 检测告警 — 05-18 131.9h 断供是真实事件, 应被监控而非静默. 可单独立债.
3. **对照组**: BTC 同口径核查 (data_btc asof_join_rate 是否同样被分母放大).

---

## IC 裁决 & 执行记录 (2026-08-21, 雷霆裁决 The Denominator Alignment)

**投委会三条裁决 (verbatim 精神)**:
1. **度量分母对齐 (The Denominator Alignment)** 🟢 绝对批准 — "让 Builder 在其生成的元数据报告 (Report JSON) 中, 显式吐出 valid_trades_count, 让 Readiness 脚本直接读取这个权威数字作为分母, 彻底实现 Single Source of Truth." **执行令**: "立即执行修复。务必让那刺眼的 22.3% 恢复到它应有的 82.9% 绿色达标态。"
2. **历史断层建档 (The Continuity Gap)** 🟢 批准独立建档 **TECH_DEBT-022** (特征流长时间断供无强告警机制, Sev 3, 监控管道) — "绝不并案", "今夜不修, 留待后续排期".
3. **最高开火令 (The Green Light)** — 修复 + 收口 P1.5/TECH_DEBT-021 (附取证报告) 后, 直接拉起 **P2 战役 (MetaExit 门禁复查与统一特征重训)**.

**已执行**:
- **FIX-20260821-007** (三腿): ① Builder Report JSON SSOT — `build_btc_metafilter_v2_dataset.py` main() 落 `*.report.json` 边车 (journal: valid_trades_count=1262 / real_closed=1238 / manual_close=416 / orphan=7; asof: matched=1046 / stale=211 / not_known=5); ② readiness `asof_join_rate = n_samples / report.valid_trades_count` (report 缺失 → 本地去重含 PnL 交易数回退, 显式 fallback); ③ 阶段2 全度量改 distinct 宇宙 + 排除 manual_close/orphan → pnl_completeness 0/1238 = 0.0%.
- **验收** (真实 XAU 就绪评估): asof_join_rate **82.9% (1046/1262) PASS** ≥ 80% + pnl_completeness **0.0% PASS** ≤ 5% + closed_trade_count 1661 ≥ 200 + sample_count 1046 ≥ 500 + 标签 39.2% ≥ 15%. **目标全绿**. 残余 WARN = 既有 `feature_quality_outliers` (原始 ATR/MACD/RSI >20, 数据特性非分母问题, 越本 docket 授权范围).
- **回归锁**: +3 新测试 (SSOT 读 / fallback / manual_close 排除), 测试文件 10/10 passed.
- **取证资产**: `scripts/_audit_asof_join_miss_20260821.py` + `scripts/_audit_journal_universe_20260821.py` (hash-lock 豁免, 留工作树).
- **三步归档**: DQAF_DOCKET_REGISTRY CLOSED → CCT-20260821-003 → ReB `METRIC_DENOMINATOR_SEMANTIC_SHIFT`.

[DQAF-CLOSED] DQAF-20260821-002 案卷关闭 — 22.3% 假象根治, 82.9% 真理回归. 取证报告即本文件 (知识库资产).
