# 开发者韧性编码手册 v1.0 (Resilience Coding Guide)

> **版本**: v1.0 — UGR v3.1  
> **生效日期**: 2026-06-28  
> **适用范围**: `core/`, `scripts/`, `apps/` — 所有 Python 模块  
> **合规要求**: 违反本手册的模式将被 CI 门禁拦截 (AST Scanner + pre-push pytest)

---

## 目录

1. [CapResult 模式](#1-capresult-模式)
2. [Phantom 编写指南](#2-phantom-编写指南)
3. [时间类型纪律](#3-时间类型纪律)
4. [新增合约/不变式流程](#4-新增合约不变式流程)

---

## 1. CapResult 模式

### 1.1 核心原则

`CapResult[T]` 是 UGR v3.1 的类型安全结果类型。**禁止**使用裸 `Optional[T]` 或 `try/except` 吞没错误来表示可失败操作。

```
正确: def compute() -> CapResult[float]: ...
错误: def compute() -> Optional[float]: ...
错误: def compute() -> float:  # raises ValueError silently
```

### 1.2 ok/err 使用规范

```python
from core.runtime.cap_result import CapResult, Kernel

# 创建成功结果
def safe_divide(a: float, b: float) -> CapResult[float]:
    if b == 0:
        return CapResult.err("Division by zero")
    return CapResult.ok(a / b)

# 消费结果 — 模式 A: success_scope (推荐)
with Kernel.success_scope() as scope:
    result = safe_divide(10.0, 2.0)
    value = scope.unwrap(result)  # 类型安全: 在此作用域内 result 已知为 ok
    print(f"Result: {value}")

# 消费结果 — 模式 B: 手动检查 (不推荐，仅用于兼容旧代码)
result = safe_divide(10.0, 2.0)
if result.ok():
    value = result.value()  # ok() 守卫后才能调用 value()
else:
    print(f"Error: {result.error()}")
```

### 1.3 `_SuccessProof` 生命周期

`_SuccessProof` 是 `success_scope` 内部使用的令牌类型，证明 `CapResult.ok()` 已通过验证：

```python
# _SuccessProof 的生命周期:
# 1. 创建: CapResult.ok() 在 success_scope 内返回
# 2. 传递: 可在 scope 内传递给其他函数
# 3. 销毁: scope 退出时自动失效

# 严禁:
# - 将 _SuccessProof 存储在 scope 之外的实例变量中
# - 将 _SuccessProof 序列化到磁盘/网络
# - 在另一个 scope 中复用 _SuccessProof
# → 以上行为被 AST Scanner (ProofLeakDetector) 拦截
```

### 1.4 AST Scanner 强制约束

| 违规模式 | 检测器 | 示例 |
|---------|--------|------|
| `CapResult.ok()` 在 `success_scope` 外调用 | `CapResultOkPlacementDetector` | `if result.ok(): return result.value()` (除非在 scope 内) |
| `_SuccessProof` 存储在持久位置 | `ProofLeakDetector` | `self._proof = proof` |
| `getattr/setattr/hasattr` 作用于 CapResult | `DynamicCallDetector` | `getattr(result, "_raw")` |
| `fail_open_guard` / `log_and_continue` | `FailOpenGuardDetector` | 任何 `with fail_open_guard(...)` |

---

## 2. Phantom 编写指南

### 2.1 什么是 Phantom 合约

Phantom 合约是在生产环境中以「影子模式」运行的不变式检查。它记录完整的输入快照到 WAL，允许离线重放验证，但**绝不**在生产路径上阻断执行。

```
热路径 (Hot Path):  零开销 — 装饰器直接跳过
非热路径 (Non-hot): 记录 PhantomStub → WAL → 离线审计
```

### 2.2 谓词注册

```python
from core.contracts.phantom_contract import PredicateRegistry, phantom

# 步骤 1: 注册谓词
@PredicateRegistry.register(
    "my_invariant_name",           # contract_id — 全局唯一
    version=1,                     # 合约版本
    required_state_keys={"positions", "brain_states"},  # 依赖的状态键
)
def my_invariant(state: dict) -> bool:
    """检查: 所有仓位必须有对应的 Brain 分配"""
    positions = state.get("positions", {})
    brains = state.get("brain_states", {})
    for pos_id, pos in positions.items():
        if pos.get("brain_id") not in brains:
            return False
    return True

# 步骤 2: 使用 @phantom 装饰器
@phantom("my_invariant_name", hot_path=False)
def check_positions():
    # 在非热路径上: 此调用将记录 PhantomStub
    state = load_current_state()
    return my_invariant(state)
```

### 2.3 Hot Path vs Non-Hot Path 分类

| 分类 | 特征 | 性能预算 | 示例 |
|------|------|---------|------|
| **Hot Path** | 每 tick/每条 Bar 执行 | <1μs 开销 | `risk_budget_non_negative` |
| **Non-Hot Path** | 每日/每小时执行 | <10ms 开销 | `training_readiness`, `model_card_completeness` |

```python
# Hot Path: 生产模式 (-O) 下零开销
@phantom("risk_budget_non_negative", hot_path=True)
def check_budget(budget: float) -> bool:
    return budget >= 0

# Non-Hot Path: 生产模式下记录 stub
@phantom("training_readiness", hot_path=False)
def check_training_readiness() -> bool:
    # 每日运行; stub 记录到 WAL
    ...
```

### 2.4 Phantom WAL 独立配置

```python
from core.data.write_ahead_log import WALConfig
from core.contracts.phantom_contract import init_phantom_wal

# Phantom 使用独立 WAL 实例
phantom_config = WALConfig(
    path=Path("data/phantom_audit.jsonl"),
    fsync_on_write=False,     # 审计数据可容忍少量丢失
    disk_quota_mb=50,         # 硬配额: 防止审计 WAL 无限增长
    rotate_on_size_mb=10,
)
init_phantom_wal(phantom_config)
```

### 2.5 StateProjector 集成

```python
from core.contracts.phantom_contract import StateProjector

projector = StateProjector()

# 注册状态变更处理器
projector.register_handler("position_open", handle_position_open)
projector.register_handler("position_close", handle_position_close)
projector.register_handler("budget_update", handle_budget_update)
projector.register_handler("brain_state", handle_brain_state)

# 声明依赖键 (与 PredicateRegistry.required_state_keys 一致)
projector.declare_required_keys({"positions", "brain_states"})

# 从 WAL 投影到指定序列号
state, reached_seq = projector.project_to(wal, target_seq=1000)
```

---

## 3. 时间类型纪律

### 3.1 三种时间类型

UGR v3.1 定义了三种不可互操作的时间类型：

| 类型 | 用途 | 获取方式 | 严禁操作 |
|------|------|---------|---------|
| `MonotonicInstant` | 测量时间间隔 | `TypedClock.monotonic()` | 与 WallInstant 比较/加减 |
| `WallInstant` | 墙上时钟时间戳 | `TypedClock.wall()` | 用于超时计算 |
| `Duration` | 时间间隔 | `MonotonicInstant` 之差 | 直接与 float 运算 |

### 3.2 使用规则

```python
from core.runtime.typed_clock import TypedClock, MonotonicInstant, WallInstant, Duration

clock = TypedClock()

# ✅ 正确: MonotonicInstant 用于超时
start: MonotonicInstant = clock.monotonic()
result = do_work()
elapsed: Duration = clock.monotonic() - start
if elapsed > Duration.seconds(5.0):
    alert("Timeout!")

# ✅ 正确: WallInstant 用于日志/时间戳
timestamp: WallInstant = clock.wall()
log.info(f"Event at {timestamp.isoformat()}")

# ❌ 错误: MonotonicInstant 不能用于墙上时钟
timestamp = clock.monotonic()  # 类型错误! monotonic() 返回的是任意起点

# ❌ 错误: Duration 不能与 float 直接比较
if elapsed > 5.0:  # 类型错误! 应该用 Duration.seconds(5.0)
    ...
```

### 3.3 AST Scanner 保护

| 违规 | 检测 |
|------|------|
| `._raw` 访问时间类型的内部字段 | `RawAccessDetector` |
| `getattr/setattr` 作用于时间类型 | `DynamicCallDetector` |
| 时间类型序列化到 JSON/pickle | 运行时 `TypeError` |

### 3.4 与旧代码兼容

```python
# 旧代码: import time; time.time() / time.monotonic()
# 新代码: 使用 TypedClock
# 迁移路径:
#   time.monotonic() → clock.monotonic() → MonotonicInstant
#   time.time()      → clock.wall()      → WallInstant
```

---

## 4. 新增合约/不变式流程

### 4.1 流程图

```
需求分析 → 谓词注册 → Phantom 装饰 → StateProjector → InvariantEngine → CI 集成
```

### 4.2 步骤详解

#### 步骤 1: 谓词注册 (PredicateRegistry)

```python
# 在 core/contracts/phantom_contract.py 或你的模块中:

from core.contracts.phantom_contract import PredicateRegistry

@PredicateRegistry.register(
    "data_freshness_check",     # 全局唯一的 contract_id
    version=1,
    required_state_keys={"feature_store"},  # 依赖的状态键
)
def check_data_freshness(state: dict) -> bool:
    """数据新鲜度检查: 最新特征时间戳不超过 5 分钟"""
    fs = state.get("feature_store", {})
    latest = fs.get("latest_timestamp", 0)
    now = time.time()
    return (now - latest) < 300
```

#### 步骤 2: Phantom 装饰

```python
from core.contracts.phantom_contract import phantom

@phantom("data_freshness_check", hot_path=False)
def daily_data_quality_check():
    """每日数据质量审计 — 非热路径"""
    state = load_feature_store_state()
    return check_data_freshness(state)
```

#### 步骤 3: StateProjector 集成

如果新合约依赖新的状态键，必须注册处理器：

```python
from core.contracts.phantom_contract import StateProjector

def handle_feature_store_update(entry: dict) -> None:
    """处理 feature_store 更新事件"""
    # entry 是从 WAL 读取的原始字典
    ...

projector = StateProjector()
projector.register_handler(
    "feature_store_update",
    handle_feature_store_update,
    writes_keys={"feature_store"},  # 声明写入的键
)
```

#### 步骤 4: InvariantEngine 注册

```python
from core.observability.invariant_engine import (
    InvariantDef,
    InvariantEngine,
)

# 定义不变式
freshness_invariant = InvariantDef(
    name="data_freshness",
    description="Feature store timestamp within 5 minutes",
    check=lambda ctx: check_data_freshness(ctx),
    severity="warning",  # "info" | "warning" | "critical"
)

# 注册到引擎
engine = InvariantEngine(wal=phantom_wal)
engine.register(freshness_invariant)
```

#### 步骤 5: CI 集成

1. **测试文件**: `tests/contracts/test_<contract_id>.py`
2. **混沌注入**: `tests/contracts/test_phantom_inconsistency.py` — 添加新的不一致场景
3. **CI 门禁**: `pytest tests/ -q --tb=short` (pre-push 自动运行)

### 4.3 检查清单

新增合约/不变式前，确认以下项目：

- [ ] `PredicateRegistry.register()` 已调用，contract_id 全局唯一
- [ ] `required_state_keys` 已声明，与 StateProjector 处理器一致
- [ ] `@phantom()` 装饰器已添加，hot_path 分类正确
- [ ] 独立测试文件已创建 (`tests/contracts/test_<id>.py`)
- [ ] 混沌注入场景已覆盖 (bit-rot, hash mismatch, assumed_ok=False)
- [ ] InvariantEngine 已注册 (如需实时告警)
- [ ] `_FORCE_PRODUCTION_MODE` 兼容性已验证 (`__debug__` 模式下)
- [ ] FIX_REGISTRY 已更新

### 4.4 反模式 (严禁)

| 反模式 | 后果 | 检测方式 |
|--------|------|---------|
| `except Exception: pass` 替代 CapResult | 数据丢失、静默失败 | ruff BLE001 |
| `_SuccessProof` 跨 scope 复用 | 假阳性验证 | AST ProofLeakDetector |
| `MonotonicInstant` 用于日志时间戳 | 不可读的时间戳 | Code review |
| 未注册 `required_state_keys` | StateProjector 状态不完整 | `project_to()` 报错 |
| Hot path 上执行 I/O | 性能退化 | 性能测试 profile |

---

## 附录 A: CI 门禁参考

| 门禁 | 阶段 | 命令 |
|------|------|------|
| ruff | pre-commit | `ruff check --fix --extend-select=BLE` |
| mypy | pre-commit | `scripts/pre_commit_mypy.py` |
| AST Scanner | pre-commit | `scripts/verify_capresult_ast.py --enforce` |
| verify.py | pre-commit | `scripts/verify.py --quick` |
| pytest | pre-push | `pytest tests/ -q --tb=short` |

## 附录 B: 相关文件索引

| 文件 | 用途 |
|------|------|
| `core/runtime/cap_result.py` | CapResult[T], Kernel, _SuccessProof |
| `core/runtime/typed_clock.py` | MonotonicInstant, WallInstant, Duration |
| `core/contracts/phantom_contract.py` | PredicateRegistry, StateProjector, @phantom |
| `core/data/write_ahead_log.py` | WriteAheadLog, WALConfig, hash chain |
| `core/observability/invariant_engine.py` | InvariantEngine, InvariantViolation |
| `core/runtime/fault_handler.py` | (DEPRECATED: fail_open_guard, log_and_continue) |
| `scripts/verify_capresult_ast.py` | AST 结构约束扫描器 |
| `tests/data/test_wal_bit_rot.py` | Bit-rot 混沌注入测试 |
| `tests/contracts/test_phantom_inconsistency.py` | Phantom 不一致注入测试 |
| `tests/resilience/test_e2e_resilience.py` | 端到端韧性集成测试 |

## 附录 C: 变更日志

| 版本 | 日期 | 变更 |
|------|------|------|
| v1.0 | 2026-06-28 | 初始版本 — UGR-A10/B05/B06 全线 go-live |
