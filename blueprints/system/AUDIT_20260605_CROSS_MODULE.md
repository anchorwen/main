# 跨模块合约审计报告 (2026-06-05)

## 结论：健康。全链路防御模式成熟。

## 详细发现

### 1. Optional 返回值调用安全

| 边界 | 被调方 | 返回类型 | 调用方保护 | 风险 |
|------|--------|---------|-----------|------|
| get_position() | position_manager.py:293 | ActivePosition \| None | 所有 10+ 调用点均有 `if pos is not None` 守护 | 低 |
| compute() | contract_groups.py:297 | ConsensusResult \| None | strategy_line.py:1866 `if signal is not None:` + fallback | 低 |
| load_state() | position_manager.py:1591 | ActivePosition \| None | 所有调用点检查 None | 低 |
| build_modify/close_payload() | position_manager.py:1488 | dict (含 None 回退) | 回退 ticket=0 会触发下游 rejection（非崩溃） | 低 |

### 2. 初始化顺序依赖

- `LiveCycleState.position_manager` 默认 `None`，所有访问均守护
- `_reentry_states` 通过 `ensure_reentry_state()` 懒初始化
- `bootstrap_restart_state()` 在第一周期调用一次
- governance 加载失败 → 静默放行所有大脑（fail-open）

### 3. 隐式接口契约

- `getattr(obj, "field", default)` 模式：50+ 处，用于 BrainSignal/BrainDecisionProposal 双协议兼容
- `hasattr()` 检查：全部为懒初始化或协议检测，无遗漏实现
- 无 `except AttributeError` 跨模块调用

### 4. 唯一中等风险

- `strategy_builder.py:121-135`: brain config 缺少 `status` 字段 → 不匹配 frozen/retired 检查 → 大脑静默放行
- 影响：配置不完整的 brain 可能意外参与投票
- 建议：空 status 应默认为 "candidate" 并记录警告

## 关键边界验证

| 边界 | 调用方 | 被调方 | 风险 |
|------|--------|--------|------|
| live_cycle → position_manager | `manage_position()` L490 | PM 全部方法 | 低 |
| strategy_line → contract_groups | `_compute_consensus()` L755 | `compute()` L297 | 低 |
| restart_state → reentry_guard | `bootstrap` L244 | `ExitRecord` L341 | 低 |
| live_startup → governance | `apply_governance_filter` L145 | `GovernanceService.load()` | 低 |
| strategy_builder → brain configs | `build_strategy_lines` L88 | brain JSON files | 中 |
| mt5_bridge → journal | `process_one` L519 | journal 消费者 | 低 |
