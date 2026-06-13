# GitHub Branch Protection — 结构性强直

## 目标

物理阻断 `git push --force origin main` 和绕过 CI 的直接提交。
任何人（包括创始人）都不能直接推送到 main 分支。

## Step 1: GitHub 仓库设置 (5 分钟)

1. 打开 https://github.com/<your-username>/<your-repo>/settings/branches
2. 点击 "Add branch protection rule"
3. 配置:

```
Branch name pattern: main

☑ Require a pull request before merging
   ☑ Require approvals (1)
   ☐ Dismiss stale pull request approvals when new commits are pushed

☑ Require status checks to pass before merging
   ☑ Require branches to be up to date before merging
   Search for: verify (if you add a GitHub Actions CI job)

☑ Require conversation resolution before merging

☐ Do not allow bypassing the above settings
   (Keep this OFF for emergency admin access)

☑ Restrict who can push to matching branches
   (Only add yourself)

☐ Allow force pushes  ← CRITICAL: leave OFF
☐ Allow deletions      ← CRITICAL: leave OFF
```

4. 点击 "Create" 保存

## Step 2: 验证

```bash
# 应该被阻止
git push origin main
# → error: GH006: Protected branch update failed

# 正常流程
git checkout -b feature/xxx
git push origin feature/xxx
# → 创建 PR → CI 通过 → 合并
```

## Step 3 (可选): GitHub Actions CI

创建 `.github/workflows/verify.yml`:

```yaml
name: Verify
on: [pull_request]
jobs:
  verify:
    runs-on: windows-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - run: pip install -r requirements.txt
      - run: python scripts/verify.py --quick
```

## 当前限制

- 单人团队 PR 流程有一定摩擦成本
- Windows CI runner 费用较高 (GitHub Actions Windows minutes)
- MT5 相关测试需要 MT5 终端环境（CI 无法运行）

**建议**: 先配置 Branch Protection（零成本），CI 暂缓。
本地防线：pre-commit hook + hash-lock + verify.py --quick。
