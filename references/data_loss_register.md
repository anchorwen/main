# 数据损失登记册 (Data Loss Register)

> **定性**: 历史不可抗力导致的数据截断 (Historical Data Truncation)
> **裁决**: IC 2026-08-08 — 放弃人工重构, 全面执行历史归档。**不补账, 不写代码**。
> **封存期**: 永久已知历史技术债 (8/19 决战前不触碰, 归档后亦不补账)。
> **证据源**: DQAF-20260807-004 全量坐实 (Iron Law #11 脚本实证) + FIX-20260807-005 审计工具修正。

---

## 一、BTC — 117 条 close-without-open 孤儿 (SEV1, 历史封存)

| 字段 | 值 |
|:---|:---|
| 数量 | **117** (identity 口径, position_identifier 不可变身份回链) |
| 时间窗 | **2026-05-31 ~ 2026-06-21** (21 个交易日, JournalGate 部署前) |
| 累计 PnL | **−$41.29** |
| 累计 volume | **2.09** |
| 备份交叉验证 | 118→117 修正: 117 条在任何 sidecar/backup 中 **0 条出现过 open 腿** (augmented/bak2/bak3/dedup/manual_fix/orphans 六备份全零) |
| ack 分布 | accepted 113 / closed 4 |

**定性**: opens 从未被 journaled — JournalGate (2026-06-21 前后) 部署前的记账缺口, "史前时代"数据截断。孤儿 close 腿本身完整 (有 PnL), 缺的是 open 腿。系统成长阵痛, 非持续泄漏 (6/22 后零新增)。

**计数修正记录**: 原坐实报告报 118。identity 口径 (pid→ticket 交叉回链, 与 `resolve_identity()` 语义一致) 精确重算为 **117** — 差异 1 条为 close 腿 `position_identifier=3946529969` 匹配 open 腿 ticket=3946529969 (open pid=None), 属正确换票回链, 非孤儿。审计工具已按 `_resolve_identity()` 口径固化 (FIX-20260807-005)。

**证据锚点**: `scripts/audit_data_chain_integrity.py` (S3_ORPHAN_CLOSE / S4_CLOSE_WITHOUT_OPEN count=117)。

---

## 二、XAU — 7 条已成交平仓缺 PnL 尸体 (SEV2, 历史封存)

| 字段 | 值 |
|:---|:---|
| 数量 | **7** |
| 时期 | **2026-05** (五月初, MT5 历史) |
| 类型 | 6 × client_close + 1 × broker client_close |
| recorded_at | **全部为空串 `''`** (无时间戳 — 终结管线早期缺陷) |
| pnl | **全部 None** (缺 PnL) |
| open 腿 | 全部真实存在 (`live_open_<hex>` 精确回链) |
| volume | 全部 0.01 |

**tickets**: 3363293291 / 3365481628 / 3363678156 / 3383549590 / 3424257573 / 3424313090 / 3424375933

**定性**: 真实成交平仓但 PnL 未入账 (与 FIX-20260807-003 的 4454299643 同病理), 但为 5 月历史遗留。IC 裁决: 不去 MT5 翻两三个月前的老账手工缝补, 历史封存。8/19 后如需可评估 MT5 历史回填 (FIX-20260807-003 Phase 3c 候选)。

**证据锚点**: `scripts/audit_data_chain_integrity.py` (S3_PNL_NULL_CORPSE count=7 dates=[''])。

---

## 三、封存声明 (Seal)

1. 上述两条疤为 **系统不可抗力的历史数据截断**, 永久归档为已知技术债。
2. **不补账, 不写代码** — 归档期内禁止任何针对这两批历史数据的重构/回填脚本。
3. 审计工具 (FIX-20260807-005) 已将其计入**修正后基线**:
   - BTC: **85🟡** (S3_ORPHAN_CLOSE/S4_CLOSE_WITHOUT_OPEN 117 仍为 fresh SEV1 — 保留观测, 但属已知封存项)
   - XAU: **92🟢** (S3_PNL_NULL_CORPSE 7 仍为 fresh SEV2 — 同上)
   - 即: 基线**如实保留**历史断层计数, 但 IC 已裁决不修; 回归门禁以 `--baseline-read` 防**新增**断层, 历史项不作为退化。
4. 若未来 (8/19 后) 决定清偿, 走新 DQAF 立项, 更新本册 + 重锁基线。

---

## 关联

- 审计工具: [scripts/audit_data_chain_integrity.py](../scripts/audit_data_chain_integrity.py)
- 基线: [gate_audit/dci_baseline_btc.json](../gate_audit/dci_baseline_btc.json) / [dci_baseline_xau.json](../gate_audit/dci_baseline_xau.json)
- 登记: FIX-20260807-005 (data_infrastructure)
- 立项: DQAF-20260807-004 Phase 0
- 同病理活例: [[dqaf_20260807_core_accounting_pipeline_fix]] (FIX-20260807-003, 4454299643 已回填)
