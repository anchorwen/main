# Cursor 系列模型：谱系、编号与命名约定

面向长期可持续与国际惯例对齐：**契约绑定（feature contract）→ 训练切片 → artifact → 注册入库**。标识符尽量 **ASCII**，便于路径、CI、容器与跨语言加载。

---

## 1. 顶层命名空间

| 符号 | 含义 |
|------|------|
| **`CRT`** | Cursor Release Train — 正式纳入本仓库谱系的模型族前缀（文件 / registry `model_id` 建议使用）。 |
| **可选厂商前缀** | 对外文档可写 `cursor-ai/crt-…`，与 HF/Garden 式 `org/name` 一致；仓库内以 `CRT` 短前缀为主。 |

历史 **`alpha_*` / `v9_*`** 名称保留作兼容别名；**新模型一律分配 CRT 谱系 ID**，并在 manifest 中写 `legacy_aliases`。

---

## 2. 家族（Lane）— 与方法论对齐，不靠「版本号拍脑袋」

Lane 表示 **能力谱系 / 训练本体**，与 `D:\ai` 四套映射如下（便于传承与分工）：

| Lane 代码 | 英文全称（文档用） | 典型职责 | 血缘参考 |
|-----------|-------------------|----------|----------|
| **`mtx`** | Microstructure Execution | Tick/微观 BAR、平稳微观特征、序列或树；偏执行短周期 | Meta_ppo_v4.5 |
| **`sur`** | Survival Institutional | 多周期 TA + OU/Hurst + 多头 dir/risk/vol；偏配置型 ONNX | Survival_V8 / V9 |
| **`mr`** | Mean-Reversion Scout | OU 状态机、均值回归可行性闸门；可与 mtx/sur 并联 | Meta_ppo_v6 |
| **`ens`** | Ensemble / Orchestrator | 多模型融合、meta-policy、权重与弃权（非单一估计器） | 新建 |
| **`rl`** | Reinforcement / Policy | PPO/SAC 等显式策略梯度（若后续引入） | 预留 |
| **`svc`** | Service / Calibration | 概率校准、排序、后处理、guardrail network（轻量） | 预留 |

新增 Lane 须在本文档追加一行表格项并 bump 文档尾部 **Lane registry 版本**。

---

## 3. 代际编号（Generation）— 机构常用的「时间 + 语义」混合

采用 **`gYYYY.N`**：

- **`YYYY`**：主代际锚点（首次立项或大规模契约切换的年份）。
- **`N`**：该年内 **契约不破兼容** 的迭代序号（新特征列 / 新输出头 → 升 `N`；破坏性契约变更可升年或走新 Lane）。

示例：`g2026.1`、`g2026.2`。

**语义版本（SemVer）仅用于**：

- **特征契约**：`feat-X.Y.Z`（见 `core/features` / manifest `feature_contract_id`）
- **推理接口**：`iface-X.Y.Z`（输入输出张量名与形状）

模型「好不好」不进代号；好坏由 **PromotionGate / shadow** 与 manifest 记录。

---

## 4. 角色（Role）— 同一 Lane 内模型干什么

| Role | 含义 |
|------|------|
| **`prd`** | Primary production candidate |
| **`chlg`** | Challenger（shadow） |
| **`cabl`** | Calibration |
| **`stub`** | Stub / fallback（如无 ONNX 时的占位逻辑） |

---

## 5. 规范模型 ID（单一真源）

**格式（不含空格）：**

```text
CRT.<lane>.<role>.gYYYY.N@feat-<name>-<semver>
```

示例：

- `CRT.sur.prd.g2026.1@feat-v9-institutional-1.0.0`
- `CRT.mtx.chlg.g2026.1@feat-tick9-seq-0.2.0`

说明：

- **`@` 右侧** 必须与仓库内 **feature manifest / schema** 的 ID 完全一致。
- 同一 `(lane, role, generation, feature_contract)` 再训练只换 **数据切片与哈希**，不改变 ID；新 candidate 用 **新 generation** 或 **新 role（chlg→prd）** 晋升流程描述。

---

## 6. Artifact 文件名（磁盘 / 对象存储）

**格式：**

```text
crt_<lane>_<role>_gYYYY_N_<featSlug>_r<gitShort>_b<build>.<ext>
```

- **`featSlug`**：`feat-v9-institutional-1-0-0`（点改为 `-`）
- **`gitShort`**：7 hex；未知则用 `nogit`
- **`build`**：可选 CI build id 或 `local`

示例：`crt_sur_prd_g2026_1_feat-v9-institutional-1-0-0_r1a2b3c4.onnx`

**伴随文件（强制）：**

- `crt_….manifest.json` — Model Card + BoM（见下）
- 若有规范化参数：`crt_….norm.json`（mean/std 或与 MQ5 对齐的导出）

---

## 7. Manifest（Model Card + Bill of Materials）最小字段

与业界 Model Card / supply-chain 实践对齐，训练结束必须生成：

| 字段 | 说明 |
|------|------|
| `model_id` | 第 5 节规范字符串 |
| `lane`, `role`, `generation` | 拆分字段便于检索 |
| `feature_contract_id` | 与 `@feat-…` 一致 |
| `iface_semver` | ONNX / TorchScript IO 契约版本 |
| `dataset_slice_id` | 训练数据时间区间 + 筛选版本 |
| `git_commit`, `train_started_at_utc`, `trainer_version` | 可复现 |
| `metrics` | 离线指标（可分 regime） |
| `risk_notes` | 已知失效模式 |
| `training_run_id` | 单次训练唯一 id（批量 job / 多种子）；与 `model_id` 族并存 |

**Schema 与工具**：[`schemas/crt_model_manifest.v1.schema.json`](../schemas/crt_model_manifest.v1.schema.json)，[`scripts/training/README.md`](../scripts/training/README.md)。

---

## 8. 晋升与废弃（可持续性）

1. **Shadow**：仅 `role=chlg`，journal / compare 报告绑定 `model_id`。
2. **Promotion**：`chlg → prd` 须更新 registry，旧 `prd` 降为 `archived` 或保留回滚指针。
3. **废弃**：ID **永不复用**；新能力起新 `generation` 或新 `lane`。

---

## 9. 与仓库现有入口对齐

| 现有概念 | CRT 映射 |
|----------|-----------|
| `configs/features/v9_institutional_40.json` | `feat-v9-institutional-*` 契约 |
| `configs/brains/v9_institutional_01.json` | brain 配置引用 `model_id` + artifact 路径 |
| `alpha_registry.json` | 逐步迁移为 **CRT ID**；`alpha_id` 可作 legacy |

---

## 10. Lane registry 文档版本

| 版本 | 日期 | 变更摘要 |
|------|------|----------|
| 1.0.0 | 2026-04-30 | 初版：CRT 命名空间、Lane、代际、artifact、manifest |
