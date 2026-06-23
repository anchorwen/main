# 验证命令参考 (Verification Commands Reference)

> 此文件是 CLAUDE.md Iron Law #1（每次代码修改后必须验证）的命令参考。
> 行为约束在 CLAUDE.md 中，此文件仅提供精确的命令语法。

---

## 快速验证 (每次 .py 修改后必须运行)

```bash
# mypy + ruff + 蓝图合规 (~10s)
python scripts/verify.py --quick
```

## 完整验证 (commit 前推荐)

```bash
# 全量 mypy + ruff + 蓝图合规 + pytest (~2min)
python scripts/verify.py --full
```

## Pre-push CI-Mirror Gate

每次 `git push` 自动触发，在代码到达 GitHub 之前运行 CI 等价的检查：
- `ruff check core/ apps/ scripts/` (全量，非仅变更文件)
- `mypy baseline check` (新类型错误阻断)

```bash
# 一次性安装 (新机器):
pre-commit install --hook-type pre-push

# 手动运行:
python scripts/hook_pre_push.py

# 紧急绕过 (需在 commit message 中说明原因):
git push --no-verify
```

设计原理: 本地 pre-commit 只检查变更文件，CI 检查全量代码。如果长期 `--no-verify` 绕过本地 hook，lint 债务只在 CI 层面可见 → 每次 push 都是 CI 红叉赌博。Pre-push hook 关闭了这个缺口。

## 其他命令

```bash
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
