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
