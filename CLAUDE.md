# CLAUDE.md

## 铁律 (Iron Law)

### 1. 每次代码修改后必须验证
- 完成任何 `.py` 文件修改后，必须运行 `python scripts/verify.py --full` 并通过
- 如果 `--full` 不通过，**不得声明工作完成**
- mypy 新错误增加 = 阻断，必须先修复再交付
- `verify.py --quick` 现在包含蓝图合规检查（Iron Law #7 自动阻断）

### 2. Ruff F821 零容忍
- F821 (undefined name) 永不豁免
- 任何未定义变量引用必须修复，不得添加 `# noqa: F821`

### 3. 类型安全基线只升不降
- `mypy_baseline.json` 每个文件的错误数只能减少不能增加
- 新增代码必须通过 mypy `check_untyped_defs` 检查
- 若需更新基线：`python scripts/pre_commit_mypy.py --update-baseline`

### 4. 提交前验证链
- `pre-commit` 自动运行 mypy + ruff + 架构文档刷新
- mypy 错误增加的提交会被物理阻断
- 绕过方式仅在极少数情况允许，且必须在提交信息中说明原因

### 5. 修复必须彻底
- 修复 bug 时，搜索同类模式确认无重复问题
- 对同一 bug 类型，考虑是否应在类型系统层面防止复发
- 如果 mypy 能捕获该 bug 类型但未配置，调整 mypy 配置

### 6. 修改前必须查阅蓝图 (Pre-Fix Protocol)
- 修改任何 `.py` 文件前，必须先确定目标模块
- 读取 `blueprints/modules/<module>.md` 的 Fix History 和 Known Issues
- 搜索 `blueprints/system/FIX_REGISTRY.md` 确认同一文件/函数是否有历史修复
- 如有历史修复，先分析根因再编码，避免重复修复同一问题
- 运行 `python scripts/analyze_deps.py <module>` 评估修改影响范围

### 7. 修改后必须注册修复 (Post-Fix Protocol)
- 分配 FIX ID：`FIX-YYYYMMDD-NNN`
- 更新 `blueprints/modules/<module>.md` 的 Fix History 表格
- 更新 `blueprints/system/FIX_REGISTRY.md` 的 Fix Index + Fix Details
- 可用 `python scripts/register_fix.py` 辅助格式化（非必须，手动 Edit 效果相同）
- 使用约定式提交格式：`<type>(<scope>): [FIX-YYYYMMDD-NNN] <description>`
- 如果修改影响到跨模块合约，同步更新依赖模块的 Cross-Module Contracts

### 8. 根因诊疗协议 (Root Cause Diagnosis Protocol)
- **发现问题后，不挖到根因不出方案，不画影响链路不写代码**
- 执行五步：**STOP**（停）→ **LOOKUP**（查蓝图/依赖/历史）→ **DIG**（至少3层追问+横向搜索）→ **MAP**（影响链路+双品种验证）→ **PLAN**（一篮子方案，ExitPlanMode 审批）
- 完整协议：`blueprints/system/ROOT_CAUSE_DIAGNOSIS_PROTOCOL.md`
- 适用门槛：改逻辑/改共享基础设施/改状态格式 → 完整五步；改配置值 → STOP+MAP；typo → 跳过但说明
- 反例参考：协议文档中记录了 FIX-022（8轮修同一bug）、BTC三连打地鼠（spread→max_spread→min_sl）、XAU shadow 两次误判

## 验证命令

```bash
# 快速验证 (mypy + ruff + 蓝图合规, ~10s)
# 每次修改 .py 后必须运行并通过（铁律 #1）
python scripts/verify.py --quick

# 完整验证 (全量 mypy + ruff + 蓝图合规 + pytest, ~2min)
python scripts/verify.py --full

# 安装 pre-commit 钩子（一次性）—— git commit 时自动触发验证
# pre-commit install

# 更新验证戳
python scripts/verify.py --full --stamp

# 检查验证戳是否有效
python scripts/verify.py --check-stamp

# 更新 mypy 基线 (类型改进后)
python scripts/pre_commit_mypy.py --update-baseline

# 蓝图验证
python scripts/validate_blueprints.py

# 依赖分析
python scripts/analyze_deps.py <module-name>

# 注册修复
python scripts/register_fix.py --help
```

---

### Iron Law #9: Agentic DQAF — 零幻觉双轨诊断协议

**触发条件**: 收到 Bug 报告、异常日志分析请求、或任何需要诊断系统异常的情境。

**核心约束**: 绝对禁止在输出 DQAF_REPORT 并获得人类 IC 批准之前修改任何代码。

**强制 4 步诊断流程**:

1. **ECoL 证据锚定**: 结论中的每一个声明必须有具体日志行号或文件路径作为支撑。孤证（单源）不得作为根因结论的唯一依据。

2. **AR 对抗反驳**: 你必须自己提出至少一个与你初始结论相反的假设，并用代码逻辑或日志证据去尝试推翻它。如果推翻失败（初始结论存活），记录为何反证无效。如果推翻成功，以存活的假设为准。

3. **CCT 因果链**: 必须写出至少 2 层传导链：症状 → 中间变量/状态异常 → 根因。每个环节标注置信度（confirmed=双源确认 / hypothesis=单源推断 / speculative=纯逻辑推理）。

4. **IRA 影响半径 & ReB 模式**: 声明此修改对 XAU 和 BTC 的差异化影响，提炼一个 Pattern Signature 供未来检索。

**标准输出格式** (必须且只能以此开头):

**🛑 强制握手协议 (The Handshake Protocol)**:
遇到以下类型的请求时，若未提供 ECoL 证据包（`dqaf_collect.py` 生成的 `.zip`），必须输出 `🛑 [DQAF_HALT]` 并拒绝开始诊断：
- 含"排查"、"为什么"、"是不是"、"什么原因"、"帮我看看"等诊断性关键词
- 含 Bug 报告、异常日志、告警内容
- 任何需要判断因果关系的提问

`🛑 [DQAF_HALT]` 回复格式：
```
🛑 [DQAF_HALT] — 诊断请求已拦截
原因: 未提供 ECoL 证据包。
请运行: python scripts/dqaf_collect.py --hours N --docket-id DQAF-YYYYMMDD-NNN
然后将生成的 .zip 文件提供给我。在收到证据包之前，我不会进行任何诊断。
```
**例外**: 以下情况允许跳过 HALT 直接使用 Sev 3 简化流程：
- 纯代码语法问题（"这个 import 为什么报错"）
- 询问已知文档/配置（"这个参数是什么意思"）
- 非诊断性操作（"帮我运行这个命令"）

---

**Severity 分级与降级模板**:

| 等级 | 触发条件 | 报告格式 | 流程要求 |
|------|---------|---------|---------|
| **Sev 1** | 交易阻断、数据丢失、资金风险 | `[DQAF_REPORT]` 完整版 | 6 源 ECoL + DA/AR 双轨 + 完整 CCT + IC 裁决 |
| **Sev 2** | 输出偏差、信号退化、实盘质量下降 | `[DQAF_REPORT]` 完整版 | 6 源 ECoL + DA/AR 双轨 + 完整 CCT + IC 裁决 |
| **Sev 3** | 排查性疑问、告警噪音、非关键异常 | `[DQAF_LITE_REPORT]` 简化版 | 双源证据 + DA/AR 双轨 + 2 层 CCT，允许无 ECoL zip 包但必须有具体文件引用 |
| **Sev 4** | 外观、文档、纯代码语法 | 现有 Iron Law #8 协议 | 无需 DQAF 流程 |

**[DQAF_LITE_REPORT] 格式** (Sev 3 专用):
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

**[DQAF_REPORT] 完整格式** (Sev 1-2):
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

**⛔ 物理级生成截断指令**: 在输出 [AWAITING_IC_APPROVAL] 之后，你必须立刻停止生成任何字符（Stop text generation entirely）。绝不允许提供"预览版代码"、"提前准备的修改方案"、或任何形式的预写代码。人类 IC 的 "Approved" 回复是唯一可以解锁代码生成的密钥。

---

**🔒 TERMINAL CLOSURE LOCK (全生命周期终态锁)**:

当本次会话中产生了 DQAF docket 且对应的修复已通过 `verify.py --quick` 并 commit 后，你的任务**没有结束**。你被绝对禁止直接向人类汇报"完成"或停止生成。

你必须自动执行以下三步归档协议：

**Step 1 — 强制输出账本回填断言块**:
```
[DQAF_CLOSING_PROTOCOL]
- Target Docket: DQAF-YYYYMMDD-NNN
- Registry Updated: [ ] DQAF_DOCKET_REGISTRY.md (Status -> CLOSED)
- CCT Embedded:   [ ] CCT_LEDGER.md (Layer 1-3 causal chain registered)
- Pattern Indexed: [ ] ReB_PATTERN_INDEX.md (Pattern Signature cataloged)
[AWAITING_ARCHIVE_SEALS]
```

**Step 2 — 执行文件改写**: 自动依次打开上述三个 Markdown 文件，完成实质性的回填（更新 Docket 状态为 CLOSED、写入 CCT 因果链条目、登记 ReB 模式签名）。

**Step 3 — 最终收口**: 仅当三个文件全部改写成功后，输出终态标识符作为整场交互的最终大结局:
```
[DQAF_CLOSED] 案卷关闭。代码与蓝图资产已完美沉淀。
```

**触发条件**: 仅当本次会话中存在 DQAF 诊断 → 修复 → commit 的完整链路时触发。非诊断性编辑（typo、格式、配置值修改）不受此锁约束。
