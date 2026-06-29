# Iron Law #8 — Root Cause Diagnosis Protocol

> **纪律**: 发现问题后，不挖到根因不出方案，不画影响链路不写代码。

---

## 五步协议: STOP → LOOKUP → DIG → MAP → PLAN

### Step 1: STOP — 停

不急于出修改方案。先回答三个问题：

```
1. 现象是什么？（精确描述，不是猜测）
2. 影响面多大？（单品种/双品种/全系统）
3. 紧急程度？（阻断交易/数据损坏/告警噪音/技术债）
```

**输出**: 一句话问题陈述，写入计划文件 Context 节。

---

### Step 2: LOOKUP — 查

按顺序执行以下查询，不可跳过：

```bash
# 2a. 依赖分析
python scripts/analyze_deps.py <module-name>

# 2b. 蓝图已知问题
读 blueprints/modules/<module>.md   → Fix History + Known Issues
读 blueprints/modules/<module>.md   → Cross-Module Contracts（谁依赖这个模块）

# 2c. 历史修复
搜 blueprints/system/FIX_REGISTRY.md      → 同文件/同函数的历史 FIX
搜 blueprints/system/FIX_REGISTRY_2026.md → 详细修复记录

# 2d. 项目记忆
读 CLAUDE.md                              → 铁律约束
搜 memory/                                → 搁置项/提醒/已知限制
```

**输出**: 汇总已有信息——这个问题是已知的还是新的？历史上有过类似的吗？

---

### Step 3: DIG — 挖

从根因追问至少 **3 层 "为什么"**：

```
现象: XAU 启动即退出
  → 为什么？因为锁竞争
    → 为什么锁竞争？因为 XAU 和 BTC 用同一把锁
      → 为什么用同一把锁？因为 lock_dir 硬编码了 "data/locks"
        → 为什么没被发现？因为没有自动化检测多进程锁冲突
          （4 层 → 根因不是"改了 lock_dir"，而是"缺少多品种锁隔离的架构约束"）
```

**分类根因**:

| 类别 | 定义 | 示例 |
|------|------|------|
| 代码 bug | 逻辑错误、边界条件 | FIX-022: 硬编码 schema |
| 配置错误 | 参数值不正确 | BTC spread_points=1400 |
| 设计缺陷 | 架构层面的脆弱性 | 状态格式迁移不完整 |
| 流程漏洞 | 缺乏检查机制 | 孤儿检测崩溃循环 |

**横向搜索**: 同一 bug 类型是否在其他模块也存在？

```bash
grep -rn "相同模式" core/ scripts/ --include="*.py"
```

**输出**: 根因链 + 根因类别 + 横向影响范围。

---

### Step 4: MAP — 画

画影响链路，确认修复不会引发连锁反应：

```
修复 A → 检查点:
  ├─ 是否涉及共享基础设施？（live_cycle.py, strategy_line.py, ...）
  │   └─ 是 → BTC 和 XAU 都要验证
  ├─ 是否改了配置格式？（live.yaml, state JSON, ...）
  │   └─ 是 → 需要迁移方案 + 向后兼容
  ├─ 是否改了函数签名？
  │   └─ 是 → 搜索所有调用点: grep -rn "func_name" core/ scripts/
  └─ 是否新增了字段/数据？
      └─ 是 → 内存文件（memory/）是否要更新？
```

**输出**: 影响链路图 + 验证范围清单。

---

### Step 5: PLAN — 案

出一篮子方案而非单点补丁。方案必须回答：

```
✓ 根因是什么（Step 3 的结论）
✓ 为什么这个方案能防止复发（而不只是缓解症状）
✓ 回归验证计划（具体命令 + 预期结果）
✓ 如果 mypy 能捕获此类 bug，是否需要加类型约束
✓ 是否需要更新蓝图的 Known Issues
```

写入计划文件 → 提交 `ExitPlanMode` 审批 → **批准后才开始编码**。

---

## 反例：违反协议的真实案例

| 案例 | 违反步骤 | 后果 |
|------|---------|------|
| FIX-022 维度不匹配 | 跳过 Step 3 横向搜索 | swing_strategy.py 修了 8 轮才发现根因是上游装配点硬编码 |
| BTC spread_points=1400 | 跳过 Step 4 MAP | 改了 spread 才发现 max_spread 也挡着，再改发现 min_sl 也挡着——打了 3 次地鼠 |
| FIX-040 孤儿检测 | 跳过 Step 2 LOOKUP | 没查 FIX-036 的关联性，结果 v2→v3 格式迁移遗留问题又爆发 |
| XAU shadow 误判 | 跳过了整个协议 | 第一次诊断"governance 死锁"被实盘打脸，第二次诊断"bootstrap 失败"又错了 |
| FIX-056 测试修复 | 跳过全部步骤 | 改了断言值但未注册 FIX、未更新蓝图——"看起来简单"不是跳过协议的理由 |
| verify stamp 过期 | 跳过全部步骤 | 直接跑命令但未先检查蓝图——"只是跑个命令"不是跳过协议的理由 |

---

## 红线：以下情况绝对不可跳过协议

| 情况 | 最低要求 |
|------|---------|
| 修改任何 `.py` 文件 | 至少: 读蓝图 Fix History + 注册 FIX |
| 修改测试期望值 | 同上——测试即规格 |
| 修改配置文件 | 至少: STOP + MAP |
| "看起来简单"的改动 | **完整协议——越是"简单"越容易漏** |
| 跑 verify.py --stamp | 必须先通过全部检查 |

### 自检清单（每次编辑前必问）

```
□ 我读了目标模块的蓝图 Fix History 吗？
□ 我搜了 FIX_REGISTRY 确认没有历史修复冲突吗？
□ 如果这是修改逻辑，我执行了 STOP→LOOKUP→DIG→MAP→PLAN 吗？
□ 改完后我注册了 FIX ID 并更新了蓝图吗？
□ 我跑 verify.py --quick 确认通过了吗？
```

**如果任一项答案是"否"——停下来，先完成再继续。**

---

## 成功案例：FIX-039 Feature Assembly Factory

```
Step 1 STOP:  Barrier_V9_12B_V2 每 cycle 报 feature_dimension_mismatch
Step 2 LOOKUP: 读 FIX_REGISTRY → FIX-022 同类 bug 在 swing_strategy.py
              读 blueprints → 架构师早有计划用工厂模式
              搜横向 → barrier_strategy.py 也是硬编码
Step 3 DIG:   根因 = 装配逻辑散落在各策略类中（设计缺陷），不是 barrier 这一个文件的问题
Step 4 MAP:   改动涉及 swing/barrier/live_cycle/registry → 4 文件都要改
              两个品种都跑 swing 相关逻辑 → 都要回归
Step 5 PLAN:  升维方案 = 中央工厂 + 基类共享 + 向后兼容
              一次修复覆盖所有现有和未来的策略类
              2738 测试通过
```

---

## 协议适用门槛

| 情况 | 是否执行协议 |
|------|------------|
| 单行 typo 修复 | 跳过，但必须说明 |
| 配置值调整（改数字） | 简化版：STOP + MAP |
| 新增/修改逻辑 | **完整五步** |
| 涉及共享基础设施 | **完整五步 + 双品种验证** |
| 涉及状态/配置格式变更 | **完整五步 + 迁移方案** |
