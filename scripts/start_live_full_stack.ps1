param(
  [string]$RepoRoot = "D:\cursor",
  [string]$BaseDir = "data",
  [string]$Mt5TerminalPath = "D:\MetaTrader 5\terminal64.exe",
  [string]$Symbol = "XAUUSDc",
  [double]$DefaultVolume = 0.01,
  [double]$IntentIntervalSeconds = 30,
  [double]$ThresholdPriceDelta = 10.0,
  [double]$SlDistance = 15.0,
  [double]$TpDistance = 25.0,
  [double]$CooldownSeconds = 300.0,
  [int]$MaxPositions = 1,
  [switch]$SkipPolicy,
  [switch]$DryRun
)

$ErrorActionPreference = "Stop"

Set-Location $RepoRoot

Write-Host "=== Live full stack ===" -ForegroundColor Cyan
Write-Host "Read docs/LIVE_OPS.md — anchor loop is NOT the ONNX V9 brain; it only feeds mt5_outbox."
Write-Host "1) Background: start_live_ops (bridge + policy + P1)"
Write-Host "2) Foreground: live_intent_loop (anchor-delta intents -> mt5_outbox)"
Write-Host "MT5 terminal must already be logged in. HIGH FINANCIAL RISK." -ForegroundColor Yellow
Write-Host ""

$opsArgs = @(
  "-NoProfile", "-ExecutionPolicy", "Bypass",
  "-File", (Join-Path $RepoRoot "scripts/start_live_ops.ps1"),
  "-RepoRoot", $RepoRoot,
  "-BaseDir", $BaseDir,
  "-Mt5TerminalPath", $Mt5TerminalPath,
  "-Symbol", $Symbol,
  "-DefaultVolume", "$DefaultVolume"
)
if ($SkipPolicy) { $opsArgs += "-SkipPolicy" }
if ($DryRun) { $opsArgs += "-DryRun" }

if ($DryRun) {
  Write-Host "[dry-run] Start-Process powershell $($opsArgs -join ' ')" -ForegroundColor Yellow
  Write-Host "[dry-run] python scripts/live_intent_loop.py ..." -ForegroundColor Yellow
  exit 0
}

Start-Process -FilePath "powershell" -ArgumentList $opsArgs -WindowStyle Normal

Start-Sleep -Seconds 8

Write-Host "Post-wait bridge healthcheck (non-strict; failures are warnings only)..." -ForegroundColor DarkGray
$healthOut = Join-Path $BaseDir "reports/ops_logs/mt5_bridge_health_preflight.json"
$HealthErr = 0
try {
  & python scripts/mt5_bridge_healthcheck.py --outbox-dir "$BaseDir/mt5_outbox" --receipt-dir "$BaseDir/receipts" --max-rejected 999 --output $healthOut
  $HealthErr = $LASTEXITCODE
} catch {
  $HealthErr = 1
}
if ($HealthErr -ne 0) {
  Write-Host "mt5_bridge_healthcheck exit_code=$HealthErr (see $healthOut or run manually). Continuing." -ForegroundColor Yellow
} else {
  Write-Host "mt5_bridge_healthcheck ok -> $healthOut" -ForegroundColor DarkGreen
}

python scripts/live_intent_loop.py `
  --base-dir $BaseDir `
  --mt5-terminal-path $Mt5TerminalPath `
  --symbol $Symbol `
  --interval-seconds $IntentIntervalSeconds `
  --threshold-price-delta $ThresholdPriceDelta `
  --sl-distance $SlDistance `
  --tp-distance $TpDistance `
  --cooldown-seconds $CooldownSeconds `
  --max-positions $MaxPositions
