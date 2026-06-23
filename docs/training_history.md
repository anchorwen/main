---
name: Training history
description: Record of successful training runs, their datasets, and results
type: project
originSessionId: 2cd4f4a3-5fa6-46a1-b679-a6264f1478dd
---

# Training Run: CRT.sur.chlg.g2026.1 (2026-05-05)

**Why:** First successful end-to-end training run. Bootstrapped the training pipeline with fresh historical data from the new Exness MT5 terminal.

**Dataset:**
- Source: Exness MT5 at `D:\exness\MetaTrader 5 EXNESS1\terminal64.exe`
- Period: 2025-11-06 to 2026-05-05 (180 days, 34,320 M5 bars)
- Features: 6,813 vectors × 40-dim V9 Institutional (M5/M15/M30/H1)
- Labels: 13.1% TP / 34.9% SL / 52% timeout (SL:TP = 2.65:1 on base contract)

**Results:**
- Val accuracy: 69.46% (3-class, random baseline = 33.3%)
- ONNX model: `data/models/crt_sur_chlg_g2026/CRT.sur.chlg.g2026.1.40dim.onnx`
- Status: shadow (inference only, not live execution)

**How to apply:** When retraining is needed, run:
`python scripts/training/train_from_csv.py --csv data/raw/xauusdc_m5_180d.csv --epochs 100`
Then export new data first: `python scripts/training/export_mt5_data.py --mt5-terminal-path "D:\exness\MetaTrader 5 EXNESS1\terminal64.exe" --days 180`

---

# Training Run: XGBoost V9 P&L Regression (2026-05-07)

**Why:** Classification target (win/loss binary) showed near-zero feature correlation (max 0.055). Switched to P&L regression — predict realized trade P&L directly from 40-dim features.

**Dataset:**
- train.npz: 54,833 samples × 40 features (dataset_builder, indexed join)
- val.npz: 13,709 samples
- P&L distribution: mean=-0.89, std=14.67, winners=12,538, zeros(timeout)=28,375, losers=13,920

**Results (XGBoost reg):**
- train R²=0.415, train RMSE=11.22
- val R²=0.257, val RMSE=9.04 (baseline zero-pred RMSE=14.67, R²=0.0)
- 162 trees (early stopped from 200)
- Model: `artifacts/brains/xgboost/xgb_s42_reg.json`
- Adapter: XGBoostBrainAdapter → _score_to_direction (raw_score > 0.1 → long)

**How to apply:** `python scripts/training/trainers/xgb_trainer.py --data data/training/train.npz --val-data data/training/val.npz --output-model artifacts/brains/xgboost/xgb_s42_reg.json --mode reg`

---

# Training Run: LightGBM V1 P&L Regression (2026-05-07)

**Why:** Same data, same regression target. LightGBM slightly outperforms XGBoost (train R² 0.428 vs 0.415).

**Results (LightGBM reg):**
- train R²=0.428, train RMSE=11.10
- val R²=0.258, val RMSE=9.04
- 136 rounds (early stopped)
- Model: `artifacts/brains/lightgbm/lgb_s42_reg.txt`
- Adapter: LightGBMBrainAdapter → _score_to_direction (raw_score > 0.1 → long)

**How to apply:** `python scripts/training/trainers/lgb_trainer.py --data data/training/train.npz --val-data data/training/val.npz --output-model artifacts/brains/lightgbm/lgb_s42_reg.txt --mode reg`

---

# Training Run: DeepResMLP V1 P&L Regression (2026-05-07)

**Why:** DeepResMLP model architecture converted from 3-class classification to P&L regression. 3 heads (direction logits + risk + vol) → 3 heads (regression + risk + vol). V9OnnxBrainAdapter extended to auto-detect regression format via output shape (1,1 vs 1,3).

**Results (DeepResMLP reg):**
- train R²=0.484, train RMSE=10.54
- val R²=0.369 (43% better than XGBoost/LightGBM at 0.257)
- 129 epochs (early stopped from 200)
- 39,811 parameters
- Model: `artifacts/brains/deep_res_mlp/drm_s42_reg.onnx`
- Adapter: V9OnnxBrainAdapter → regression detection via shape check → _score_to_direction

**How to apply:** `python scripts/training/trainers/deep_res_mlp_trainer.py --data data/training/train.npz --val-data data/training/val.npz --output-model artifacts/brains/deep_res_mlp/drm_s42_reg.onnx --mode reg`

**Architecture change:**
- head_direction(128→3) → head_regression(128→1)
- Loss: cross_entropy → MSE
- Metrics: accuracy → R²/RMSE
- ONNX output: ["direction","risk","vol"] → ["regression","risk","vol"]

---

# Training Run: OU Params V7 Optuna (2026-05-07)

**Why:** Upgraded OU statistical arbitrage from 324-combination grid search to Optuna TPE Bayesian optimization (300 trials). Added Kalman filter for dynamic half-life tracking and ADX-based trend mute. Fixed max_drawdown_pct numerical explosion bug (division by near-zero equity).

**Dataset:**
- Source: `data/raw/xauusdc_m5_180d.csv` (34,320 M5 bars, 180 days)
- Range: 3967.68 – 5586.78

**Results (Optuna TPE, 300 trials):**
- Window: 250, Z-Entry: 1.3σ, Z-Exit: 1.0σ
- Max Half-Life: 58 bars, Theta Min: 0.00142
- Trades: 354 (~2/day), Winrate: 67.8%
- Sharpe: 0.54 (properly annualized by trade count)
- Total PnL: +$147.61, Profit Factor: 1.06
- Max DD: 73.9% (inherent to mean-reversion on trending gold)
- Artifact: `data/models/arb_params_v7.json`
- Adapter: ParamsBrainAdapter → receives raw price, computes OU z-score, signals on deviation

**Key fixes:**
- Sharpe annualization: `sqrt(288*252)` → `sqrt(trades/year)` — was inflating Sharpe 100x
- max_drawdown_pct: `(peak - equity) / (peak + 1e-8)` → denom `max(|peak|, 1.0)`
- Price routing: OU Params now receives raw mid_price instead of V9 feature vector (was always neutral before)
- Z-entry threshold: 1.3σ means fires only on significant deviations (~65 points on XAUUSD)

**How to apply:** `python scripts/training/trainers/arb_trainer.py --manifest-path data/training/arb_v6/manifest_arb42.json --result-json-path data/training/arb_v6/result_v7.json --artifact-path data/models/arb_params_v7.json --dataset-csv data/raw/xauusdc_m5_180d.csv --n-trials 300 --timeout 600`

**Note:** Strategy has modest standalone Sharpe (0.54) and high drawdown — its value is ensemble diversity (only non-ML signal), not standalone profitability.

---

# Training Run: Transformer V5 Microstructure (2026-05-07)

**Why:** Old V4.3 model always output neutral (signal_rate=0%). Root cause: scaler fitted on wrong distribution (mean=-3.4e-7, scale=0.0003). Built in-repo trainer with upgraded architecture (d_model 64→96, seq_len 64→32, RegimeContext pathway).

**Dataset:**
- Source: `D:\ai\Meta_ppo_v4.5\V4_Train_Tensors.pt` (49,463 samples, from Meta_ppo_v4.5 project)
- Used: First 10,000 samples (sequential), last 32 bars of each 64-bar sequence
- Features: 9-dim microstructure × 32-bar sequence (tick_return, hl_ratio, co_ratio, avg_spread, OIM, tick_velocity, XAGUSDc_return, EURUSDc_return, USDJPYc_return)
- Labels: Binary win/loss (51.2% positive)

**Results:**
- train_accuracy: 89.1%, train_R²=0.687
- val_accuracy: 74.9%, val_R²=0.293
- signal_rate: 96.4% (vs 0% for old V4.3) — long_rate=44.3%, short_rate=52.1%
- 100 epochs, 245,121 parameters
- Train time: 797s (~13min CPU)
- ONNX: `data/models/transformer_v5.onnx` (217,697 bytes)
- Adapter: TransformerBrainAdapter → SEQ_LEN=32, ONNX inference → _score_to_direction

**Architecture:**
- feature_embedding(9→96) + pos_encoding(32×96)
- RegimeContext: concat(seq_mean, seq_std) → Linear(18→96)→GELU→Linear(96→96)
- TransformerEncoder(d_model=96, n_heads=4, num_layers=2, dropout=0.15)
- GlobalMeanPool + RegimeContext → decoder(96→64→1)
- BCEWithLogitsLoss with pos_weight for balanced data

**Key fixes during development:**
- Kaiming init on output layer (fan_out=1): std=sqrt(2)/1=1.41 → logits mean=21, sigmoid saturated → fixed with std=1e-3 for output layers
- OneCycleLR pct_start=0.1 starved learning → removed scheduler, constant LR works
- Random sampling destroyed temporal structure (val_acc=54%) → sequential sampling (val_acc=74%)
- Batch size 128, lr=1e-3, AdamW, gradient clipping max_norm=1.0

**How to apply:** `python scripts/training/trainers/transformer_trainer.py --data "D:/ai/Meta_ppo_v4.5/V4_Train_Tensors.pt" --output-model data/models/transformer_v5.onnx --output-result data/models/transformer_v5_result.json --max-samples 10000 --epochs 100`

**Adapter config:** SEQ_LEN=32, loads ONNX via onnxruntime, inference time 0.77ms, buffer warmup 32 cycles (~32min at 60s interval vs old 64min).

---

# Training Run: XGBoost/LightGBM Calibrated Barrier (2026-05-14)

**Why:** 旧标签合约 (SL=2.0, TP=3.5) 数学上无法盈利。用盈利能力曲面校准找到盈利配置 (SL=3.0, TP=1.0)，过滤超时标签，训练二分类器预测 TP vs SL。

**Dataset:**
- Source: `data/raw/xauusdc_m5_180d.csv` (34,320 M5 bars, 180 days)
- Labels: 51,983 TP/SL-only samples (80.8% TP, 19.2% SL, EV=+0.231R)
- Features: 40-dim v9_institutional, 历史从 OHLC CSV 计算 (420-bar 回看)
- Train/Val: 41,586 / 10,397 (80/20 chronological split)

**Results (XGBoost, Optuna TPE 50 trials):**
- Best Optuna Sharpe: 2.646
- Best seed Sharpe: 2.2294, Win rate: 79.96%, Profit Factor: 1.38
- Confidence filtering: thresh>0.85 → 2,889 trades, EV=+0.371R (84.3% TP)
- Model: `data/models/institutional/XGBOOST_barrier_12bar_*.json`
- Brain config: `configs/brains/XGBOOST_barrier_12bar_*.json`

**Results (LightGBM, Optuna TPE 50 trials):**
- Best Optuna Sharpe: 2.525
- Best seed Sharpe: 2.0055, Win rate: 79.96%, Profit Factor: 1.34

**Key insights:**
- 模型始终预测 TP (基础率 80.8%)，但置信度变化有价值
- 高置信度 (>0.85) 显著提升 EV (+0.371R vs 基础 +0.231R)
- 超时标签有害：含超时 EV 为负 (-0.0174R)，过滤后 EV 变正
- 3:1 不利比率下最大回撤 99R，需要仓位管理

**How to apply:**
```
python scripts/training/build_profitable_labels.py --price-data data/raw/xauusdc_m5_180d.csv --output data/labels/calibrated_barrier_labels.jsonl
python scripts/training/build_calibrated_dataset.py --labels data/labels/calibrated_barrier_labels.jsonl --price-data data/raw/xauusdc_m5_180d.csv --output-dir data/training/calibrated_v1
python scripts/training/institutional_train.py --data data/training/calibrated_v1_tpsl/train.npz --arch all --contract barrier_12bar --optuna-trials 50 --n-seeds 5 --output-dir data/models/institutional --register
```

---

# Training Run: M15/M30/H1/H4 Swing Multi-Timeframe (2026-05-15)

**Why:** M5-bar v9_institutional_40 features showed zero predictive signal (AUC 0.52, |corr|<0.02) for 12-bar labels. Switched to swing datasets with 24 features across 4 timeframes. Each timeframe got XGBoost + LightGBM training (8 models total).

**Datasets** (swing NPZs with pre-split train/val/test, 24 features, -1/0/1 labels, pnl_r in R-multiples):
- `data/training/m15_swing_24bar.npz` — 37,447 train / 7,470 val / 4,996 test, 24-bar horizon
- `data/training/m30_swing_12bar.npz` — 37,469 / 7,483 / 4,997, 12-bar horizon
- `data/training/h1_swing_24bar.npz` — 11,194 / 2,219 / 1,495, 24-bar horizon (best: LR AUC 0.695)
- `data/training/h4_swing_18bar.npz` — 6,464 / 1,278 / 864, 18-bar horizon (small, overfit risk)

**Pipeline**: `python scripts/training/train.py --contract configs/training/{tf}_swing_{arch}.yaml`
- TrainingContract v2.1 YAML → Optuna TPE 50 trials → Multi-seed (3-5) → Quality Gates → SQLite Registry
- Pre-split NPZ auto-detection added to pipeline

**Results:**

| Timeframe | Best Arch | Train Sharpe | Forward Sharpe | Overfit Gap | LR AUC |
|-----------|-----------|-------------|---------------|-------------|--------|
| M15 | XGBoost | 3.54 | 3.58 | 0.03 | 0.614 |
| M30 | XGBoost | 4.18 | 4.50 | 0.32 | 0.597 |
| H1 | LightGBM | 8.73 | 8.21 | 0.52 | 0.695 |
| H4 | XGBoost | 5.19 | 5.14 | 0.06 | 0.646 |

**Brain configs generated:** `configs/brains/xgboost_m15_swing_*.json`, `xgboost_m30_swing_*.json`, `xgboost_h1_swing_*.json`, `lightgbm_h1_swing_*.json`, `xgboost_h4_swing_*.json`

**Key findings:**
- H1 has strongest predictive signal (AUC 0.70, |corr| up to 0.35)
- XGBoost more robust (3/4 timeframes, lower overfit)
- LightGBM highest absolute forward Sharpe on H1 (8.21)
- M5 features fundamentally don't predict 12-bar outcomes (abandoned)
- All models in "shadow" status (inference-only, not live execution)

**Note:** Sharpe values inflated by ±1 return simulation. Relative ranking is meaningful; absolute values are not comparable to live trading.

**How to apply:**
```
# Train all timeframes
python scripts/training/train.py --contract configs/training/m15_swing_xgboost.yaml
python scripts/training/train.py --contract configs/training/m30_swing_xgboost.yaml
python scripts/training/train.py --contract configs/training/h1_swing_lightgbm.yaml
python scripts/training/train.py --contract configs/training/h4_swing_xgboost.yaml

# View registry
python -c "from core.training.training_registry import create_registry; r=create_registry('data/training/registry.db'); [print(f'{x.contract_id}: {x.arch} fw={x.forward_sharpe:.2f}') for x in r.list_runs()]"
```

---

# Meta-Labeling v2.0: Two-Stage Architecture (2026-05-15)

**Why:** 用户发现致命数据泄露 bug (_atr_percentile 全局排序泄露未来波动率) 和架构缺陷 (Stage 1 启发式胜率 47% 低于随机)。分析显示 M5/12bar 特征无法预测方向 — 三种 ML 方法 (回归/3分类/双模型) 验证方向准确率均不超 49%。保留启发式 Stage 1，用因果滚动窗口 + 非对称样本权重 + PnL 加权评估重建 Stage 2 元模型。

**关键发现:**
- M5 频段 40 维特征对 12bar 方向预测无信号 (连续回归 val R²=-0.002, 3分类 val acc=45.7%, 双模型 val acc=28.6%)
- 元数据数据集 bug: 按 ROW 而非按 BAR 遍历，每个 bar 两行 (long/short) 导致非匹配侧被错误计为亏损
- 修复后元数据: 26,318 bar 级样本 (从 52,636 行减半)，胜率 58.2%

**Dataset (Meta v2):**
- Source: `data/training/calibrated_12bar_v3/train.npz`
- Meta samples: 26,318 bar-level (15 features), 58.2% win rate
- Train/Val: 21,054 / 5,264 (chronological 80/20)
- Sample weights: SL loss → 2.0, TP win → 1.0 (avg=1.418)
- ATR percentile: 500-bar causal rolling window (no future leak)
- Schema: meta_label_v2, saved as `data/training/meta_12bar_v2/train.npz`

**Results (LightGBM Meta Model, Optuna 30 trials × 3 seeds):**
- Base win rate: 55.7%, Base expected PnL: -0.49R (negative EV)
- **Kept win rate: 74.4%, Kept expected PnL: +0.35R (positive EV!)**
- Precision improvement: +18.7%, Signal retention: 4.24%
- Threshold: 0.42, Net PnL improvement: +0.036R per unfiltered trade
- Model: `data/models/meta_filter_12bar_v2/meta_model_20260515_063944.txt`
- Top features: h1_macd (35.7%), atr_percentile (17.6%), s1_direction (10.2%)

**Key insights:**
- 对称权重下元模型几乎不学习 (round 1 即 best iteration)
- 非对称权重 (SL=2.0, TP=1.0) 强制模型优先避免灾难性亏损
- PnL 加权评估比纯胜率改进更能反映真实交易价值
- 信号留存率 4.24% 偏低 (每 100 个信号仅 4 个通过)，实际应用中可降低阈值换取更多交易
- 元模型将负 EV (-0.49R) 转为正 EV (+0.35R)，证明两阶段架构可行

**Known limitations:**
- Stage 1 方向预测准确率 47% (低于随机)，元模型只能过滤而非纠正方向错误
- 信号留存率低意味着实盘交易频率受限
- 模型对 H1 MACD 特征高度依赖 (35.7% importance)，H1 特征可用性是关键风险

**How to apply:**
```
# 重建元数据
python scripts/training/build_meta_labels.py --dataset data/training/calibrated_12bar_v3/train.npz --output data/training/meta_12bar_v2/train.npz

# 训练元模型
python scripts/training/train_meta_model.py --dataset data/training/meta_12bar_v2/train.npz --output data/models/meta_filter_12bar_v2 --optuna-trials 30 --n-seeds 3
```
