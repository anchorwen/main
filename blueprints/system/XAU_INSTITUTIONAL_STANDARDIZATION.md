# XAU 机构级标准化蓝图 — Schema/大脑/策略/训练 全资源整合

> **Docket ID**: DQAF-20260615-010  
> **日期**: 2026-06-15  
> **性质**: 机构级架构标准化 (Institutional Standardization)  
> **范围**: XAU 全资源——大脑、Schema、策略、训练管线、特征计算机

---

## 一、Schema 维度地图

### XAU 专属 Schema

| Schema | 维度 | 组成 | 用途 |
|--------|------|------|------|
| `swing_enhanced_35` | 35 | 24 宏 + 9 微 + 2 TF | **主力**: M15/M30/H1/H4 Swing |
| `daily_swing_24` | 24 | 纯宏 (含黄金/白银比) | 日线 Swing |
| `v9_institutional_40` | 40 | 4TF × 10 特征 | 通用技术面 (符号无关) |
| `v9_micro_49` | 49 | 40 + 9 微结构 | 微结构增强 |
| `v4.3_microstructure_9` | 9 | tick/OFI/交叉 | 微结构 |

### BTC 专属 Schema

| Schema | 维度 | 用途 |
|--------|------|------|
| `btc_macro_enhanced_37` | **41** | BTC 宏增强 (AUDJPY, BTC/黄金比, regime derivatives) |

### 过渡/精简 Schema (XAU 衍生)

| Schema | 维度 | 用途 |
|--------|------|------|
| `swing_enhanced_29` | 29 | BTC 优化版 (去除 XAU 交叉资产) |
| `swing_enhanced_21` | 21 | 纯宏 (无微结构/TF) |

### ⚠️ 危险别名

| 别名 | 解析到 | 风险 |
|------|--------|------|
| `swing_enhanced_37` | `btc_macro_enhanced_37` (BTC 41维) | **高危**: 名称看起来像 XAU 但指向 BTC。V10_H1 因此被废。 |

---

## 二、大脑全量清单 (21 + 2 支持文件)

### ACTIVE (5) — 当前实盘

| Brain ID | TF | Schema | 维度 | Magic | WR | 状态 |
|----------|-----|--------|------|-------|-----|------|
| Swing_V9_M30_V2 | M30 | swing_enhanced_35 | 35 | 90320 | 16.2% (37笔) | ✅ 主力 |
| Swing_V9_M15_V2 | M15 | swing_enhanced_35 | 35 | 90310 | 7.1% (28笔) | ⚠️ 弱 |
| Swing_V9_H1_V2 | H1 | swing_enhanced_35 | 35 | 90330 | 50.0% (8笔) | ✅ 刚恢复 |
| Swing_V9_H4_V2 | H4 | swing_enhanced_35 | 35 | 90340 | 0.0% (3笔) | ⚠️ 刚恢复 |
| OU_Params_V7_M15 | M15 | v6_price_series_1 | 1 | 90103 | 6.5% (31笔) | ⚠️ statarb_m15 disabled |

### BROKEN (1) — 需重训

| Brain ID | 问题 | 当前状态 |
|----------|------|---------|
| **Swing_V10_H1_Directional** | schema=`swing_enhanced_37`→BTC alias (41维), 模型37维, 配置声明35维→三方不一致 | disabled in live.yaml |

### DISABLED (15) — 在 live.yaml 中但未启用

| Brain ID | TF | Schema | 维度 | 原因 |
|----------|-----|--------|------|------|
| Swing_V10_M15 | M15 | swing_enhanced_35 | 35 | 备选 |
| Swing_M15_V3 | M15 | swing_enhanced_35 | 35 | 备选 |
| Swing_LGB_M15_V1 | M15 | swing_enhanced_35 | 35 | LightGBM 变体 |
| Swing_LGB_M30_V1 | M30 | swing_enhanced_35 | 35 | LightGBM 变体 |
| Brain_Trend_M30_V1 | M30 | swing_enhanced_35 | 35 | 备选 |
| Brain_Trend_M30_V2 | M30 | swing_enhanced_35 | 35 | 备选 |
| Brain_Trend_V10_M30 | M30 | swing_enhanced_35 | 35 | 备选 |
| Brain_Rev_M30_V1 | M30 | swing_enhanced_35 | 35 | archived (均值回归) |
| Brain_Rev_M30_V2 | M30 | swing_enhanced_35 | 35 | archived (均值回归) |
| Barrier_V9_12B_V1 | M5 | swing_enhanced_35 | 35 | barrier 策略 disabled |
| Barrier_V9_12B_V2 | M5 | swing_enhanced_35 | 35 | barrier 策略 disabled |
| Meta_Stage1_Huber_V1 | M5 | v9_institutional_40 | 40 | archived |
| Meta_Stage1_Binary_Cls_V1 | M5 | v9_institutional_40 | 40 | archived |
| Meta_Stage1_MetaLabel_Binary_V1 | M5 | v9_institutional_40 | 40 | archived |
| OU_Params_V6_Sniper | M5 | v6_price_series_1 | 1 | frozen |

### PHANTOM (2) — live.yaml 中有但文件不存在

| 路径 | 状态 |
|------|------|
| `configs/brains/lgb_barrier_12bar_lightgbm_v3_20260517_084114.json` | 文件不存在 |
| `configs/brains/xgb_barrier_12bar_xgboost_v3_20260517_084031.json` | 文件不存在 |

---

## 三、策略线配置

| 策略 | 启用 | TF | SL | TP | RR | Breakeven WR | 大脑数 | 状态 |
|------|------|-----|-----|-----|-----|-------------|--------|------|
| m15_swing | ✅ | M15 | 3.0×ATR | 1.5×ATR | 0.50 | 66.7% | 1 | 97% neutral |
| m30_swing | ✅ | M30 | 3.0×ATR | 1.5×ATR | 0.50 | 66.7% | 1 | 活跃交易中 |
| h1_swing | ✅ | H1 | 3.0×ATR | 2.0×ATR | 0.67 | 60.0% | 1 | 刚恢复 |
| h4_swing | ✅ | H4 | 3.0×ATR | 2.0×ATR | 0.67 | 60.0% | 1 | 刚恢复 |
| structural | ✅ | M5 | 3.0×ATR | 1.5×ATR | 0.50 | 66.7% | 0 (规则) | 100% SHORT 阻塞 |
| statarb_dynamic | ❌ | M5 | — | — | — | — | 0 | 策略线 disabled |
| statarb_m15 | ❌ | M15 | — | — | — | — | 1 (OU) | 策略线 disabled |
| barrier/micro/daily | ❌ | — | — | — | — | — | 多个 | 全部 disabled |

---

## 四、训练数据集

### XAU 35 维 (swing_enhanced_35)

| 数据集 | TF | 样本 | SL/TP | 日期 |
|--------|-----|------|-------|------|
| swing_m5_enhanced | M5 | 9,673 | 1.5/1.5 | 05-30 |
| swing_m15_sl3_tp1.5 | M15 | 10,815 | 3.0/1.5 | 06-04 |
| swing_m30_sl3_tp1.5 | M30 | 5,409 | 3.0/1.5 | 06-04 |
| swing_h1_sl3_tp2 | H1 | 2,690 | 3.0/2.0 | 06-04 |
| swing_h4_sl3_tp2 | H4 | 717 | 3.0/2.0 | 06-04 |

### XAU 37 维 (旧 swing_enhanced_37, 已废弃)

| 数据集 | TF | 样本 | SL/TP | 日期 |
|--------|-----|------|-------|------|
| xau_directional_h1 | H1 | 10,032 | 2.0/3.5 | 06-11 |
| xau_directional_m15 | M15 | 9,243 | 2.0/3.5 | 06-11 |

> ⚠️ 这 2 个 37 维数据集是用旧版 `train_btc_swing_v9` (当时 37 维) 构建的。现在 v9 升级到 41 维，训练脚本 `train_xau_directional_v1.py` 已无法重建这些数据集。

---

## 五、关键问题与修复路线

### 🔴 P0: swing_enhanced_37 别名是定时炸弹

```
"swing_enhanced_37" → "btc_macro_enhanced_37" (BTC 41维)
```

任何 XAU 大脑引用此 schema → 静默解析为 BTC 特征 → 维度不匹配 → 大脑被丢弃。

**修复**: 删除此别名。改为在 `SCHEMA_DIMENSIONS` 中显式注册 `swing_enhanced_37` (如果曾经存在) 或彻底移除。BTC 训练脚本直接使用 `btc_macro_enhanced_37`。

### 🔴 P1: XAU 方向性训练管线断裂

`train_xau_directional_v1.py` 调用 `v9.compute_feature_row()` 返回值已从 37 维 list 变为 `(41维list, float, float)` tuple → 无法构建新数据集。

**修复**: 
1. 将 XAU 方向性训练脚本改为使用 XAU 专属特征计算机
2. 输出 schema 应为 `swing_enhanced_35` (35 维)
3. 用 SL=2.0/TP=3.5 (h1_swing 参数) 重建 H1 训练集

### 🟠 P2: V10_H1 重训

当前状态: 模型 37 维, schema 声明 35 维, 实际 schema=BTC 41 维 → 三方分裂。

**修复路径**:
1. 修复 P1 (训练管线)
2. 用 35 维特征 + `swing_enhanced_35` schema 重训
3. 部署 → 替换 V9_H1_V2 → 恢复方向性 H1 大脑

### 🟡 P3: Phantom 条目清理

`live.yaml` 中有 2 个 brain 引用指向不存在的文件。

**修复**: 从 `live.yaml` registry_entries 中移除。

### 🟡 P4: 非标准 schema 名称

`v9_40dim` 和 `ou_price` 不在 SCHEMA_DIMENSIONS 中（通过 feature_schema_id 间接解析）。

**修复**: 统一使用 `feature_schema_id` 中的标准名称。

---

## 六、标准化规范

### Schema 命名规范

```
XAU: swing_enhanced_{N}   (N = 21, 29, 35)
BTC: btc_macro_enhanced_{N} (N = 37, 但实际 41)
通用: v9_institutional_40, v9_micro_49, v6_price_series_1

禁止: 跨品种别名 (如 swing_enhanced_37 → btc_macro_enhanced_37)
```

### 训练输出规范

```
Schema 声明: feature_schema = feature_schema_id = 标准名称
维度: n_features = SCHEMA_DIMENSIONS[标准名称]
特征列表: features = get_schema_feature_names(标准名称)
模型文件: num_features == SCHEMA_DIMENSIONS[标准名称]
```

### Magic Number 规范

```
同一策略线内: 所有大脑共享相同 magic
不同策略线: 必须使用不同 magic

XAU 当前:
  m15_swing:    90310
  m30_swing:    90320
  h1_swing:     90330
  h4_swing:     90340
  structural:   90501
```

---

## 七、关联文档

- [CROSS_ASSET_CONTAMINATION_AUDIT.md](CROSS_ASSET_CONTAMINATION_AUDIT.md) — XAU/BTC 交叉感染审计
- [FIX_REGISTRY.md](FIX_REGISTRY.md) — 修复登记
- [MIA_GHOST_POSITION_PIPELINE.md](MIA_GHOST_POSITION_PIPELINE.md) — MIA 管道
- [runtime_live.md](../modules/runtime_live.md) — 运行时蓝图
