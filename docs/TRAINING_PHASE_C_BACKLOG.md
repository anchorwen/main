# 阶段 C：批量重训与模型接入（独立里程碑）

本文件仅列 **实盘收敛（阶段 A/B）之后** 的建议 backlog，与「修 bridge / 闸口 / 运维脚本」分开排期。

**阶段 B（执行契约）已完成**：统一 envelope.payload（volume、close、modify_sltp、schema 标记）— 见 [**LIVE_EXECUTION_CONTRACT.md**](LIVE_EXECUTION_CONTRACT.md) 与 `dispatch_live_mt5_execution`。训练产物只需写入同一契约即可对接 bridge。

## 目标

- 借鉴 `D:\ai` 下四套管线（Survival V8/V9、Meta_ppo_v4.5、Meta_ppo_v6）中的 **特征工程、多头输出、风控叙事**，产出 **版本化** 数据集与模型 artifact。
- 与仓库内 **`V9_INSTITUTIONAL_40_FEATURES`**（`core/features/schemas/v9_institutional_schema.py`）及 **`PromotionGate` / shadow baseline** 对齐，而不是直接覆盖现有运维脚本。
- **Cursor 系列模型谱系与编号**：见 [**MODEL_LINEAGE_AND_NAMING.md**](MODEL_LINEAGE_AND_NAMING.md)（`CRT.<lane>.<role>.gYYYY.N@feat-…` + manifest / artifact 约定）。

## 建议交付形态

1. **数据湖 / 特征**：Harvester → 版本化特征表（列名与 schema 版本绑定）。
2. **训练**：Survival 式 PyTorch 多头（dir / risk / vol）或 v4.5 式序列模型；导出 **ONNX / torchscript / joblib**。
3. **清单**：`manifest.json`（CRT 字段见 **`schemas/crt_model_manifest.v1.schema.json`**；骨架脚本 **`scripts/training/`**）。
4. **接入**：推理服务或进程 → **替换或并行** `live_intent_loop`，仍只写 **`mt5_outbox`** + 既有 bridge（契约不变）。

## 安全

- **禁止**在仓库中提交 MT5 账号密码；训练与推理环境使用 **环境变量或密钥管理**。
- `D:\ai` 部分历史脚本含明文凭证，迁移时需清理。

## 非目标（本阶段不做）

- 在 bridge 内嵌复杂模型推理（应保持「意图 → outbox → worker」边界）。
- 与实盘救火同一迭代混做（避免同时改运维与大数据管线）。

---

## 源自 `D:\ai` 四套的可迁移实践（摘录）

对应目录：`Meta_ppo_v4.5`、`Meta_ppo_v6`、`Survival_V8`、`Survival_V9`。下列条目可直接指导 Cursor 侧 Phase C 数据集与模型契约设计（与上文「建议交付形态」一致）。

### Meta_ppo_v4.5 — 微观 Tick → 平稳特征 → 序列 + 树双轨

- **数据粒度**：MT5 Tick 聚合为小 BAR，刻画微观结构（点差、买卖 Tick、订单不平衡 OIM、Tick 流速等）。
- **平稳化**：优先收益率与比例特征，弱化绝对价格；跨品种用 **收益率** 对齐宏观上下文，避免绝对价差作主特征。
- **标签**：「动态波动阈值 × 未来若干 BAR」思想（脚本中的波动乘子 + 前瞻窗口）；标签定义须版本化写入 manifest。
- **序列管线**：固定 `SEQ_LEN`，训练集 **StandardScaler**，**joblib 持久化**，推理必须与训练同一 scaler artifact。
- **模型分工**：Transformer（含 **Focal Loss**、时序池化）与 **XGBoost**（对序列维做 mean/std/max/min/last/momentum 坍缩）可并行作为 challenger，便于校准类别不均衡（如 `scale_pos_weight`）。

### Meta_ppo_v6 — OU 均值回归侦察 + 纵深矩阵（Gamma）

- **信号侧**：用回归估计 OU 相关量时，对 \(\theta\le 0\)、均值离谱等情况做 **熔断**，只在「物理上可解释的均值回归窗口」参与决策或训练加权。
- **风控侧**：Gamma（风险厌恶）与「纵深 / 折合 ATR」类沙盘，适合映射为 **仓位层或闸口参数搜索**，而非塞进单一方向 logits。
- **凭证**：迁移脚本时 **严禁**把 MT5 明文密码写入本仓库；一律环境变量或密钥管理。

### Survival_V8.2 — 多周期技术指标 + OU/Hurst 高阶特征 + 多任务头

- **特征**：以多周期（如 M5/M15/M30/H1）技术指标族为基底，再叠加 **OU Theta**、**Hurst（近似）** 等多窗口滚动特征；注意大窗口导致的 **NaN 裁剪** 与样本起始对齐。
- **输出**：**方向（多分类）+ risk（0–1）+ vol（回归）** 的多任务头与实盘「方向 / 风险预算 / 波动档位」叙事一致；损失权重需在 manifest 中记录。
- **产物**：与 ONNX 导出、MQ5/推理侧常量对齐的思路，与本仓库 `configs/features/v9_institutional_40.json`、`configs/brains/*.json` 的契约化路线同源。

### Survival_V9 — 机构锻造：归一化落盘 + 加权多损失 + ONNX 三输出

- **归一化 artifact**：训练集 mean/std **导出为独立文件**（那边示例为 `*.mqh`；Cursor 侧可用 JSON + 推理加载），保证训练与推理同一套规范化。
- **损失组合**：方向 CE + risk BCE + vol MSE（或同类）按权重相加；权重属于 **训练配置**，需版本化。
- **导出契约**：ONNX `input` → `out_dir` / `out_risk` / `out_vol`（命名固定），与 `core/brains/adapters/v9_onnx_brain_adapter.py` 及引擎 bootstrap 路径对齐。

### 汇入 Cursor 的优先级（执行顺序）

1. **契约冻结**：特征列集合 + schema 版本 + normalization artifact（先有清单再训练）。
2. **标签分层**：微观模型（波动缩放类标签）与宏观多头模型（dir/risk/vol）**分开数据集**，禁止混用同一标签定义冒充「四模型融合」。
3. **接入纪律**：新模型先 **shadow / promotion gate**，再替换或并行 `live_intent_loop`；执行仍只走 **`CommunicationEnvelope` → outbox → bridge**。

### 交叉引用

- 实盘运维边界与 ONNX 未默认挂载：`docs/LIVE_OPS.md`
- 执行载荷契约：`docs/LIVE_EXECUTION_CONTRACT.md`
- 路线图总览：`EVOLUTION_PLAN.md`
