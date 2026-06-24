# UGR v3.1 — 生产就绪检查清单 (Production Readiness Checklist)

> **目标**: UGR-A10 → B06 韧性架构全线 go-live
> **审核日期**: 2026-06-28 (W4 末)
> **审核人**: Ω Agent + IC 批准

---

## 1. WAL 完整性 (Write-Ahead Log Integrity)

| # | 检查项 | 当前状态 | 目标 | 验证方法 |
|---|--------|---------|------|---------|
| 1.1 | WAL hash chain 全量验证 | ✅ 已实现 | ✅ | `wal.verify_integrity()` + `tests/data/test_write_ahead_log.py` |
| 1.2 | WAL rotation + checkpoint HMAC | ✅ 已实现 | ✅ | `wal.verify_integrity_from_checkpoint()` |
| 1.3 | WAL 磁盘配额保护 | ✅ UGR-A10 | ✅ | `wal.check_quota()` + `tests/data/test_wal_multi_instance.py` |
| 1.4 | WAL 多实例并行 | ✅ UGR-A10 | ✅ | `tests/data/test_wal_multi_instance.py::TestTwoIndependentWALs` |
| 1.5 | WAL 在线抽查 (SupervisedScheduler) | ✅ UGR-A10 | ✅ | `scheduler.register_wal_integrity_check()` |
| 1.6 | WAL bit-rot 混沌检测 | ✅ UGR-B05 | ✅ | `tests/data/test_wal_bit_rot.py` (8 chaos modes) |
| 1.7 | WAL crash recovery | ✅ UGR-B06 | ✅ | `tests/resilience/test_e2e_resilience.py::TestWALCrashRecovery` |

## 2. Phantom 合约 (Phantom Contracts)

| # | 检查项 | 当前状态 | 目标 | 验证方法 |
|---|--------|---------|------|---------|
| 2.1 | Phantom stub 独立 WAL | ✅ UGR-A10 | ✅ | `init_phantom_wal()` + `tests/data/test_wal_multi_instance.py` |
| 2.2 | Phantom WAL 磁盘配额 | ✅ UGR-A10 | ✅ | `_write_phantom_stub()` 配额守卫 |
| 2.3 | Phantom stub 不一致检测 | ✅ UGR-B05 | ✅ | `tests/contracts/test_phantom_inconsistency.py` |
| 2.4 | 9 个谓词注册完整 | ✅ 现有 | ✅ | `PredicateRegistry.list_contracts()` |
| 2.5 | StateProjector 投影正确 | ✅ 现有 | ✅ | `tests/contracts/test_phantom_contract.py` |

## 3. AST 扫描器 (AST Structural Scanner)

| # | 检查项 | 当前状态 | 目标 | 验证方法 |
|---|--------|---------|------|---------|
| 3.1 | 5 检测器全覆盖 | ✅ UGR-A10 | ✅ | `verify_capresult_ast.py --enforce` |
| 3.2 | Proof leak 检测 | ✅ UGR-B05 | ✅ | `tests/scripts/test_proof_leak_detection.py` |
| 3.3 | FOG/LC 废弃检测 | ✅ UGR-A07 | ✅ | `FailOpenGuardDetector` + `scripts/` 0 残留 |
| 3.4 | CI pre-commit 集成 | ✅ UGR-A10 | ✅ | `.pre-commit-config.yaml: verify-capresult-ast` |
| 3.5 | CI pre-push 全量测试 | ✅ UGR-A10 | ✅ | `.pre-commit-config.yaml: ci-mirror-pytest` |

## 4. 韧性混沌注入 (Resilience Chaos Injection)

| # | 检查项 | 当前状态 | 目标 | 验证方法 |
|---|--------|---------|------|---------|
| 4.1 | Bit-rot 注入 → hash chain 检测 | ✅ UGR-B05 | ✅ | `tests/data/test_wal_bit_rot.py` |
| 4.2 | Phantom stub 不一致 → CI 捕获 | ✅ UGR-B05 | ✅ | `tests/contracts/test_phantom_inconsistency.py` |
| 4.3 | Proof leak → runtime + CI 双检测 | ✅ UGR-B05 | ✅ | `tests/scripts/test_proof_leak_detection.py` |
| 4.4 | 全量集成测试 (NTP/磁盘/进程) | ✅ UGR-B06 | ✅ | `tests/resilience/test_e2e_resilience.py` |

## 5. Feature Flags — Shadow → Live

| # | Flag | 当前模式 | 目标模式 | 切换条件 |
|---|------|---------|---------|---------|
| 5.1 | `InvariantEngine` 告警 | Shadow (log-only) | Live (halt-on-critical) | CI 全绿 + 7 天无假阳性 |
| 5.2 | `PhantomStub` 记录 | Shadow (stderr) | Live (WAL persist) | `verify_phantom_contracts.py` 通过 |
| 5.3 | `verify_capresult_ast` | Baseline (1 detector) | Enforce (5 detectors) | ✅ UGR-A10 已切换 |

## 6. 代码质量门禁 (Code Quality Gates)

| # | 检查项 | 状态 |
|---|--------|------|
| 6.1 | ruff F/E 零错误 | ✅ `ruff check core/ apps/ scripts/ --select=F,E` |
| 6.2 | mypy baseline 零新错误 | ✅ `scripts/pre_commit_mypy.py` |
| 6.3 | verify.py --full 通过 | 待验证 |
| 6.4 | pytest 全量通过 | 待验证 `pytest tests/ -q --tb=short` |

## 7. 文档 (Documentation)

| # | 检查项 | 状态 |
|---|--------|------|
| 7.1 | 开发者韧性编码手册 | 📝 UGR-B06 创建中 |
| 7.2 | WAL API 文档 | ✅ `write_ahead_log.py` docstrings |
| 7.3 | Phantom 合约文档 | ✅ `phantom_contract.py` docstrings |
| 7.4 | FIX_REGISTRY 更新 | 📝 待更新 |

## 8. 签名 (Sign-off)

- [ ] **Ω Agent**: 代码审查通过
- [ ] **IC (Institutional Controller)**: 批准 go-live
- [ ] **CI 门禁**: 全绿通过
- [ ] **部署**: `main` 分支 → 生产环境
