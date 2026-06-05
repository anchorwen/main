# 配置校验缺口审计报告 (2026-06-05)

## 结论：2 个高风险——LiveCycleConfig 零验证 + YAML 损坏静默回退

## 高风险发现 🔴

### 1. LiveCycleConfig 无字段验证

**位置**: `live_cycle.py:78-160`

`__post_init__` 仅检查 `contract_size` 与 ASSET_REGISTRY 的一致性。**所有其他字段均无合理性验证**：

| 字段 | 可接受的非法值 | 后果 |
|------|-------------|------|
| `interval_seconds` | 0, -1 | 无限循环或崩溃 |
| `cooldown_seconds` | 0, -1 | 冷却禁用 |
| `max_positions` | 0 | 所有入场被阻止 |
| `sl_atr_mult` | 0, -2.0 | 零距离止损或无止损 |
| `tp_atr_mult` | 0 | 零距离止盈 |
| `risk_budget_usd` | -100 | 负风险预算 |
| `lot_step` | 0 | 除零错误 |
| `confidence_threshold` | 1.5 | 永不开单 |
| `market_type` | "forex_24_6" | 静默回退默认行为 |

### 2. 损坏/空 YAML → 静默默认值回退

**位置**: `live_intent_loop.py:306-389`, `brain_lifecycle_manager.py:185`

- `yaml.safe_load()` 失败 → 捕获 → `strategy_configs = {}`
- `_load_live_yaml()` 文件缺失/为空 → 返回 `{}`
- **系统不崩溃**，使用所有硬编码默认值静默运行
- 配置损坏没有告警渠道（仅日志记录）

## 中等风险发现 🟡

### 3. 缺失 YAML 键静默回退默认值

**位置**: `strategy_builder.py:172-182`

```python
def _cfg(name, key, default):
    return config.strategy_configs.get(name, {}).get(key, default)
```

缺失键 → 静默返回默认值，**无日志警告**。

特例：`btc_swing` 需要 `min_sl_distance: 80.0`，但默认值为 `0.0`。如果 BTC 配置中漏掉此键，XAU 默认值会静默应用。

### 4. YAML 策略名与合约组不匹配 → 静默忽略

**位置**: `strategy_builder.py:88-155`

策略在 YAML 中定义但合约组不存在 → 策略行永不构建，无错误。

### 5. 退出配置验证不完整

**位置**: `live_cycle.py:1844-1857`

仅检查未知键。不检查：负值、缺失必需键、类型错误、`hesitation_cycles: 0`。

### 6. 未知时间框架无警告回退

**位置**: `live_cycle.py:1806-1841`

`"H2"` 等未知时间框架 → `mult = 1`（视为 M5），无警告。

### 7. 反向 market_type 配置

- XAU 配置 `market_type: crypto_24_7` → 周末交易，流动性差
- BTC 配置 `market_type: forex_24_5` → 周末完全停止
- 无校验阻止错误配置

## 建议修复优先级

1. **P0**: `LiveCycleConfig.__post_init__` 增加字段合理性验证（至少检查正值/非零）
2. **P0**: YAML 加载失败 → 硬崩溃（或至少 CRITICAL 告警），不接受默认值
3. **P1**: `_cfg` 闭包在键缺失时记录 WARNING
4. **P2**: 未知时间框架记录 ERROR
5. **P2**: `market_type` 值校验（仅允许已知值）
