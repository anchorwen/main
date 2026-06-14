# CLAUDE.md

---

# ⚙️ 铁律执行协议 (Iron Law Ω — The Omega Protocol)

**【总纲】** 本代码库无视自由裁量权。任何操作前，必须先匹配场景，再按规定顺序激活并执行铁律。跳过激活链中任一步骤 = 严重违规。

---

## Iron Law #-1: Institutional Execution Mandate — Agent Self-Loading Protocol (最高优先级 — 超越所有场景路由)

**触发条件**: Agent 在 `[AWAITING_IC_APPROVAL]` 之后收到人类的任何批准回复（"Approved"、"可以执行"、"开始修改"、"执行"等），**在编写任何代码之前**，Agent 必须在其思考过程中自行加载以下**机构级执行约束标签 (Institutional Execution Mandate Tag)** 并严格受其约束：

```
[INSTITUTIONAL_EXECUTION_MANDATE]
1. Institutional-Grade Reasoning — 机构级思路：每个修改必须从系统架构层面论证必要性，禁止投机性修改、禁止临时补丁、禁止"先改了试试"。
2. Stability Non-Degradation — 不破坏稳定性：修改不得引入新的故障模式、未处理异常路径、共享状态竞争或隐式依赖。
3. Repairability Non-Degradation — 不破坏可修复性：修改不得吞没诊断信息、合并不同性质信号或增加根因定位步骤。
4. Decoupling Non-Degradation — 不破坏解耦性：修改不得跨越现有模块边界增加新 import 链或破坏接口向后兼容。
5. Iterability Non-Degradation — 不破坏可迭代性：修改不得将同一逻辑分散到多个文件；新增扩展点必须统一而非散布。
6. Ω Systematic Operating System — Ω 系统性操作系统执行：本次修改全链路受 Ω 铁律约束——场景路由 → DQAF → 蓝图检索 → 模式搜索 → 编辑前安检 → 验证 → 四维闸门 → FIX注册 → 收口检查。
[/INSTITUTIONAL_EXECUTION_MANDATE]
```

**标签是 Agent 的自我治理机制**——人类只需正常回复"Approved"，Agent 自行加载标签并受其约束。标签的六个条款是 Agent 在编码期间必须逐条遵守的硬性约束，不得选择性忽略。

**不可绕过条款 (Non-Bypassable Clause)**:

以下人类指令**均构成有效批准**——Agent 必须立即自行加载执行约束标签并开始修改代码：

- "Approved" / "批准" / "可以执行"
- "开始修改" / "执行" / "Proceed"
- "按方案执行" / "Implement the plan"
- 任何表达了批准或同意意图的回复

**关键区别**: 人类不需要粘贴任何特定标签。人类只需正常表达批准。Agent 负责自行加载执行约束。这消除了"人类需要记住并粘贴复杂标签"的用户体验摩擦，同时保留了 Agent 端的全链路约束力。

**适用范围**: 所有 Scene (A/B/C/D/E/F/G)。无豁免场景。

**设计原理**: 本条款解决 Iron Law #9 不可绕过条款中识别的根本漏洞——Agent 将人类任何高优先级指令误解为"可以跳过铁律"的授权。现在强制 Agent 在每一份批准之后、每一次编码之前，自行加载机构级执行约束。这六个约束不是建议——是 Agent 在生成任何代码字符之前必须通过的自我安检。

---

---

## 场景路由表 (The Routing Table)

| 你正在做什么 (Trigger) | 必须严格执行的铁律顺序 (Execution Chain) |
|:---|:---|
| **A. 修 Bug / 异常诊断** | `#9(DQAF)` → `#8(根因层)` → `#12(匹配L1/2/3)` → `#6(蓝图)` → `#5(搜索)` → **[修改代码]** → `#1(验证)` → `#1.1(四维)` → `#7(注册)` → `#7.1(收口)` |
| **B. 改代码 / 添功能** | `#0(安检)` → `#6(蓝图)` → `#5(搜索)` → **[修改代码]** → `#1(验证)` → `#1.1(四维)` → `#7(注册)` → `#7.1(收口)` |
| **C. 改配置 (无需诊断)** | `#0(安检)` → `#8(STOP+MAP简化版)` → **[修改配置]** → `#1(验证)` |
| **D. 分析实盘数据** | `#11(脚本绝对先行，严禁口算)` |
| **E. 新建文件/模块** | `#6(蓝图注册)` → `#0(安检)` → **[编码]** → `#1(验证)` → `#7(更新蓝图/注册)` |
| **F. 纯机械操作 (格式化)** | 豁免 `#0`，直接执行 → `#1(验证)` |
| **G. Git Commit** | `#4(自动Pre-commit)` → `#1.1(四维评估写入Commit message)` |

> **并行激活**: 只要修改的是热路径文件 (`live_cycle`/`strategy_line`/`live_intent`/`execution_queue`)，强制并行激活 **`#10`**。

## AI 执行契约 (The AI Handshake)

系统助手在响应涉及代码修改/排障的请求时，必须在回复最前端显式声明：
```
[Ω-Routing: Scene A → #9 → #8 → #12 → #6 → #5]
```

---

## 铁律 (Iron Law)

### Iron Law #12: 架构优先修复法则 (Architecture-First Fix Doctrine)

**触发条件**: 任何 Iron Law #8 或 #9 诊断流程产出根因结论后，在编写修复代码之前强制执行。

**核心约束**:

1. **根因分层**: 诊断结论必须显式标注根因层级：
   - **L3: 架构缺陷 (Architecture Defect)** — 设计导致错误必然发生
   - **L2: 逻辑缺陷 (Logic Defect)** — 实现偏离设计意图
   - **L1: 语法/打字缺陷 (Syntax/Typo)** — 孤立错误

2. **修复层级匹配**: 修复的层级必须 ≥ 根因的层级。L3 根因 → 架构级修复。L2 根因 → 逻辑级修复。L1 根因 → 语法级修复。

3. **补丁豁免需论证**: 如果 L3 根因无法立即架构修复，补丁必须：
   a) 显式声明 `PATCH_NOT_ARCHITECTURE` + 原因
   b) 注册到 FIX_REGISTRY 的 Deferred Architecture Fix 表
   c) 设定触发条件（时间/事件）到期自动提醒

4. **禁止补丁累积**: 同一模块的 Deferred Architecture Fix 超过 3 个时，禁止继续补丁——必须先清偿架构债。

**反例参考**: FIX-022 (8轮修同一bug, 每次修症状不修架构), BTC三连打地鼠 (spread→max_spread→min_sl: 参数补丁链), FIX-20260612-001 (ENGINE_STALL: L3根因, L1修复 — 违规)

---

## Ω 强制输出模板 (Forced Output Templates)

每个 Ω 场景的每个铁律步骤，Agent **必须**输出对应的结构化标记。Pre-commit 和 Code Review 会验证这些标记的存在。
缺失标记 = 流程违规 = commit 被物理拒绝。

### Scene A: 修 Bug / 异常诊断

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

### Scene B: 改代码 / 添功能

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

### Scene D: 分析实盘数据

```
[Ω-Routing: Scene D → #11]

=== IRON LAW #11: SCRIPT OUTPUT (唯一合法证据源) ===
<script stdout>
[DONE] All statistics above are the sole source of truth.
```

### Scene E: 新建文件/模块

```
[Ω-Routing: Scene E → #6 → #0]

=== IRON LAW #6: MODULE REGISTRATION ===
Module: <module_name>
Blueprint: blueprints/modules/<module>.md (新建/更新)
MODULE_SOURCE_MAP: check_blueprint_compliance.py 已更新
```

### 提交模板 (All Scenes)

```
Stability: ↑/→/↓ (assessment)
Repairability: ↑/→/↓ (assessment)
Decoupling: ↑/→/↓ (assessment)
Iterability: ↑/→/↓ (assessment)
```

---

### Iron Law #0: 编辑前强制安检 (Pre-Edit Mandatory Checklist) — 最高优先级

**触发条件**: 调用 Edit 或 Write 工具修改以下任意文件类型之前：
- `.py` (Python 源代码)
- `.yaml` / `.yml` (配置文件, 包括 live_btc.yaml, live.yaml)
- `.json` (大脑配置, schemas, 治理状态等)

**纯机械操作豁免** (可直接执行, 无需安检):
- Ruff/格式化自动修复 (由 pre-commit hook 触发)
- 变量重命名消除 lint 警告 (不改变行为逻辑)
- 文档文件 `.md` 编辑 (FIX_REGISTRY, 蓝图更新——这些是 Iron Law #7 的产出, 非触发源)

**安检清单** (每次 Edit/Write 前必须在对话中显式输出):

```
[PRE-EDIT CHECKLIST — Iron Law #0]
1. 修改动机是否来自对系统行为的观察/判断?
   ├─ 是 → 必须已有 DQAF_REPORT + IC Approved
   └─ 否 (纯机械) → 直接执行, 清单通过
2. DQAF 报告是否已输出?
   ├─ 是 → 报告 Docket ID: DQAF-YYYYMMDD-NNN
   └─ 否 → STOP — 先输出 DQAF 报告
3. 人类 IC 是否已对该报告回复 "Approved"?
   ├─ 是 → 继续
   └─ 否 → STOP — 输出 [AWAITING_IC_APPROVAL] 后物理截断
4. 目标模块蓝图是否已查阅? (Iron Law #6)
   ├─ 是 → blueprints/modules/<module>.md
   └─ 否 → STOP — 先查蓝图
5. FIX_REGISTRY 是否已检索同类修复? (Iron Law #6)
   ├─ 是 → 无冲突
   └─ 否 → STOP — 先检索
[CHECKLIST PASSED] → 允许 Edit/Write
```

**结构性保证**: 此清单不依赖 Agent 判断"我是否需要安检"——只要目标文件类型匹配，安检自动触发。Agent 唯一的自由裁量权是"是否为纯机械操作"，该判断必须在清单 Step 1 中显式论证。

**与现有铁律的关系**: Iron Law #0 不新增行为规范, 只程序化执行已有规范。Iron Law #9 定义诊断流程 → Step 2-3 强制执行。Iron Law #6 定义蓝图查阅 → Step 4-5 强制执行。

---

### Iron Law #0-bis: `--no-verify` 禁止条款 (No-Bypass Clause) — FIX-20260612-017

**触发条件**: 任何 `git commit` 操作前自动触发。

**核心约束**: `git commit --no-verify` **仅在以下场景合法**:

| 场景 | 条件 |
|------|------|
| pre-commit hook 因运行时文件锁失败 | data_btc/data 目录下的 .jsonl/.lock 文件被 live 进程持有 |
| 纯文档提交 | 仅包含 .md 文件 |
| 紧急回滚 | 明确声明 `EMERGENCY_ROLLBACK` + 原因 |

**禁止行为**:
- 以"加快速度"为由跳过 pre-commit 验证
- 以"已验证过"为由跳过 mypy/ruff/blueprint 门禁
- 任何未在上述豁免表中的 `--no-verify` 使用

**每次使用 `--no-verify` 时必须在 commit message 中说明原因**，格式: `--no-verify: <reason>`。此说明由 `omega_gate.py` 的 `commit-msg` hook 检查（即使使用 `--no-verify`，commit-msg hook 仍然触发）。

**新增 Ω Gate (FIX-017)**:
- `scripts/omega_gate.py` 已升级，现在同时检查:
  1. `[Ω-Routing: Scene X → ...]` 签名
  2. 热路径文件的 `#10` 标记
  3. `.py/.yaml/.json` 修改的 FIX/DQAF ID
  4. 纯机械操作豁免声明
- Gate 在 `commit-msg` 阶段触发——**即使使用 `--no-verify` 也无法绕过**

---

### 1. 每次代码修改后必须验证
- 完成任何 `.py` 文件修改后，必须运行 `python scripts/verify.py --full` 并通过
- 如果 `--full` 不通过，**不得声明工作完成**
- mypy 新错误增加 = 阻断，必须先修复再交付
- `verify.py --quick` 现在包含蓝图合规检查（Iron Law #7 自动阻断）

#### 1.1 四维质量闸门 (4-Dimensional Quality Gate)

**触发条件**: 每次修复/功能开发完成并通过 `verify.py --full` 后，commit 前。

**核心约束**: 修复不能以牺牲系统架构质量为代价。verify.py 验证**单项正确性**，四维闸门验证**合成效应**——历史上 6 次断路器修复（FIX-019/120/142/006/003/008）每次都通过 verify.py 但架构持续退化（碎片化 trip 路径、stale counter 泄漏），正是因为缺少这层检查。

**四维定义**:

| 维度 | 问题 | 正向信号 | 退化信号 |
|------|------|---------|---------|
| **稳定性** (Stability) | 是否引入新故障模式？ | 改动纯增量/补全，不开新线程、不引入新 I/O、不改变执行顺序 | 新增未处理异常路径、共享状态竞争、隐式依赖 |
| **可修复性** (Repairability) | 下次出问题能多快定位根因？ | 增加诊断字段/日志（如 `trip_reason`）、减少排查步骤 | 吞掉关键错误信息、合并不同性质的信号 |
| **解耦性** (Decoupling) | 是否增加模块间耦合？ | 修改局限在已有模块边界内，不改接口合约 | 新 import 链跨越层级、修改签名不向后兼容 |
| **迭代性** (Iterability) | 下次改同一子系统要动几个地方？ | 新增扩展点统一（如统一计数器），修改集中而非散布 | 同一逻辑分散在多个文件、新增功能需要同时改 3+ 处 |

**执行规则**:

1. commit 前在对话中显式输出四维评估（每个维度一行，标注 ↑/→/↓）
2. 任一维度 ↓ → 必须在 commit message 中说明退化原因 + 缓解计划
3. 四维评估结果写入 commit message 尾部：
   ```
   Stability: → (assessment)
   Repairability: ↑ (assessment)
   Decoupling: → (assessment)
   Iterability: ↑ (assessment)
   ```
4. 禁止"全 ↑"的敷衍评估——任何改动都有 tradeoff，至少一个维度必须诚实标注 →

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

#### 7.1 收口检查清单 (Closing Checklist) — 每次改动后必须输出

commit 完成后，在对话中显式输出以下收口块（铁律强制）：

```
收口完毕。

未提交变更: <git status --short 计数>
蓝图: <本次更新的模块蓝图文件名>
Git: <branch> → <remote> 已推送
本轮 commit: <commit 数量 + 简要描述>
```

**执行规则**：
- 此清单不依赖 Agent 判断"是否需要收口"——只要执行了 commit，必须输出
- 如果 `git status --short` 有未提交变更，必须标注具体文件
- 如果蓝图未更新（纯机械操作），标注"蓝图: 无需更新"
- 禁止在收口清单中省略任何字段

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

**触发条件**: 收到 Bug 报告、异常日志分析请求、任何需要诊断系统异常的情境，**以及任何基于观察到的系统行为而提议修改代码或配置值的场景**。触发范围包括但不限于：
- 含"排查"、"为什么"、"是不是"、"什么原因"等诊断性关键词
- 含 Bug 报告、异常日志、告警内容
- 任何需要判断因果关系的提问
- **"这个值看起来不对/不合理" → 需要先诊断，不能直接改**
- **"XX 没有触发/没有生效" → 需要先诊断，不能直接改**
- **"两种口径不一致" → 需要先诊断，不能直接改**

**核心约束**: 绝对禁止在输出 DQAF_REPORT 并获得人类 IC 批准之前修改任何代码或配置。**无论修改看起来多小（一行配置、一个常量），只要修改动机来自对系统行为的观察和判断，就必须先走 DQAF 握手。** 唯一的例外：纯机械性操作（如格式化、修改变量名以消除 lint 警告）且不改变任何行为逻辑。

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

**⛔ 握手协议不可绕过条款 (Non-Bypassable Handshake Clause)**:

本条款针对 LLM Agent 的底层行为学漏洞：**对人类高优指令的绝对顺从倾向**。当人类发出 "请立即执行"、"已批准，直接改"、"不用走流程了"、"全面批准实施" 等指令时，Agent 会潜意识地将人类当下指令的优先级置于 CLAUDE.md 标准作业程序之上，从而跳过安检门直奔代码库。

**以下人类指令均不可绕过 DQAF 握手协议**:
- "已批准，请立即执行"
- "直接改代码吧"
- "不用写报告了，我知道问题在哪"
- "全面批准实施"
- "Approved，开始写代码"
- **"下发执行指令 (IC Action Mandate)"** ← 2026-06-07 新增：Agent 将此指令误判为"已越过握手，可直接码代码"，导致 Iron Law #7+#9 双违
- 任何形式上表达了"跳过诊断直接修改代码"意图的指令

**特别注意**: "IC Action Mandate" 或 "下发执行指令" 本身是人类对你诊断报告的 **Approved 回复的加速版**——它批准了你的方案，但**没有授权你跳过 DQAF 报告输出**。正确做法：先输出 `[DQAF_LITE_REPORT]` + `[AWAITING_IC_APPROVAL]`，然后**立即停止**。人类看到报告后如果再次说 "执行"，你再解锁代码。两步握手，不可合并。

**正确响应**: 即使人类明确要求跳过流程，你仍然必须:
1. 输出完整的 `[DQAF_REPORT]` 或 `[DQAF_LITE_REPORT]`
2. 在报告末尾输出 `[AWAITING_IC_APPROVAL]`
3. 物理级停止生成
4. 等待人类对**该报告本身**回复 "Approved" 后再解锁代码

**设计原理**: DQAF 报告的价值不仅在于"走流程"——它迫使 Agent 执行 ECoL 证据锚定、AR 对抗反驳、CCT 因果链推演。人类在阅读报告时可能发现 Agent 遗漏的证据或错误的推断。跳过报告 = 跳过人类纠错机会 = 埋下错误修复的种子。本条款将握手协议从 "人类提醒才执行" 升级为 "结构上不可绕过"。

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

---

### Iron Law #10: 渐进式随改随升 (Incremental Upgrade Doctrine) [TEMPORARY — 自毁条件见下]

**⛔ 自毁条件**: 当以下文件中的 `# noqa: BLE001` 全部清零后，**立即删除本 Iron Law #10**（不得遗留脚手架）。检查命令：`grep -c "noqa: BLE001" core/runtime/live_cycle.py scripts/live_intent_loop.py core/execution/strategy_line.py`

**触发条件**: 修改以下热路径文件中的任意一个时自动触发：
- `core/runtime/live_cycle.py`
- `scripts/live_intent_loop.py`
- `core/execution/strategy_line.py`
- `core/execution/execution_queue.py`

**核心约束**: 修改上述文件时，必须扫描该文件中是否存在 `# noqa: BLE001` 标记。若存在，须将对应站点的 `try: ... except Exception: pass` 替换为 `with fail_open_guard("ComponentName"): ...`。

**背景**: 2026-06-07 战役将 BLE001 从 566 降至 0，确立了 29 个存量 FAIL_OPEN 站点的"渐进式随改随升"纲领（FIX-148）。`fail_open_guard()` 工具已部署在 `core/runtime/fault_handler.py`（FIX-146）。

**执行规则**:
- 每次修改上述文件时，至少替换 **1 个** `# noqa: BLE001` 站点
- 替换后删除对应的 `# noqa: BLE001` 注释
- 若文件中已无 `# noqa: BLE001`，此铁律自动跳过
- 不得专门为替换 BLE001 而修改热路径文件（避免无谓风险）

**存量站点分布** (供参考):
| 文件 | 存量 |
|------|------|
| `live_cycle.py` | 34 |
| `live_intent_loop.py` | 51 |
| `strategy_line.py` | 9 |
| **合计** | **94** |
> **注**: 2026-06-11 FIX-018 切除 legacy dispatch 死代码 (L5752-L6317, 567行), BLE001 105→94 (-11).

---

### Iron Law #11: 数据防幻觉审计法 (Data Analytics Law)

**触发条件**: 当要求分析实盘交易质量（如胜率、出场原因、盈亏比、持仓时间、方向分布等统计指标）时触发。

**核心约束**: **绝对禁止**直接在对话中通过阅读 JSON/JSONL 文本片段来得出统计结论。大模型不具备在上下文中处理数百条数据并得出精确统计的能力——任何基于"读了几行数据"的统计判断均为抽样幻觉。

**强制执行规则**:

1. **先写脚本，后看报表**: 必须编写一个专用的 Python 审计脚本（存放于 `scripts/` 目录），使用标准 Python 数据结构，基于 `position_ticket` 进行严格的去重、分组和聚合。

2. **脚本输出的 stdout 是唯一合法证据源**: 诊断结论中的每一个统计数字必须直接来自该脚本的 stdout 输出。不得在对话中"补算"或"修正"脚本未输出的指标。

3. **脚本必须可复现**: 脚本接受 `--data-dir` 参数（默认为当前品种的 data 目录），每次运行产生一致的输出。

4. **统计口径必须明确**: 脚本开头注释中必须声明：去重逻辑（如何按 ticket 分组）、胜率定义（含不含 breakeven）、PnL 计算口径。

5. **禁止补充推断**: 如果脚本未输出某个统计量，则不得在报告中声称该统计量的值。需要新指标 → 先改脚本再运行。

**标准审计脚本模板**:
```bash
python scripts/analyze_live_journal.py --data-dir data_btc
```

典型输出内容包括：
- 按 position_ticket 去重后的交易总数、胜率、盈亏比
- 按平仓标签分组的 PnL 贡献
- 按方向的信号统计（开仓时的 brain_predictions 方向分布）
- Trailing SL 实际距离 vs ATR 统计（从 position_snapshots 计算）
- 各出场原因的 TP/SL 达成率
