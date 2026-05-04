param(
  [string]$RepoRoot = "D:\cursor",
  [string]$OutputDir = "data\models\crt_batch_smoke",
  [string]$Seeds = "42,43,44",
  [string]$Lane = "sur",
  [string]$Role = "chlg",
  [string]$Generation = "g2026.1",
  [string]$FeatureContractId = "feat-v9-institutional-1.0.0"
)

$ErrorActionPreference = "Stop"
Set-Location $RepoRoot

python scripts/training/batch_train_skeleton.py `
  --output-dir $OutputDir `
  --seeds $Seeds `
  --lane $Lane `
  --role $Role `
  --generation $Generation `
  --feature-contract-id $FeatureContractId
