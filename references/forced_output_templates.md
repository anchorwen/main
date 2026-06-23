# Ω 强制输出模板 (Forced Output Templates)

> **读取指令**: 此文件是 CLAUDE.md 的延伸。当 Iron Law 场景路由激活时，
> Agent **必须在生成输出前执行**: `Read d:\future\references\forced_output_templates.md`
> 严禁使用内部记忆捏造格式。缺失标记 = 流程违规 = commit 被物理拒绝。

---

## Scene A: 修 Bug / 异常诊断

```
[Ω-Routing: Scene A → #9 → #8 → #12 → #6 → #5]

=== IRON LAW #9: DQAF REPORT ===
[DQAF_LITE_REPORT] 或 [DQAF_REPORT]
- Docket ID: DQAF-YYYYMMDD-NNN
- Severity: Sev 1/2/3/4
- Evidence (双源): ...
- DA Diagnosis: ...
- AR Adversarial Check: ...
- Causal Chain (2-3层): ...
- Blast Radius (XAU/BTC): ...
[AWAITING_IC_APPROVAL]

=== IRON LAW #12: ROOT CAUSE LAYER ===
Root Cause Layer: L1 | L2 | L3
Fix Level Match: YES | NO (PATCH_NOT_ARCHITECTURE: <reason>)

=== IRON LAW #6: BLUEPRINT CHECK ===
Module: <module_name>
Blueprint: blueprints/modules/<module>.md
FIX_REGISTRY: searched, <N> related fixes found

=== IRON LAW #5: PATTERN SEARCH ===
Pattern: <pattern>
Results: <N> matches across <files>

[PRE-EDIT CHECKLIST — Iron Law #0]
1-5 逐项论证
[CHECKLIST PASSED]
```

## Scene B: 改代码 / 添功能

```
[Ω-Routing: Scene B → #0 → #6 → #5]

[PRE-EDIT CHECKLIST — Iron Law #0]
1-5 逐项论证
[CHECKLIST PASSED]

=== IRON LAW #6: BLUEPRINT CHECK ===
Module: <module_name>
Blueprint: blueprints/modules/<module>.md

=== IRON LAW #5: PATTERN SEARCH ===
Pattern: <pattern>
Results: <N> matches
```

## Scene D: 分析实盘数据

```
[Ω-Routing: Scene D → #11]

=== IRON LAW #11: SCRIPT OUTPUT (唯一合法证据源) ===
<script stdout>
[DONE] All statistics above are the sole source of truth.
```

## Scene E: 新建文件/模块

```
[Ω-Routing: Scene E → #6 → #0]

=== IRON LAW #6: MODULE REGISTRATION ===
Module: <module_name>
Blueprint: blueprints/modules/<module>.md (新建/更新)
MODULE_SOURCE_MAP: check_blueprint_compliance.py 已更新
```

## 提交模板 (All Scenes)

```
Stability: ↑/→/↓ (assessment)
Repairability: ↑/→/↓ (assessment)
Decoupling: ↑/→/↓ (assessment)
Iterability: ↑/→/↓ (assessment)
```

---

## DQAF 报告格式 (from Iron Law #9)

### [DQAF_LITE_REPORT] 格式 (Sev 3 专用)

```
[DQAF_LITE_REPORT]
- Docket ID: DQAF-YYYYMMDD-NNN
- Severity: Sev 3
- Trigger: [触发原因——一句话]
- Evidence (简化的双源证据):
  - Source 1: [具体文件路径/行号]
  - Source 2: [另一独立来源]
- DA Diagnosis: [症状→推断]
- AR Adversarial Check: [反向假设 + 验证]
- Causal Chain (简化 2 层):
  - [Layer 1 — 症状]:
  - [Layer 2 — 根因]:
- Blast Radius: [XAU/BTC 影响]
- Proposed ReB Pattern: [模式签名或引用已有]
[AWAITING_IC_APPROVAL]
```

### [DQAF_REPORT] 完整格式 (Sev 1-2)

```
[DQAF_REPORT]
- Docket ID: DQAF-YYYYMMDD-NNN
- Severity: Sev 1/2/3/4
- Evidence (硬证据): [具体日志行/文件路径/代码位置]
- DA Diagnosis (初始诊断): [症状→推断→根因假设]
- AR Adversarial Check (对抗反驳): [反向假设 + 验证结果]
- Causal Chain (因果链): [Layer 1 症状 → Layer 2 中间异常 → Layer 3 根因]
- Blast Radius (影响半径 XAU/BTC): [双品种差异化影响]
- Proposed ReB Pattern: [模式签名]
[AWAITING_IC_APPROVAL]
```

### 🛑 [DQAF_HALT] 回复格式

```
🛑 [DQAF_HALT] — 诊断请求已拦截
原因: 未提供 ECoL 证据包。
请运行: python scripts/dqaf_collect.py --hours N --docket-id DQAF-YYYYMMDD-NNN
然后将生成的 .zip 文件提供给我。在收到证据包之前，我不会进行任何诊断。
```
