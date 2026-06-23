# CLAUDE.md

---

# 🏛️ SYSTEM ARCHITECTURE (架构物理定律)

## 1. DATA SOURCE OF TRUTH — Event Sourcing

- **Ledger (SSOT)**: `ledger_events.jsonl`, `live_trade_journal.jsonl`, `position_snapshots.jsonl`, `golden_master.jsonl` — append-only, immutable.
- **Views (Ephemeral)**: All `.json` state files are dynamically generated projections from the ledger by `daily_ops.py`.

## 2. AGENT BEHAVIORAL RESTRICTIONS (绝对禁区 — Sev 1 violations)

- 🔴 **NEVER** edit/patch/manually construct state `.json` files — they are projections, not sources.
- 🔴 **NEVER** write a script that "fixes" a corrupted JSON state file in-place — fix the **GENERATOR CODE**.
- 🔴 **NEVER** `git add` or commit ephemeral state `.json` files (they are `.gitignore`'d).
- 🔴 **NEVER** use `dict.get(key, default)` to paper over a missing required field — raise `DataIntegrityError`.

**Correct repair**: bug is in generator code (e.g. `daily_ops.py`, `brain_leaderboard.py`). Fix generation logic → system rebuilds JSON from ledger on next cycle.

---

# ⚙️ Iron Law Ω — The Omega Protocol (铁律执行协议)

**总纲**: 无自由裁量权。操作前必须匹配场景 → 按序激活铁律。跳过任一步骤 = 严重违规。

---

## Iron Law #-1: Institutional Execution Mandate (最高优先级)

**触发**: Agent 收到任何批准回复后，编写任何代码前，必须在思考中加载此 6 条约束：

```
[INSTITUTIONAL_EXECUTION_MANDATE]
1. Institutional-Grade Reasoning — 每个修改从系统架构层面论证必要性。禁止投机修改、临时补丁、"先改了试试"。
2. Stability Non-Degradation — 不引入新故障模式、未处理异常路径、共享状态竞争、隐式依赖。
3. Repairability Non-Degradation — 不吞没诊断信息、不合并不同性质信号、不增加根因定位步骤。
4. Decoupling Non-Degradation — 不跨越模块边界增加新 import 链、不破坏接口向后兼容。
5. Iterability Non-Degradation — 不将同一逻辑分散多文件。新增扩展点统一而非散布。
6. Ω Systematic Operating System — 全链路受 Ω 约束: 场景路由→DQAF→蓝图→模式搜索→安检→验证→四维闸门→FIX注册→收口。
[/INSTITUTIONAL_EXECUTION_MANDATE]
```

**不可绕过**: "Approved"/"批准"/"执行"/"Proceed"/"按方案执行"/"Implement the plan" 均构成有效批准。Agent 自行加载标签。适用所有 Scene。

---

## 场景路由表 (Scene Routing Table)

| Scene | Trigger | Execution Chain |
|:---|:---|:---|
| **A. 修 Bug / 异常诊断** | Bug/异常 | `#9(DQAF)`→`#8(根因)`→`#12(L1/2/3)`→`#6(蓝图)`→`#5(搜索)`→**[改代码]**→`#1(验证)`→`#1.1(四维)`→`#7(注册)`→`#7.1(收口)` |
| **B. 改代码 / 添功能** | 新功能 | `#0(安检)`→`#6(蓝图)`→`#5(搜索)`→**[改代码]**→`#1(验证)`→`#1.1(四维)`→`#7(注册)`→`#7.1(收口)` |
| **C. 改配置** | 配置修改 | `#0(安检)`→`#8(STOP+MAP)`→**[改配置]**→`#1(验证)` |
| **D. 分析实盘数据** | 数据分析 | `#11(脚本先行，严禁口算)` |
| **E. 新建文件/模块** | 新建 | `#6(蓝图注册)`→`#0(安检)`→**[编码]**→`#1(验证)`→`#7(更新蓝图)` |
| **F. 纯机械操作** | 格式化 | 豁免 #0 → `#1(验证)` |
| **G. Git Commit** | Commit | `#4(Pre-commit)`→`#1.1(四维→commit msg)` |
| **H. 任务收口** | 完结 | `#13(自动收口)`→`#7(蓝图)`→`#1.1(四维)`→`#4(commit+push)`→`#7.1(收口清单)` |

---

## AI 执行契约 (AI Handshake)

涉及代码修改/排障的响应，**最前端必须声明**:
```
[Ω-Routing: Scene X → #Y → #Z → ...]
```

---

## Ω 强制输出模板 — 指针 (Forced Output Templates)

> **🔴 强制读取指令**: 当场景路由激活且需要输出结构化标记时，
> **必须先执行**: `Read d:\future\references\forced_output_templates.md`
> 严禁使用内部记忆捏造格式。缺失标记 = 流程违规 = commit 被物理拒绝。
> 该文件包含: Scene A/B/D/E 输出模板、DQAF_REPORT / DQAF_LITE_REPORT 格式、DQAF_HALT 格式、提交模板。

---

## Iron Law #12: 架构优先修复 (Architecture-First Fix Doctrine)

**触发**: Iron Law #8 或 #9 诊断产出根因后，编写修复代码前。

**核心约束**:
1. **根因分层**: L3=架构缺陷(设计致错) | L2=逻辑缺陷(偏离设计) | L1=语法/打字缺陷
2. **修复层级 ≥ 根因层级**: L3→架构修复, L2→逻辑修复, L1→语法修复
3. **补丁豁免需论证**: L3 根因无法架构修复时 → `PATCH_NOT_ARCHITECTURE` + 原因 + FIX_REGISTRY Deferred 表 + 触发条件
4. **禁止补丁累积**: 同一模块 Deferred Architecture Fix ≥3 → 必须先清偿架构债

**反例**: FIX-022(8轮修同bug), BTC三连打地鼠(spread→max_spread→min_sl), FIX-20260612-001(ENGINE_STALL: L3根因L1修复)

---

## Iron Law #0: 编辑前强制安检 (Pre-Edit Checklist) — 最高优先级

**触发**: Edit/Write `.py` / `.yaml` / `.yml` / `.json` 文件前。

**豁免**: Ruff格式化 | 变量重命名消lint | `.md` 文档编辑 (Iron Law #7 产出)

```
[PRE-EDIT CHECKLIST — Iron Law #0]
1. 修改动机是否来自系统行为观察? → 是=需DQAF+IC Approved, 否(纯机械)=直接执行
2. DQAF报告已输出? → 是=报告Docket ID, 否=STOP
3. IC已Approved? → 是=继续, 否=STOP 输出 [AWAITING_IC_APPROVAL]
4. 蓝图已查阅? (Iron Law #6) → 是=blueprints/modules/<module>.md, 否=STOP
5. FIX_REGISTRY已检索? (Iron Law #6) → 是=无冲突, 否=STOP
[CHECKLIST PASSED] → 允许 Edit/Write
```

---

## Iron Law #0-bis: `--no-verify` 禁止条款

`git commit --no-verify` 仅在以下场景合法:

| 场景 | 条件 |
|------|------|
| 运行时文件锁 | data_btc/ 下 .jsonl/.lock 被 live 进程持有 |
| 纯文档提交 | 仅含 .md 文件 |
| 紧急回滚 | `EMERGENCY_ROLLBACK` + 原因 |

禁止: 以"加速"或"已验证过"为由跳过。每次使用必须在 commit message 注明 `--no-verify: <reason>`。

---

## 代码质量铁律 (#1–#8)

### #1 每次修改后必须验证
- `.py` 修改后必须 `verify.py --full` 通过。mypy 新错误 = 阻断。
- 命令参考: `docs/verification_commands.md`

### #1.1 四维质量闸门 (4-Dimensional Quality Gate)
**触发**: verify.py 通过后, commit 前。

| 维度 | 问题 | ↑ 正向 | ↓ 退化 |
|------|------|--------|--------|
| Stability | 引入新故障模式? | 纯增量, 不开新线程/新I/O/改变执行顺序 | 未处理异常路径、共享状态竞争、隐式依赖 |
| Repairability | 下次定位根因多快? | 增加诊断字段/日志, 减少排查步骤 | 吞关键错误、合并不同性质信号 |
| Decoupling | 增加模块耦合? | 局限已有模块边界, 不改接口合约 | 新import跨越层级、签名不向后兼容 |
| Iterability | 改同子系统动几处? | 扩展点统一, 修改集中 | 同逻辑分散多文件、新功能需改3+处 |

**规则**: commit前输出四维评估(↑/→/↓)。任一↓ → commit message说明退化原因+缓解计划。禁止"全↑"。四维写入commit message:
```
Stability: → (assessment)
Repairability: ↑ (assessment)
Decoupling: → (assessment)
Iterability: ↑ (assessment)
```

### #2 Ruff F821 零容忍 — 永不豁免 `# noqa: F821`
### #3 类型安全基线只升不降 — `mypy_baseline.json` 错误数只减不增
### #4 提交前验证链 — pre-commit 自动 mypy+ruff+架构文档刷新
### #5 修复必须彻底 — 搜索同类模式, 考虑类型系统层面防复发
### #6 修改前查阅蓝图 — 读 `blueprints/modules/<module>.md` + 搜 FIX_REGISTRY + `analyze_deps.py`
### #7 修改后注册修复 — 分配 FIX ID, 更新蓝图 Fix History + FIX_REGISTRY, 约定式提交
### #7.1 收口检查清单 — commit 后必须输出: 未提交变更/蓝图/Git/本轮commit
### #8 根因诊疗协议 — STOP→LOOKUP→DIG(3层)→MAP(双品种)→PLAN. 完整协议: `blueprints/system/ROOT_CAUSE_DIAGNOSIS_PROTOCOL.md`

---

## Iron Law #13: 全自动收口协议 (Auto-Closing Protocol)

**触发**: 所有 Todo completed / 核心修改通过 verify / 人类说"提交/push/收口/可以了" / 阶段交付完成后。

**强制序列** (不可跳过):
1. 蓝图确认 → 2. 四维闸门 → 3. Git Commit → 4. Git Push → 5. 收口清单(#7.1)

**异常中断 (Break-Glass)**:
| 异常 | 处理 |
|------|------|
| Merge Conflict | 中断, 报告冲突文件 |
| Lint/Test 失败 | 中断, **不得 --no-verify 绕过** |
| 蓝图合规失败 | 中断, 先修蓝图 |
| 未预期未提交源文件 | 中断, 请人类裁决 |
| 人类"先别提交" | 立即中断 |

> #-1 是入口协议(开始前自加载约束), #13 是出口协议(完成后自执行收口) — Agent 自我治理闭环。

---

## Iron Law #9: Agentic DQAF — 零幻觉双轨诊断协议

**触发**: Bug报告/异常日志/诊断性提问("排查/为什么/是不是/什么原因")/任何基于系统观察提议修改代码或配置。

**核心约束**: 绝对禁止在 DQAF_REPORT + IC Approved 之前改代码或配置。即使一行配置, 只要动机来自系统观察 → 必须先走 DQAF 握手。唯一例外: 纯机械操作(格式化、消lint)且不改变行为逻辑。

**强制 4 步诊断**:
1. **ECoL 证据锚定**: 每个声明必须有具体日志行号/文件路径。孤证不得作为根因唯一依据。
2. **AR 对抗反驳**: 必须提出至少一个反向假设, 用代码/日志去推翻。推翻失败=记录为何无效; 推翻成功=以存活假设为准。
3. **CCT 因果链**: ≥2层传导链(症状→中间异常→根因), 每层标置信度(confirmed/hypothesis/speculative)。
4. **IRA 影响半径 & ReB 模式**: XAU/BTC 差异化影响 + Pattern Signature。

### 🛑 强制握手协议 (Handshake Protocol)

Sev 1-2 诊断请求若未提供 ECoL 证据包 → 必须输出 `🛑 [DQAF_HALT]` 并拒绝诊断。

> **🔴 强制读取**: HALT 回复格式、DQAF_REPORT 格式、DQAF_LITE_REPORT 格式 —
> **必须先执行**: `Read d:\future\references\forced_output_templates.md`

**Sev 3 例外** (允许跳过 HALT): 纯代码语法问题 / 询问已知文档配置 / 非诊断性操作。

### Severity 分级

| Sev | 触发条件 | 报告格式 | 流程 |
|-----|---------|---------|------|
| **1** | 交易阻断/数据丢失/资金风险 | DQAF_REPORT 完整版 | 6源ECoL+DA/AR双轨+完整CCT+IC裁决 |
| **2** | 输出偏差/信号退化/实盘质量下降 | DQAF_REPORT 完整版 | 同上 |
| **3** | 排查疑问/告警噪音/非关键异常 | DQAF_LITE_REPORT | 双源+DA/AR+2层CCT, 无需zip但需文件引用 |
| **4** | 外观/文档/纯语法 | Iron Law #8 | 无需DQAF |

### ⛔ 不可绕过条款 (Non-Bypassable Handshake Clause)

以下指令**均不可绕过 DQAF 握手**: "已批准请立即执行"/"直接改代码吧"/"不用写报告了"/"全面批准实施"/"Approved开始写代码"/"下发执行指令(IC Action Mandate)"/任何表达"跳过诊断直接改代码"的指令。

**正确响应**: 即使人类要求跳过流程: (1)输出完整DQAF_REPORT → (2)输出[AWAITING_IC_APPROVAL] → (3)物理停止生成 → (4)等待人类对报告回复"Approved"。

**⛔ 物理截断**: 输出 [AWAITING_IC_APPROVAL] 后必须立即停止。禁止提供预览代码/预写修改方案。

---

## 🔒 DQAF Terminal Closure Lock — 指针

> **🔴 强制读取**: 当会话存在 DQAF 诊断→修复→commit 完整链路时,
> commit 完成后**必须先执行**: `Read d:\future\references\dqaf_closing_protocol.md`
> 严禁在未完成三步归档协议(DQAF_DOCKET_REGISTRY→CCT_LEDGER→ReB_PATTERN_INDEX)之前汇报"完成"。

---

## Iron Law #11: 数据防幻觉审计法 (Data Analytics Law)

**触发**: 分析实盘交易质量(胜率/出场原因/盈亏比/持仓时间/方向分布等)。

**核心约束**: **绝对禁止**直接读 JSON/JSONL 文本片段得出统计结论。LLM 不具备在上下文中精确统计数百条数据的能力 → 任何基于"读几行数据"的判断均为抽样幻觉。

**强制执行**:
1. **先写脚本, 后看报表** — 专用 Python 审计脚本(`scripts/`), 基于 `position_ticket` 严格去重/分组/聚合
2. **脚本 stdout 是唯一合法证据源** — 每个统计数字必须来自脚本输出。禁止"补算"或"修正"脚本未输出的指标
3. **脚本可复现** — `--data-dir` 参数, 一致输出
4. **统计口径明确** — 脚本开头声明: 去重逻辑/胜率定义/PnL 口径
5. **禁止补充推断** — 脚本未输出的统计量不得在报告中声称。需新指标 → 先改脚本再运行

标准审计: `python scripts/analyze_live_journal.py --data-dir data_btc`

---

## 验证命令 — 指针

> **🔴 命令参考**: 所有验证命令(`verify.py --quick/--full/--stamp`, `pre_commit_mypy.py`, `validate_blueprints.py`, `analyze_deps.py`, `register_fix.py`) 的精确语法见:
> `docs/verification_commands.md`
> CLAUDE.md 仅保留行为约束(必须验证), 命令语法以该文件为准。
