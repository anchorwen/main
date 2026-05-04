param(
  [string]$RepoRoot = "D:\cursor",
  [string]$BaseDir = "data",
  [double]$PollSeconds = 1.0,
  [double]$DefaultVolume = 0.01,
  [int]$Deviation = 20,
  [int]$Magic = 90001,
  [int]$RestartDelaySeconds = 3,
  [int]$MaxRestarts = 0,
  [switch]$DryRun
)

$ErrorActionPreference = "Stop"

function Step([string]$Message) {
  Write-Host ""
  Write-Host "==== $Message ====" -ForegroundColor Cyan
}

function Build-WorkerCommand() {
  return @(
    "python", "scripts/mt5_bridge_worker.py",
    "--outbox-dir", "$BaseDir/mt5_outbox",
    "--receipt-dir", "$BaseDir/receipts",
    "--archive-dir", "$BaseDir/mt5_outbox_processed",
    "--default-volume", "$DefaultVolume",
    "--deviation", "$Deviation",
    "--magic", "$Magic",
    "--poll-seconds", "$PollSeconds"
  )
}

Set-Location $RepoRoot

Step "Bridge Supervisor Parameters"
Write-Host "repo_root=$RepoRoot"
Write-Host "base_dir=$BaseDir"
Write-Host "poll_seconds=$PollSeconds"
Write-Host "default_volume=$DefaultVolume"
Write-Host "deviation=$Deviation"
Write-Host "magic=$Magic"
Write-Host "restart_delay_seconds=$RestartDelaySeconds"
Write-Host "max_restarts=$MaxRestarts (0 means unlimited)"
Write-Host "dry_run=$DryRun"

Step "Preflight bridge health"
$healthCmd = "python scripts/mt5_bridge_healthcheck.py --outbox-dir `"$BaseDir/mt5_outbox`" --receipt-dir `"$BaseDir/receipts`""
if ($DryRun) {
  Write-Host "[dry-run] $healthCmd" -ForegroundColor Yellow
} else {
  Invoke-Expression $healthCmd
}

if ($DryRun) {
  $workerPreview = (Build-WorkerCommand) -join " "
  Step "Worker command preview"
  Write-Host "[dry-run] $workerPreview" -ForegroundColor Yellow
  exit 0
}

$restartCount = 0
while ($true) {
  $cmd = Build-WorkerCommand
  Step "Starting mt5_bridge_worker (attempt $($restartCount + 1))"
  & $cmd[0] $cmd[1] $cmd[2] $cmd[3] $cmd[4] $cmd[5] $cmd[6] $cmd[7] $cmd[8] $cmd[9] $cmd[10] $cmd[11] $cmd[12] $cmd[13] $cmd[14] $cmd[15]
  $code = $LASTEXITCODE
  if ($code -eq 0) {
    Write-Host "mt5_bridge_worker exited normally (code=0). supervisor stopping." -ForegroundColor Green
    break
  }

  $restartCount += 1
  Write-Host "mt5_bridge_worker crashed (exit=$code), restart_count=$restartCount" -ForegroundColor Yellow
  if ($MaxRestarts -gt 0 -and $restartCount -ge $MaxRestarts) {
    throw "Reached max restarts ($MaxRestarts)."
  }
  Start-Sleep -Seconds $RestartDelaySeconds
}
