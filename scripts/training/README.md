# CRT 批量训练骨架

与 **`docs/MODEL_LINEAGE_AND_NAMING.md`**、**`schemas/crt_model_manifest.v1.schema.json`** 对齐。

## 单条 manifest（stub）

```powershell
python scripts/training/write_manifest_stub.py `
  --dataset-slice-id 2025Q4_xau_train_v1 `
  --output data/models/example.manifest.json
```

## 批量占位（每 seed 一个目录 + manifest + batch_plan.jsonl）

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/training/batch_train_skeleton.ps1
```

或：

```powershell
python scripts/training/batch_train_skeleton.py `
  --output-dir data/models/crt_batch_smoke `
  --seeds 42,43,44 `
  --lane sur --role chlg --generation g2026.1 `
  --feature-contract-id feat-v9-institutional-1.0.0
```

将真实训练命令写入环境变量 **`CRT_TRAIN_CMD`**（可含 `{manifest}` 占位则由你们 trainer 解析）；当前骨架仅打印 `suggested_command` 占位。

## 自定义 job 列表

```json
[
  {"seed": 42, "dataset_slice_id": "2024-01_2024-06_xau_v3", "generation": "g2026.1"},
  {"seed": 43, "dataset_slice_id": "2024-07_2024-12_xau_v3", "generation": "g2026.2"}
]
```

每条 job 可选 **`generation`** 覆盖默认值，避免多条 challenger 共用同一 `model_id` 而无区分（亦可仅靠 manifest 中的 **`training_run_id`** 区分同一族下的多次训练）。

```powershell
python scripts/training/batch_train_skeleton.py `
  --output-dir data/models/my_batch `
  --jobs-file path/to/jobs.json
```

## 最小训练器（占位可运行）

仓库内已提供 `scripts/training/your_trainer.py`：读取 manifest，写 placeholder artifact，并回填 `metrics` / `artifact_primary`。

单次执行：

```powershell
python scripts/training/your_trainer.py `
  --manifest data/models/crt_batch_smoke/job_seed_42/*.manifest.json
```

### Lane 路由适配（接外部训练器）

- 可按 lane 传入命令模板：`--lane-command-template`（本次覆盖）
- 或用 JSON 文件映射 lane→命令：`--lane-command-file scripts/training/lane_trainers.sample.json`
- 模板占位符：`{manifest_path}` `{model_id}` `{training_run_id}` `{lane}` `{dataset_slice_id}` `{artifact_path}`

示例（shell 模式）：

```powershell
python scripts/training/your_trainer.py `
  --manifest data/models/crt_batch_smoke/job_seed_42/*.manifest.json `
  --lane-command-template "python your_real_trainer.py --manifest {manifest_path} --out {artifact_path}" `
  --shell
```

## 执行 batch_plan（dry-run / 真执行）

先生成 `batch_plan.jsonl`，再执行：

```powershell
python scripts/training/run_train_batch.py `
  --plan data/models/crt_batch_smoke/batch_plan.jsonl `
  --command-template "python scripts/training/your_trainer.py --manifest {manifest_path}"
```

默认是 **dry-run**（仅渲染并写 `*.run_report.jsonl`）。真实执行加：

```powershell
python scripts/training/run_train_batch.py `
  --plan data/models/crt_batch_smoke/batch_plan.jsonl `
  --command-template "python scripts/training/your_trainer.py --manifest {manifest_path}" `
  --execute
```

也可用 PowerShell 包装器：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/training/run_train_batch.ps1 `
  -Plan "data\models\crt_batch_smoke\batch_plan.jsonl" `
  -CommandTemplate "python scripts/training/your_trainer.py --manifest {manifest_path}"
```

---

## 正式 Lane 训练器 (Production Lane Trainers)

`scripts/training/trainers/` 提供 lane 专用的外部训练包装器，打通 `D:\ai` 与 CRT 管线。

### 架构：三段式协议

```
batch_plan.jsonl  →  your_trainer.py (lane-command 模式)
                         │
                         ├── 执行 lane 命令模板
                         │      └── sur_trainer.py / mtx_trainer.py
                         │             ├── 读取 manifest (输入约定)
                         │             ├── 调用 D:\ai 真实训练脚本
                         │             ├── 复制 artifact 到目标路径
                         │             └── 写出 result.json (回填协议)
                         │
                         └── 读取 result.json → 回填 manifest metrics/artifact
```

### 输入/输出约定

| 参数 | 来源 | 说明 |
|------|------|------|
| `--manifest-path` | your_trainer 传入 `{manifest_path}` | CRT manifest JSON (只读输入) |
| `--result-json-path` | your_trainer 传入 `{manifest_path}.result.json` | 训练结果 JSON (输出) |
| `--artifact-path` | your_trainer 传入 `{artifact_path}` | 目标 artifact 路径 (输出) |

### result.json 回填协议

外部训练器必须产出如下结构的 JSON（your_trainer 自动合并到 manifest）：

```json
{
  "trainer": "sur_trainer",
  "trainer_version": "sur-v9-institutional-1.0.0",
  "completed_at_utc": "2026-04-30T12:00:00Z",
  "model_id": "CRT.sur.chlg.g2026.1@feat-v9-institutional-1.0.0",
  "lane": "sur",
  "generation": "g2026.1",
  "exit_code": 0,
  "metrics": {
    "train_finished": true,
    "trainer_exit_code": 0,
    "epochs_requested": 200,
    "val_accuracy_pct": 87.3,
    "artifact_size_bytes": 123456
  },
  "risk_notes": [],
  "artifact_primary": "data/models/.../crt_sur_chlg_...onnx",
  "norm_artifact": "data/models/.../crt_sur_chlg_..._norm.mqh"
}
```

your_trainer 会自动将：
- `result.metrics` → 合并到 manifest `metrics`
- `result.artifact_primary` → 覆盖 manifest `artifact_primary`（若非空）
- `result.norm_artifact` → 覆盖 manifest `norm_artifact`（若非空）
- `result.risk_notes` → 追加到 manifest `risk_notes`

### 可用 Lane 训练器

#### 1. sur_trainer — Survival V9 Institutional

将 `Survival_V9\1_V9_Institutional_Forge.py` 桥接到 CRT。

```powershell
python scripts/training/trainers/sur_trainer.py `
  --manifest-path data/models/crt_batch_smoke/job_seed_42/CRT.sur.*.manifest.json `
  --result-json-path data/models/crt_batch_smoke/job_seed_42/result.json `
  --artifact-path data/models/crt_batch_smoke/job_seed_42/artifacts/model.onnx
```

参数：
| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--manifest-path` | (必填) | CRT manifest JSON 路径 |
| `--result-json-path` | (必填) | result.json 输出路径 |
| `--artifact-path` | (必填) | ONNX artifact 目标路径 |
| `--trainer-root` | `D:\ai\Survival_V9` | 训练脚本目录 |
| `--dataset-csv` | `<trainer-root>/V9_Symbiosis_Matrix.csv` | 训练数据 CSV |
| `--epochs` | `200` | 训练轮数 |

输出件：
- `model.onnx` — V9 机构级大脑 ONNX 模型
- `model_norm.mqh` — 标准化参数 (mean/std 数组)
- `result.json` — 训练指标与风险备注

#### 2. mtx_trainer — Meta PPO v4.5 Microstructure

将 `Meta_ppo_v4.5\03_Profit_Aware_Training.py` (Transformer) 或 `03_XGBoost_Training.py` (XGBoost) 桥接到 CRT。

```powershell
# Transformer 模式 (默认)
python scripts/training/trainers/mtx_trainer.py `
  --manifest-path data/models/crt_batch_smoke/job_seed_42/CRT.mtx.*.manifest.json `
  --result-json-path data/models/crt_batch_smoke/job_seed_42/result.json `
  --artifact-path data/models/crt_batch_smoke/job_seed_42/artifacts/model.onnx `
  --mode transformer

# XGBoost 模式
python scripts/training/trainers/mtx_trainer.py `
  --manifest-path data/models/crt_batch_smoke/job_seed_42/CRT.mtx.*.manifest.json `
  --result-json-path data/models/crt_batch_smoke/job_seed_42/result.json `
  --artifact-path data/models/crt_batch_smoke/job_seed_42/artifacts/model.json `
  --mode xgboost
```

参数：
| 参数 | 默认值 | 说明 |
|------|--------|------|
| `--manifest-path` | (必填) | CRT manifest JSON 路径 |
| `--result-json-path` | (必填) | result.json 输出路径 |
| `--artifact-path` | (必填) | 目标 artifact 路径 |
| `--mode` | `transformer` | `transformer` 或 `xgboost` |
| `--trainer-root` | `D:\ai\Meta_ppo_v4.5` | 训练脚本目录 |

输出件：
- Transformer 模式：`model.onnx` + `model.pth` (PyTorch 权重)
- XGBoost 模式：`model.json` (XGBoost 权重)
- `result.json` — 训练指标 (含准确率) 与风险备注

### 批量执行：从 batch_plan 一键跑全量

```powershell
# Step 1: 生成 batch_plan.jsonl
python scripts/training/batch_train_skeleton.py `
  --output-dir data/models/crt_batch_v1 `
  --seeds 42,43,44 `
  --lane sur --role chlg --generation g2026.1 `
  --feature-contract-id feat-v9-institutional-1.0.0

# Step 2: 用 lane_trainers.json 作为命令模板，--execute 真跑
python scripts/training/run_train_batch.py `
  --plan data/models/crt_batch_v1/batch_plan.jsonl `
  --command-template "python scripts/training/your_trainer.py --manifest {manifest_path} --lane-command-file scripts/training/lane_trainers.json --result-json-path {manifest_path}.result.json --shell" `
  --execute
```

### 自定义外部训练器接入

如需接入自己的训练脚本，只需实现：

1. **命令行参数**：必须接受 `--manifest-path`, `--result-json-path`, `--artifact-path`
2. **result.json**：按上述协议写入 JSON
3. **退出码**：成功返回 0，失败返回非 0

然后在 `lane_trainers.json` 中注册命令模板即可：

```json
{
  "my_lane": "python my_trainer.py --manifest-path {manifest_path} --result-json-path {manifest_path}.result.json --artifact-path {artifact_path}"
}
```

模板可用占位符：`{manifest_path}` `{model_id}` `{training_run_id}` `{lane}` `{dataset_slice_id}` `{artifact_path}`
