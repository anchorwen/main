# XAU/BTC 代码路径分叉审计报告 (2026-06-05)

## 结论：大部分健康。2 个中等风险——BTC 参数泄漏到 XAU。

## 无分歧路径 ✅

| 模块 | 行数 | 说明 |
|------|------|------|
| `strategy_evaluator.py` | 全文件 | 符号无关，遍历所有策略线 |
| `position_manager.py` | ~1,760 行 | 零 `symbol` 引用，完全符号无关 |
| `_execute_management_phase` | L467+ | 符号无关，退出逻辑委托给 PM |
| `contract_groups.py` | 共识逻辑 | BTC_SWING_GROUP 为正确的架构抽象 |

## 有意的结构性分叉 ✅

| 位置 | 分叉方式 | 风险 |
|------|---------|------|
| `pre_trade_guards.py:28-55` | `crypto_24_7` vs `forex_24_5` 会话检测 | 低—由 YAML 控制 |
| `event_bar_sync.py:116,151` | BTC 24/7 不跳过轮询，XAU 周末跳过 | 低 |
| `feature_assembler.py:85,191-195` | BTC 37-dim 含 BTC/XAU 比率特征 | 低 |

## 有问题的分叉 ⚠️

### 1. BTC 校准的 reentry 阈值无条件用于 XAU

**位置**: `reentry_guard.py:139-143, 247-248`

```python
# FIX-20260603-069: BTC-calibrated thresholds.
_sl_cooldown = 300  # XAU 原为 180
_sl_penalty = 0.15  # XAU 原为 0.10
```

设计文档要求按品种参数化（注释明确写了 `XAU defaults: cooldown=180s, confidence_penalty=0.10`），但从未实现。XAU 被迫使用更严格的 BTC 阈值。

**影响**: XAU 低时间框架策略（statarb, micro）被不必要的严格阈值惩罚。

### 2. 制度趋势信念阈值因 BTC 全局降低

**位置**: `regime_gate.py:167`

```python
# FIX-20260602-053: 0.30→0.15 for BTC compatibility
```

所有品种的趋势信念阈值从 0.30 降至 0.15。XAU 策略可能在高噪声制度中保持全仓位，原本预期会缩减。

### 3. 脆弱的字符串匹配回退

**位置**: `market_ingress.py:104-107`

```python
elif "BTC" in symbol.upper():
    _price_min, _price_max, _max_spread = _BTC_PRICE_MIN, _BTC_PRICE_MAX, _BTC_MAX_SPREAD
else:
    _price_min, _price_max, _max_spread = _GOLD_PRICE_MIN, _GOLD_PRICE_MAX, _DEFAULT_MAX_SPREAD
```

仅在 ASSET_REGISTRY 查找失败时执行（生产环境不触发）。但如果 registry 导入失败，会走此路径。

## 建议

1. **P1**: 将 reentry 阈值按品种参数化（通过 `config` 或资产元数据注入）
2. **P2**: 评估 trend_conviction 0.15 对 XAU 的影响并考虑分品种设置
3. **P3**: `market_ingress.py` 回退路径改为 registry 驱动的查找
