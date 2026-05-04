# 方案A：中枢加冕期（0 - 1.5 年）

> **状态**: 进行中 (70%)
> **目标**: 完成基础设施重构，摆脱客户端束缚，实现策略的模块化。
> **核心动作**: 不是新建中枢——是将已有的 `core/` 层正式加冕为生产中枢。

---

## 为什么叫"中枢加冕"

经过架构审查发现，`core/` 层已包含完整的自动驾驶引擎：

```
已有（未加冕）                    将被替换的/缺失的
─────────────────                ─────────────────
ServiceContainer (30+服务)       → 目前手动启动各脚本
RuntimeLoop (推理→决策→派发)     → live_intent_loop.py (硬编码价格差)
BrainFactory (ONNX v9)          → 训练产出从未接入
BrainRegistryService            → 无模型注册清单
DecisionCycleOrchestrator       → 无统一编排
ParliamentService               → 无多信号融合
ConfigHotReload                 → 改代码才能改参数
SystemModeState                 → 无模式切换
```

**加冕 = 让 `main.py` 组装这一切，替代零散的 shell 脚本。**

---

## 五阶段实施计划

### 阶段 A-1：中枢入口 `main.py`

**预期耗时**: 2-3周
**依赖**: 无（纯装配工作）
**里程碑**: [A1] 中枢加冕 — main.py 上线

**详细步骤**:

1. 创建 `configs/live.yaml` — 统一实盘配置入口
   ```yaml
   environment:
     base_dir: "data/live"
     producer_name: "quant_os_live"
     target_name: "mt5_gateway"
     system_mode: "NORMAL"
   
   adapter:
     type: "file_queue"          # stub | file_queue | mt5
     outbox_dir: "data/live/outbox"
   
   risk:
     max_open_positions: 3
     max_drawdown_pct: 0.15
     max_notional_exposure: 500000
     max_per_symbol: 200000
   
   brains:
     registry_path: "configs/brain_entries.json"
   ```

2. 创建 `D:\future\main.py`
   ```python
   def main():
       # 1. 加载配置
       config = EnvironmentConfig.from_yaml("configs/live.yaml")
       
       # 2. 构建服务容器（30+服务自动装配）
       container = ServiceContainer(config).build()
       
       # 3. 构建运行时循环
       runtime = container.build_runtime_loop()
       
       # 4. 构建编排器
       orchestrator = container.build_orchestrator(runtime)
       
       # 5. 启动健康检查
       container.health_check.start()
       
       # 6. 启动主循环
       orchestrator.run_forever()
   ```

3. 验证：`python main.py` 能在本地通过 StubAdapter 跑完整循环

**实现约束**:
- `main.py` 不得超过150行
- 不得包含任何信号生成或风控逻辑（这些在 `core/` 中）
- 必须支持 `SIGTERM` 优雅退出

---

### 阶段 A-2：模型管线打通

**预期耗时**: 4-6周
**依赖**: A-1
**里程碑**: [A2] 模型管线打通

**当前断链状态**:
```
训练侧 (lane_trainers.json + trainers/)              实盘侧 (scripts/)
├── Transformer → ONNX (sur, mtx)                    ├── live_intent_loop.py
├── XGBoost → JSON   (mtx_xgb)                       │   └── decide_side_from_anchor()
├── OU参数优化 → JSON (arb)                          │       ↑ 从未加载任何模型
│   └── models/mtx_v2.onnx                           │       ↑ 仅用价格差判断
│   └── models/mtx_v2.xgb.json                       │
│   └── models/sur_v3.onnx                           └── 无模型加载代码
│   └── models/arb_v1.params.json
└── 注册表已存在 (lane_trainers.json)
    但 core/ 层无对应模型类型抽象
```

**打通步骤**:

1. 在 `core/brains/adapters/` 下创建**模型蓝本抽象层 `ModelArtifactAdapter`**：
   
   ```
   core/brains/adapters/
   ├── base_adapter.py              # ← NEW: 抽象基类 ModelArtifactAdapter
   │   └── 定义接口: load(), infer(features) → DecisionCandidate
   ├── onnx_brain_adapter.py         # 已有: V9OnnxBrainAdapter
   ├── xgboost_brain_adapter.py     # ← NEW: XGBoost JSON 推理
   ├── params_brain_adapter.py      # ← NEW: OU参数/Z-Score 信号生成
   └── future_adapter.py            # ← 预留: PyTorch JIT, LightGBM, TensorFlow...
   ```
   
   **设计原则**: 每种模型产出物对应一个 Adapter 实现。`BrainFactory.build()` 根据 `brain_type` 字段路由到正确的 Adapter。

2. 扩展 `configs/brain_entries.json` — 支持多种模型类型
   ```json
   {
     "brains": [
       {
         "brain_id": "mtx_v2_transformer",
         "brain_type": "onnx_v9",
         "model_path": "models/mtx_v2.onnx",
         "entry_threshold": 0.70,
         "enabled": true,
         "weight": 1.0
       },
       {
         "brain_id": "mtx_v2_xgboost",
         "brain_type": "xgboost_json",
         "model_path": "models/mtx_v2.xgb.json",
         "entry_threshold": 0.65,
         "enabled": true,
         "weight": 0.8
       },
       {
         "brain_id": "sur_v3_institutional",
         "brain_type": "onnx_v9",
         "model_path": "models/sur_v3.onnx",
         "entry_threshold": 0.60,
         "enabled": true,
         "weight": 1.0,
         "normalization_config_path": "configs/normalization_sur_v3.json"
       },
       {
         "brain_id": "arb_ou_v1",
         "brain_type": "ou_params_json",
         "model_path": "models/arb_v1.params.json",
         "entry_threshold": 0,
         "enabled": true,
         "weight": 0.7
       }
     ]
   }
   ```

3. 改造 `BrainFactory.build()` 使其能根据 `brain_type` 路由到对应的 Adapter
4. 将各 Adapter 与真实的行情数据源对接
5. 端到端测试：行情 → Feature提取 → 各类型模型推理 → 统一 DecisionCandidate → 信号产生


**信号链路验证（多模型蓝本）**:
```
行情Tick 
  → FeatureService.extract()
  → FeatureAdapter.transform()
  → BrainFactory.build(brain_type).infer(features)
      ├── brain_type=onnx_v9      → V9OnnxBrainAdapter   → score
      ├── brain_type=xgboost_json → XGBoostBrainAdapter  → score
      └── brain_type=ou_params_json → OUParamsBrainAdapter → z_score → score
  → DecisionCandidate{model_id, signal, confidence, signal_type}
  → DecisionCompiler.compile()
  → RiskEvaluationService.evaluate()
  → CommunicationDispatcher.dispatch()
  → 执行网关
```

---

### 阶段 A-3：真实Feature流

**预期耗时**: 6-8周
**依赖**: A-2
**里程碑**: [A3] 真实Feature流

**当前状态**: `live_intent_loop.py` 中策略判断仅基于：
- 当前价格 vs 均线价格（硬编码）
- 无EMA偏差率、ADX动能、波动率曲面等特征

**实施步骤**:

1. 设计 Feature Schema（基于已有 `V9InstitutionalSchema`）
   ```
   一级特征：
     - price_action: (close, high, low, volume)
     - moving_averages: (SMA_20, EMA_50, EMA_200)
     - oscillators: (RSI_14, MACD_12_26_9, ADX_14)
     - volatility: (ATR_14, BB_width_20_2, HV_20)
     - volume_profile: (VWAP, OBV, volume_ratio)
   
   二级特征（由一级衍生）：
     - ema_deviation_rate: (close - EMA_50) / EMA_50
     - adx_slope: ADX_14 - ADX_14_lag1
     - bb_position: (close - BB_lower) / (BB_upper - BB_lower)
     - volume_surge: volume / SMA(volume, 20)
   ```

2. 实现 `FeatureUpdateJob` — 每日定时增量更新
3. 将特征写入 `LocalFeatureStore`（Parquet 格式）
4. 替换 `live_intent_loop.py` 中的硬编码策略为 `FeatureService`

**数据源集成**:
- 方案1: MT5 直接导出OHLC数据 → Python消费
- 方案2: 第三方数据API (如 Polygon, Alpha Vantage)
- 方案3: 本地 ClickHouse/Parquet 自建

---

### 阶段 A-4：FIX/MT5 实盘网关

**预期耗时**: 8-12周
**依赖**: A-1
**里程碑**: [A1] 的一部分

**当前状态**: `core/` 中已有 `MT5CommunicationAdapter` 和 `FIXGatewayAdapter`，但：
- MT5Adapter 需终端路径，可能未在生产环境测试
- FIXGatewayAdapter 为框架代码，需对接真实 FIX endpoint

**实施步骤**:

1. **优先 MT5 通道**（已有代码最完整）
   - 配置 `MT5CommunicationAdapter` 指向真实终端
   - 测试下单/撤单/成交回报全链路
   - 实现断线重连和心跳维持

2. **FIX 通道备选**（用于更机构化的场景）
   - 对接真实 FIX 4.4 endpoint
   - 实现登录/心跳/订单提交/执行报告
   - 处理序列号重置和重连

3. **纸质交易模式**（开发阶段默认）
   - `StubCommunicationAdapter` 记录所有信号但不实际发单
   - 用于测试和验证

**订单类型支持**:
- 市价单 (Market Order)
- 限价单 (Limit Order)
- 止损单 (Stop Order)
- TWAP/VWAP 拆单（后续阶段）

---

### 阶段 A-5：云端24/5部署

**预期耗时**: 4-6周
**依赖**: A-1 ~ A-4
**里程碑**: [A4] 云端24/5运行

**实施步骤**:

1. **Docker 化**
   ```dockerfile
   FROM python:3.11-slim
   WORKDIR /app
   COPY requirements.txt .
   RUN pip install -r requirements.txt
   COPY core/ ./core/
   COPY configs/ ./configs/
   COPY main.py .
   CMD ["python", "main.py"]
   ```

2. **systemd 服务化**（单机部署）
   ```
   [Unit]
   Description=Quant OS Live Engine
   After=network.target
   
   [Service]
   Type=simple
   User=quant
   WorkingDirectory=/opt/quant_os
   ExecStart=/usr/bin/python main.py
   Restart=always
   RestartSec=10
   
   [Install]
   WantedBy=multi-user.target
   ```

3. **健康检查脚本**（已有）
   - `live_auto_healthcheck.py` — 检查所有服务状态
   - `live_stack_diagnostic.py` — 进程堆栈诊断
   - `live_daily_recap.py` — 每日运行摘要

4. **监控告警**
   - 监控 `live_dispatch_block.flag` 状态
   - 监控 MT5 终端连接状态
   - 监控账户余额/保证金/回撤
   - 异常时发送通知（已有 `alert_service.py`）

---

## 方案A完成标准

- [ ] `python main.py` 一键启动整个实盘系统
- [ ] **至少3种模型蓝本**（ONNX / XGBoost / OU参数）产生的信号经过完整的风控门禁到达执行网关
- [ ] `ModelArtifactAdapter` 抽象层完成，新增模型类型只需实现一个新 Adapter
- [ ] Feature Store 每日自动更新
- [ ] 系统在云端连续运行 7 天无人工干预
- [ ] 断网/断电后自动恢复
- [ ] `start_live_ops.ps1` 保留作为降级备份但不再是主要入口

---

## 关键风险

| 风险 | 概率 | 影响 | 缓解措施 |
|------|------|------|---------|
| ONNX模型在线推理延迟过高 | 中 | 高 | 预留降级到纯Python推理的路径；XGBoost JSON 天然低延迟 |
| 多模型蓝本接口不统一 | 中 | 高 | `ModelArtifactAdapter` 抽象基类强制统一接口 |
| MT5终端不稳定 | 高 | 中 | 保留 FileQueue 模式作为缓冲 |
| 特征计算与训练时不一致 | 中 | 高 | 严格的特征版本控制 (`schema_versions.py`)，每个 brain_type 声明所需特征集 |
| 云端网络不稳定 | 中 | 中 | Docker auto-restart + 健康检查 |

---

> **最后更新**: 2026-05-01
> **关联 ADR**: 待记录