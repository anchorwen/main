param(
  [string]$RepoRoot = "D:\cursor",
  [string]$BaseDir = "data",
  [string]$Symbol = "XAUUSDc",
  [string]$Mt5TerminalPath = ""
)

$ErrorActionPreference = "Stop"

Set-Location $RepoRoot

Write-Host "=== Ops acceptance (read docs/LIVE_OPS.md) ===" -ForegroundColor Cyan
Write-Host ""

Write-Host "[1/3] live_stack_diagnostic -> data/reports/live_stack_diagnostic.json"
$diagArgs = @(
  "scripts/live_stack_diagnostic.py",
  "--base-dir", $BaseDir,
  "--symbol", $Symbol,
  "--output", (Join-Path $BaseDir "reports/live_stack_diagnostic.json")
)
if (-not [string]::IsNullOrWhiteSpace($Mt5TerminalPath)) {
  $diagArgs += @("--mt5-terminal-path", $Mt5TerminalPath)
}
python @diagArgs
if ($LASTEXITCODE -ne 0) {
  Write-Host "live_stack_diagnostic exit=$LASTEXITCODE" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "[2/3] mt5_bridge_healthcheck -> data/reports/ops_acceptance_bridge_health.json"
$healthOut = Join-Path $BaseDir "reports/ops_acceptance_bridge_health.json"
python scripts/mt5_bridge_healthcheck.py `
  --outbox-dir "$BaseDir/mt5_outbox" `
  --receipt-dir "$BaseDir/receipts" `
  --max-rejected 999 `
  --output $healthOut
Write-Host "healthcheck exit=$LASTEXITCODE (non-zero often OK if no receipts today)" -ForegroundColor DarkGray

Write-Host ""
Write-Host "[3/3] Manual: send one intent then confirm journal/receipt (see docs/LIVE_OPS.md)"
Write-Host "  python scripts/send_live_order.py --help"
Write-Host "  OR dry-check outbox: Get-ChildItem -Recurse $BaseDir/mt5_outbox -Filter *.mt5.json"
Write-Host ""
Write-Host "Done."
