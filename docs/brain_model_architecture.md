# 大脑模型架构全景图

> 生成时间: 2026-05-23 | 基于当前代码状态

---

## 一、总览树

```
                        Quant OS 模型体系
                              │
        ┌─────────────────────┼─────────────────────┐
        │                     │                     │
   【Alpha 大脑】         【Meta 管线】           【治理层】
   产生原始信号            对信号做质量过滤        管理大脑生命周期
        │                     │                     │
   ┌────┴────┐           ┌────┴────┐           ┌────┴────┐
   │         │           │         │           │         │
  7种适配器  12个合约组   Stage2    MetaExit   状态机    权重
  7个活跃脑  11条策略线   过滤器    引擎       (5状态)   (动态)
```

---

## 二、Alpha 大脑 — 7 种适配器

### 2.1 适配器注册表

```
                   BRAIN_TYPE_MAP (brain_type → registry_key)
                  ┌──────────────────────────────────────────┐
                  │                                          │
  brain_type      │  registry_key       Adapter Class        │  模型格式
  ─────────       │  ────────────       ─────────────        │  ────────
  onnx_v9         │→ "onnx"           → V9OnnxBrainAdapter   │  ONNX (3输出)
  deepresmlp      │→ "onnx"           → (同上)               │  ONNX (1输出)
  lightgbm_v1     │→ "lightgbm_txt"   → LightGBMBrainAdapter │  LightGBM .txt
  xgboost_v9      │→ "xgboost_json"   → XGBoostBrainAdapter  │  XGBoost .json
  xgboost_v4.5*   │→ "xgboost_json"   → XGBoostBrainAdapter  │  XGBoost .json
  ou_params_v6    │→ "ou_params_json" → ParamsBrainAdapter   │  JSON (θ,μ,HL)
  online_sgd      │→ "online_sgd"     → OnlineLearnerAdapter │  MLP/SGD (在线)
  transformer_v5* │→ "transformer_onnx"→TransformerBrainAdapter│ ONNX (时序)
                  │                                          │
                  └──────────────────────────────────────────┘
```

### 2.2 各适配器详解

#### A. LightGBMBrainAdapter — 梯度提升树
```
文件: core/brains/adapters/lightgbm_brain_adapter.py

输入特征: 由 brain config JSON 的 features 列表决定 (40维 V9 或 24维 swing)
模型格式: LightGBM Booster (.txt)
输出模式:
  ┌─ 多分类 (num_class>2): raw_score = P(long) - P(short)
  │   (Class 0=short, Class 1=neutral, Class 2=long)
  └─ 回归: raw_score = float(pred[0])

Signal 生成:
  direction:  |raw_score| > 0.10 → long/short, 否则 neutral
  confidence: max(up_prob, down_prob)
              up_prob   = 0.5 + tanh(|raw_score|)/2
              down_prob = 0.5 - tanh(|raw_score|)/2
  fallback:   全零特征 (< 1e-10) 或维度不匹配 → True
```

#### B. XGBoostBrainAdapter — 极端梯度提升
```
文件: core/brains/adapters/xgboost_brain_adapter.py

输入特征: 与 LightGBM 相同，配置驱动
模型格式: XGBoost Booster (.json)
输出模式: 与 LightGBM 完全一致 (多分类/回归)
Signal 生成: 与 LightGBM 完全一致 (共用 _score_to_direction)

特殊能力:
  run() 可接受3种输入格式:
    1. dict → feature_adapter.build_model_input() → (9,) 向量
    2. (n_bars, 9) ndarray → .flatten() → (n_bars*9,) 展平
    3. (n_bars*9,) ndarray → 直接使用
```

#### C. V9OnnxBrainAdapter — 深度残差网络
```
文件: core/brains/adapters/v9_onnx_brain_adapter.py

输入特征: 40维 V9 institutional (8特征 × 4周期 + 4 θ + 4 Hurst)
模型格式: ONNX (onnxruntime InferenceSession)

三种输出模式:
  ┌─ V9分类 (3输出): [logits(1,3), risk(1,1), vol(1,1)]
  │   direction: softmax → argmax → index2=long, index0=short, index1=neutral
  │   confidence: max(softmax_probs)
  │   raw_score: max(probs) - min(probs)
  │
  ├─ CRT分类 (1输出): [logits(1,3)]
  │   risk/vol 从 logits 推算
  │
  └─ 回归 (output[0].shape[-1]==1): [regression(1,1), risk(1,1), vol(1,1)]
      direction: |raw_score| > 0.10, 同 LightGBM

故障降级:
  无 ONNX session → Hurst 启发式特征推断方向
  子进程隔离: 可选 InferenceGuard (timeout=5s, max_restarts=3)
```

#### D. TransformerBrainAdapter — 时序注意力
```
文件: core/brains/adapters/transformer_brain_adapter.py

输入特征: 9维 microstructure (tick_return, hl_ratio, co_ratio,
         avg_spread, OIM, tick_velocity, XAGUSDc_return,
         EURUSDc_return, USDJPYc_return)
模型格式: ONNX QuantTransformer

核心机制 — 滚动缓冲区:
  seq_len = 64 (默认)
  buffer = deque(maxlen=64)
  
  每个 M5 cycle:
    feed_tick(9维特征向量) → 加入 buffer
    buffer 未满 (size < 64):
      → raw_score=0.0, fallback=True (静默，不产生信号)
    buffer 满 (size = 64):
      → stack → (1, 64, 9) tensor → ONNX 推理
      → raw_score, fallback=False

Bootstrap: bootstrap_buffer(历史特征) 预填缓冲区，避免冷启动
```

#### E. OnlineLearnerAdapter — 在线学习
```
文件: core/brains/adapters/online_learner_adapter.py

输入特征: 40维 V9 institutional
双后端:
  ┌─ OnlineMLP (PyTorch): LayerNorm + GELU, 3分类 softmax
  └─ SGDClassifier (sklearn): 线性模型, L2 正则化

标签映射: sl_hit/loss/short → -1, timeout/neutral → 0, tp_hit/win/long → 1

核心能力 — 在线增量学习:
  partial_fit(features, label, confidence):
    从实盘成交结果中学习，更新模型权重

  防漂移机制:
    _MAX_WEIGHT_DELTA = 0.30     (单步权重变化上限)
    _SNAPSHOT_INTERVAL = 10      (每10次更新保存快照)
    _MAX_DRIFT_EVENTS = 3        (第3次漂移后永久冻结)
    
    漂移判定: BOTH 条件同时满足
      1. max_delta > 0.30
      2. recent_loss_median > 1.5x baseline
    漂移时: 回滚到最近快照
```

#### F. ParamsBrainAdapter — OU 均值回归 (统计套利)
```
文件: core/brains/adapters/params_brain_adapter.py

输入特征: 1维 (价格序列)
模型格式: JSON (arb_params.json)

参数:
  window: 100          (回归窗口)
  z_entry: 2.0         (开仓阈值, Z-score)
  z_exit: 0.5          (平仓阈值)
  max_half_life: 20.0  (最长半衰期)
  theta_min: 0.005     (最小均值回归速度)

OU 参数估计 (OLS 回归):
  price_diff[t] = alpha + beta * price[t-1] + ε
  theta = -beta               (均值回归速度, >0 表示回归)
  mu = alpha / theta          (长期均值)
  half_life = ln(2) / theta   (半衰期, bar 数)
  z_score = (price - mu) / effective_std

Signal 生成:
  z_score < -z_entry  → long  (超跌, 预期回归向上)
  z_score > +z_entry  → short (超涨, 预期回归向下)
  |z_score| < z_entry → neutral

  置信度折扣:
    excess = |z_score| - z_entry
    base_conf = 0.5 + sigmoid(excess) * 0.45, ≤ 0.95
    hl_discount = max(0.3, 1.0 - half_life / max_half_life)
    confidence = base_conf * hl_discount
    (半衰期越长 → 均值回归越慢 → 置信度越低)

Bootstrap: bootstrap_buffer(prices) 预填价格缓冲区
```

#### G. MetaFilterAdapter — 元过滤器 (独立，非大脑)
```
文件: core/brains/adapters/meta_filter_adapter.py

模型格式: LightGBM 二分类 (.pkl)
输入: 47维特征 (由 MetaFilterGate 组装)
输出: P(breakeven/win | signal_fired)
阈值: 0.50 (默认)
```

---

## 三、当前活跃大脑清单 (7个)

```
活跃大脑 (governance 过滤后):
┌────────────────────────────────┬───────────────┬──────────────┬──────────┐
│ brain_id                       │ brain_type    │ contract_grp │ 特征维度 │
├────────────────────────────────┼───────────────┼──────────────┼──────────┤
│ CRT.sur.chlg.g2026.1           │ onnx_v9       │ barrier_12bar│ 40维 V9  │
│ DeepResMLP_V1_Institutional    │ deepresmlp    │ barrier_12bar│ 40维 V9  │
│ Meta_Stage1_Huber_V1           │ lightgbm_v1   │ barrier_12bar│ 40维 V9  │ ← 独裁者
│ Online_MLP_V1                  │ online_sgd    │ barrier_12bar│ 40维 V9  │
│ OU_Params_V6_Sniper            │ ou_params_v6  │statarb_dynamic│ 1维     │
│ OU_Params_V7_M15               │ ou_params_v6  │ statarb_m15  │ 1维     │ ← 新增
│ Microstr_Transformer_V5.0_H4   │ transformer_v5│ micro_h4     │ 9维 micro│
└────────────────────────────────┴───────────────┴──────────────┴──────────┘

治理惩罚:
  Meta_Stage1_Huber_V1: vote_weight 0.8 → 0.4 (probation)
  Online_MLP_V1:        vote_weight 1.2 → 0.6 (probation)
```

---

## 四、Parliament (议会) 与 Contract Groups (合约组)

### 4.1 12 个合约组

```
contract_groups.py 中定义的 12 组:

微结构系 (union 投票 — 任一检测到 TP 即可触发):
┌──────────────┬──────────────────────────────────────────────┐
│ micro_3bar   │ XGBoost_V4.5 + Transformer_V5.0              │ 9维微结构
│ micro_m15    │ XGBoost_V4.5_M15 + Transformer_V5.0_M15      │ M15
│ micro_h1     │ XGBoost_V4.5_H1 + Transformer_V5.0_H1        │ H1
│ micro_h4     │ XGBoost_V4.5_H4 + Transformer_V5.0_H4        │ H4 (仅门控)
└──────────────┴──────────────────────────────────────────────┘

统计套利系 (weighted 投票):
┌──────────────┬──────────────────────────────────────────────┐
│statarb_dynamic│ OU_Params_V6_Sniper                         │ M5
│ statarb_m15  │ OU_Params_V7_M15                            │ M15
└──────────────┴──────────────────────────────────────────────┘

摆动系 (weighted 投票):
┌──────────────┬──────────────────────────────────────────────┐
│ barrier_12bar│ Meta_Stage1_Huber_V1 (独裁者, solo voter)    │ 12bar
│ daily_swing  │ XGBoost_D1_Swing_5d                         │ 5天后
│ m15_swing    │ xgboost_m15_swing                            │ 24bar
│ m30_swing    │ xgboost_m30_swing                            │ 12bar
│ h1_swing     │ xgboost_h1_swing                             │ 24bar
│ h4_swing     │ xgboost_h4_swing                             │ 18bar
└──────────────┴──────────────────────────────────────────────┘
```

### 4.2 Union vs Weighted 投票模式

```
Weighted (加权投票):
  weight = vote_weight × confidence × (0.5 if fallback else 1.0)
  direction = sign(Σ weight[long] - Σ weight[short])
  confidence = Σ 获胜方向权重 / Σ 总权重
  处罚: 中立投票罚 -0.03/脑, 多数派加分

Union (联合投票 — 微结构专用):
  任一 brain 检测到 TP → 触发信号
  max-confidence tie-breaking
  多脑加分: +0.06 per additional agreeing brain (max +0.18)
  反对罚分: -0.12
  中立拖拽: -0.04 per neutral voter
```

---

## 五、Meta 管线 (信号质量过滤链)

### 5.1 总体架构

```
                        Meta Pipeline 全景
                              │
        ┌─────────────────────┼─────────────────────┐
        │                     │                     │
   【Track 2】            【Track 3】           【Track 4d】
  barrier_12bar          statarb_dynamic       barrier_12bar
  (Huber probe)          (OU signal)           (退路)
        │                     │                     │
        v                     v                     v
  MetaPipeline            MetaFilterGate        MetaSignalFilter
  ┌─ 9步执行链 ─┐         ┌─ 47dim ─┐           ┌─ 59dim ─┐
  │1. Volume guard│        │ LGB预测  │           │ LGB+MLP  │
  │2. Raw提取    │         │ P(win)   │           │ P(TP)    │
  │3. Direction   │        │ > 0.40?  │           │ Kelly    │
  │4. Stage2过滤  │        └──────────┘           └──────────┘
  │5. Dynamic SL  │
  │6. RR check    │              ┌──────────────────┐
  │7. Kelly 规模  │              │  Layer 2.5       │
  │8. Volume 计算  │              │  MetaExitEngine  │
  │9. StrategyDec  │              │  多因子退出评分   │
  └───────────────┘              └──────────────────┘
```

### 5.2 MetaPipeline 9 步详解

```
文件: core/execution/meta_pipeline.py

Step 1: Volume Guard
  if max_volume <= 0 → return None (shadow-only, no capital)

Step 2: Raw Score 提取
  从 BrainSignal.raw_score 读取 Stage1 探针的原始预测值

Step 3: Direction 映射
  raw_score < -0.30 → short
  raw_score > +0.30 → long
  否则 → None (方向不够强)

Step 4: Stage2 过滤 ← 核心质量闸
  输入: direction, raw_score, V9数组(40), Micro数组(9)
  经过 MetaSignalFilter.filter_arrays()
  → FilterResult.passed → P(TP|signal) 是否达标

Step 5: Dynamic SL/TP
  base_sl_atr_mult × ATR → SL 距离
  base_tp_atr_mult × ATR → TP 距离

Step 6: RR Check
  tp_dist / sl_dist >= min_rr_ratio
  不满足 → return None

Step 7: Kelly Sizing
  kelly_mult = (p_win * (rr + 1) - 1) / rr
  fractional_k = kelly_mult × 0.5 (fractional Kelly)
  裁剪到 [floor=0.5, cap=1.5]
  kelly_mult == 0 → return None (负期望值否决)

Step 8: Volume 计算
  risk_budget_usd / sl_distance × kelly_mult
  × regime_multiplier × z_depth_penalty × streak_reduction

Step 9: StrategyDecision
  包含 direction, confidence(=p_win), volume, SL, TP
```

### 5.3 MetaSignalFilter 内部结构 (Stage2)

```
文件: core/execution/meta_signal_filter.py

输入: 59维特征
  ┌─ 40维 V9 Institutional (8个特征 × 4周期 + 4θ + 4Hurst)
  ├─  9维 Microstructure (tick_return, hl_ratio, ...)
  └─ 10维 Runtime Meta (运行时计算)
       oof_pred:          Stage1 raw_score (bps单位)
       oof_pred_zscore_20: 20bar滚动Z-score
       atr_percentile_100: ATR百分位排名
       vol_zscore:        M5波动率Z-score
       hurst_m5:          M5 Hurst指数
       session_sin/cos:   UTC小时正弦/余弦编码
       spread_zscore:     点差Z-score
       oim_divergence:    OIM与tick_return符号分歧
       toxicity_score:    tick_velocity / ATR (≤10)

双模型 Ensemble:
  ┌─ LGB probability  (权重 0.6)
  └─ MLP probability  (权重 0.4)
       ↓
  Weighted Average
       ↓
  Platt Calibration (LogisticRegression on log-odds)
       ↓
  Conformal Thresholding (动态百分位阈值)
    - 窗口: 500 个最近预测
    - 百分位: 80%
    - 有效阈值 = max(percentile_threshold, min_threshold, base_threshold)
    - 时效衰减: 14天以前的预测不参与统计

阈值:
  base_threshold: 0.30
  min_threshold:  0.50
  conformal_percentile: 80%
```

### 5.4 MetaFilterGate (OU 策略专用，Track 3)

```
文件: core/execution/meta_filter_gate.py

输入: 47维特征
  ┌─ 40维 V9 Institutional
  ├─  9维 Microstructure
  ├─  1维 OU z_entry
  └─ 过滤到 feature_names.json (47 specific names)

模型: LightGBM 二分类 (meta_filter_lightgbm.pkl)
输出: P(breakeven)
阈值: 0.50 (构造函数), 0.40 (live_cycle.py 中的 META_FILTER_GATE_THRESHOLD)

仅用于: statarb_dynamic 策略
```

### 5.5 MetaExitEngine (Layer 2.5 退出评分)

```
文件: core/execution/meta_exit_engine.py

五因子加权评分 (启发式模式):
  w_pnl=0.30:      R-multiple 映射 [0,1]
                    R≥+1.5→0.0, R≤-1.5→1.0
  w_time=0.20:     时间衰减三阶段
                    0-50%→0.0, 50-80%→线性, 80-100%→二次, >100%→0.8
  w_regime=0.15:   趋势对齐度
                    对齐+确信→0.0, 强反向+高波动→1.0
  w_consensus=0.25: 共识漂移
                    共识改善→0.0, 漂移>0.4→0.85
  w_volatility=0.10: 波动扩张
                    ATR收敛>10%→0.0, 扩张>50%→0.9

Urgency = Σ(weight × score)
Threshold: urgency >= 0.65 → 触发退出

ML 模式 (可选):
  加载 LGB 模型 → P(win)
  urgency = 1.0 - P(win)

模型质量门槛:
  _MIN_WINS = 15       (最少胜场)
  _MIN_WIN_RATE = 0.20 (最低胜率)
  拒绝含 is_sl_hit/is_tp_hit 特征的模型 (数据泄露)
```

---

## 六、治理系统 (Governance)

### 6.1 大脑状态机

```
                         ┌──────────┐
          自动晋升 ─────→│  LIVE    │←───── 良好表现
         (通过影子或评估) │  (活着)  │
                         └──┬───┬──┘
                            │   │
              降级/表现下降 │   │ 健康信号=critical
                            ↓   ↓
              ┌──────────┐  ┌──────────┐
    表现回升→│PROBATION │  │  FROZEN   │←── 反复降级
            │ (缓刑)   │  │  (冻结)   │
            └────┬─────┘  └────┬─────┘
                 │             │
        连续恶化 │             │ freeze_count >= 3
                 ↓             ↓
              ┌────────────────────┐
              │     RETIRED        │ ← 终态
              │     (退役)         │
              └────────────────────┘

VALID_TRANSITIONS (合法状态转换):
  candidate  → {live, probation, retired}
  live       → {probation, frozen, retired}
  probation  → {live, frozen, retired}
  frozen     → {probation, retired}
  retired    → {} (不可逆)
```

### 6.2 两套评估机制

```
机制 A: 规则引擎 (GovernanceRuleEngine, 60秒周期)
  ┌─────────────────────────────────────────────────────────┐
  │ 8条默认规则，优先级驱动，同周期内选最严重 action       │
  │                                                         │
  │ P100: auto_freeze_critical     health=critical          │
  │ P110: auto_retire_repeated     冻结≥3次 或 2次+critical │
  │ P90:  auto_demote_degraded     health=degraded, live    │
  │ P85:  auto_promote_shadow      影子信号≥50, 长≥5, 短≥5  │
  │ P80:  auto_demote_prob→frozen   probation+degraded      │
  │ P75:  auto_promote_prob→live   sample≥100, 综合≥0.55    │
  │ P50:  auto_promote_healthy     综合≥0.75, sample≥30     │
  │ P40:  unfreeze_recovered       frozen→probation 恢复    │
  └─────────────────────────────────────────────────────────┘

机制 B: 晋升评估器 (BrainPromotionEvaluator, Auditor→Executor 模式)
  ┌─────────────────────────────────────────────────────────┐
  │ BrainPromotionThresholds:                               │
  │   promote_wr_candidate: 0.40   (候选→缓刑, WR≥40%)     │
  │   promote_wr_probation: 0.45   (缓刑→活着, WR≥45%)     │
  │   retire_wr: 0.30              (退役线, WR<30%)        │
  │   promote_pf_probation: 0.90   (PF≥0.90)               │
  │   promote_pf_active: 1.10      (PF≥1.10)               │
  │   retire_pf: 0.60              (PF<0.60)               │
  │   max_consecutive_losses: 8    (连续亏损上限)           │
  └─────────────────────────────────────────────────────────┘
```

### 6.3 关键指标解释

```
指标                  │ 公式 / 含义                         │ 用途
──────────────────────┼─────────────────────────────────────┼──────────
Win Rate (WR)         │ 胜场 ÷ 总成交                        │ 晋升/退役
Profit Factor (PF)    │ 总盈利 ÷ |总亏损|                    │ 晋升/退役
Sharpe Ratio          │ (日均收益 ÷ 日收益标准差) × √252    │ 质量评分
Composite Score       │ 0-100分, 综合评分                    │ 健康信号
                      │ = Sharpe(40%) + WR(25%) + PF(15%)   │
                      │   + PnL(10%) + Drawdown(10%)         │
Max Drawdown          │ 峰值到谷值的最大百分比回撤            │ 风险惩罚
Consecutive Losses    │ 连续亏损笔数                         │ 冻结/退役
Signal Count          │ 产生信号总次数                       │ 统计显著性

Composite Score → 质量等级:
  85-100: exceptional → vote_weight ×1.5
  70-84:  healthy     → vote_weight ×1.2
  50-69:  stable      → vote_weight ×1.0
  35-49:  warning     → vote_weight ×0.7
  20-34:  degraded    → vote_weight ×0.4
  10-19:  marginal    → vote_weight ×0.2
  <10:    critical    → vote_weight ×0.0
```

### 6.4 Dynamic Vote Weight (动态投票权重)

```
文件: core/brains/services/dynamic_brain_weighter.py

权重计算 (0.0 ~ 3.0):
  优先取 BrainPnLStore (实盘 P&L) > BrainPerformanceTracker (合成分)
  
  硬闸: PnL<0 AND WR<30% AND trades≥100 → weight=0
  
  连续映射:
    Sharpe → 基础因子
    WR 修正: ±15%
    Drawdown 惩罚
  
  冗余惩罚 (同组相似脑):
    检测: PnL 误差 < 5%, PnL 误差 < 15%, WR 差 < 6pp
    最佳脑: 保持全权重
    第二名: ×0.65
    第三+:  ×0.45
```

---

## 七、完整数据流 (从特征到执行)

```
M5 Tick 到达 (MT5)
  │
  ├─→ 特征计算 (每 M5 cycle)
  │     ├─ 40维 V9 Institutional (归一化)
  │     ├─  9维 Microstructure (Z-score)
  │     ├─ 24维 Daily/Swing (D1 级别)
  │     └─  1维 Price (OU 价格)
  │
  ├─→ 大脑推理 (BrainRunService.run_active_brains())
  │     ├─ CRT + DeepResMLP + Huber + Online_MLP → barrier_12bar
  │     ├─ OU_Params_V6 → statarb_dynamic
  │     ├─ OU_Params_V7_M15 → statarb_m15 (仅 M15 边界)
  │     └─ Transformer_H4 → micro_h4 (门控)
  │     输出: 7个 BrainSignal (方向 + 置信度 + raw_score)
  │
  ├─→ Parliament 共识 (contract_groups.compute_all_group_signals())
  │     ├─ barrier_12bar: 独裁者 (Huber solo vote)
  │     ├─ statarb_dynamic: 加权 (单一 OU 脑)
  │     └─ statarb_m15: 加权 (单一 M15 OU 脑)
  │     输出: 3个 ConsensusResult
  │
  ├─→ 策略线评估 (StrategyLine.evaluate() × 11条策略线)
  │     │
  │     ├─ barrier_12bar (Track 2):
  │     │     提取 Huber raw_score → direction → Stage2过滤(59维)
  │     │     → Dynamic SL/TP → RR check → Kelly → Volume
  │     │     → StrategyDecision (执行否决权, 覆盖 Parliament)
  │     │
  │     ├─ statarb_dynamic (Track 3):
  │     │     MetaFilterGate.filter(47维) → P(breakeven) > 0.40?
  │     │     counter_trend_action() → H1/H4 反趋势检查
  │     │     z-depth penalty → volume
  │     │
  │     ├─ statarb_m15 (M15边界门控):
  │     │     仅 UTC分钟 % 15 == 0 时评估
  │     │     MTFPriceService.latest_m15_close → mid_price
  │     │     counter_trend_action() → H1/H4 反趋势检查
  │     │
  │     └─ micro_*/swing_*: 标准路径 (共识 + 逆趋势门控)
  │
  ├─→ 下单调度 (ExecutionQueue.flush())
  │     └─ MT5 下单 → position_manager 跟踪
  │
  └─→ 持仓管理 (每 cycle)
        ├─ Layer 1: ATR trailing stop
        ├─ Layer 2: Brain-flip exit (大脑方向翻转)
        ├─ Layer 2.5: MetaExitEngine (多因子退出)
        ├─ Layer 3: Time exit (到达 horizon)
        └─ Layer 4: Emergency stop
```

---

## 八、参数速查表

### 8.1 信号阈值

| 参数 | 值 | 位置 |
|------|-----|------|
| 方向判定阈值 (LightGBM/XGBoost) | \|raw_score\| > 0.10 | adapters |
| 方向判定阈值 (MetaPipeline) | \|raw_score\| > 0.30 | meta_pipeline.py:40 |
| OU Z-entry | 2.0 | params_brain_adapter.py:41 |
| OU Z-exit | 0.5 | params_brain_adapter.py:41 |
| Stage2 过滤阈值 | 0.30 (base), 0.50 (conformal min) | meta_signal_filter.py |
| MetaFilterGate 阈值 | 0.40 (live), 0.50 (构造) | live_cycle.py:74 |
| 逆趋势门控 (statarb) | H1 block=0.55, H4 block=0.35 | strategy_line.py |

### 8.2 Kelly 规模

| 参数 | 值 | 位置 |
|------|-----|------|
| fractional_k | 0.5 | kelly_sizer.py:32 |
| floor | 0.5 | kelly_sizer.py:33 |
| cap | 1.5 | kelly_sizer.py:34 |

### 8.3 退出引擎

| 参数 | 值 | 位置 |
|------|-----|------|
| urgency_threshold | 0.65 | meta_exit_engine.py:97 |
| P(wPnL) | 0.30 | meta_exit_engine.py |
| P(wTime) | 0.20 | meta_exit_engine.py |
| P(wRegime) | 0.15 | meta_exit_engine.py |
| P(wConsensus) | 0.25 | meta_exit_engine.py |
| P(wVolatility) | 0.10 | meta_exit_engine.py |

### 8.4 治理阈值

| 参数 | 值 | 位置 |
|------|-----|------|
| 晋升 WR (candidate→probation) | ≥ 40% | brain_promotion.py:43 |
| 晋升 WR (probation→live) | ≥ 45% | brain_promotion.py:44 |
| 退役 WR | < 30% | brain_promotion.py:45 |
| 晋升 PF (probation) | ≥ 0.90 | brain_promotion.py:46 |
| 晋升 PF (live) | ≥ 1.10 | brain_promotion.py:47 |
| 退役 PF | < 0.60 | brain_promotion.py:48 |
| 连续亏损退役 | > 8 笔 | brain_promotion.py:49 |
| 影子晋升阈值 | ≥ 50 信号 | shadow_tracker.py:50 |

---

## 九、已知问题 & 技术债务

1. **DeepResMLP 模型损坏** — `deepresmlp_v2_new.onnx` 文件编码错误，加载即失败
2. **Meta Exit 模型数据不足** — 仅 7 胜, 胜率 0.12, 未达 `_MIN_WINS=15` 门槛
3. **5个旧 swing 模型全部 disabled** — 100% LONG-only bias, 负收益
4. **12个 frozen 大脑待清理** — governance_state.json 中有大量僵尸条目
5. **Online_MLP_V1 在线学习未启用** — 仅做推理, partial_fit 链路未接通
6. **Config hot reload 失效** — 启动时 JSON 解析失败, 静默降级
7. **Transformer 仅 H4 活跃** — M5/M15/H1 的 Transformer 变体都是 frozen
8. **MetaFilter 与 MetaFilterGate 阈值不一致** — 一个是 0.30(stage2) 另一个是 0.40(OU)
