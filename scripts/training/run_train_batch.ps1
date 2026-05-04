param(
  [string]$RepoRoot = "D:\cursor",
  [string]$Plan = "data\models\crt_batch_smoke\batch_plan.jsonl",
  [string]$CommandTemplate = "",
  [switch]$Execute,
  [switch]$StopOnError
)

$ErrorActionPreference = "Stop"
Set-Location $RepoRoot

$args = @(
  "scripts/training/run_train_batch.py",
  "--plan", $Plan
)
if (-not [string]::IsNullOrWhiteSpace($CommandTemplate)) {
  $args += @("--command-template", $CommandTemplate)
}
if ($Execute) {
  $args += "--execute"
}
if ($StopOnError) {
  $args += "--stop-on-error"
}

python @args
