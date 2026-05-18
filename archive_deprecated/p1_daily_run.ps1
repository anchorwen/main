# DEPRECATED — replaced by python main.py daily-ops.
param(
  [string]$RepoRoot = "D:\cursor",
  [string]$BaseDir = "data",
  [string]$Date = "",
  [string]$Symbol = "XAUUSDc",
  [string]$AlphaId = "alpha_xau_live",
  [string]$JournalPath = "",
  [string]$ShadowBaselineJson = "data/replays/v9_shadow_baselines/neutral_stability/neutral_case.baseline.json",
  [string]$AlphaName = "",
  [string]$Mt5TerminalPath = "",
  [switch]$ApplyEvaluate,
  [switch]$SkipEnsureAlphaRegistered,
  [switch]$UseCliIngestOnly,
  [switch]$SkipBridgeHealthcheck,
  [switch]$BridgeHealthStrict,
  [switch]$SkipCompare,
  [switch]$DryRun
)

$ErrorActionPreference = "Stop"

function Step([string]$Message) {
  Write-Host ""
  Write-Host "==== $Message ====" -ForegroundColor Cyan
}

function Run-Step([string]$Command) {
  if ($DryRun) {
    Write-Host "[dry-run] $Command" -ForegroundColor Yellow
    return
  }
  Invoke-Expression $Command
  if ($LASTEXITCODE -ne 0) {
    throw "Command failed with exit code ${LASTEXITCODE}: $Command"
  }
}

function Ensure-AlphaRegistered() {
  if ($SkipEnsureAlphaRegistered) {
    Write-Host "skip_ensure_alpha_registered=True"
    return
  }
  if ([string]::IsNullOrWhiteSpace($AlphaName)) {
    $script:AlphaName = $AlphaId
  }
  if ($DryRun) {
    Write-Host "[dry-run] ensure alpha exists: $AlphaId (name=$AlphaName)" -ForegroundColor Yellow
    return
  }
  $listOutput = & python -m apps.engine.cli --base-dir $BaseDir alpha list
  if ($LASTEXITCODE -ne 0) {
    throw "Failed to query alpha list with exit code ${LASTEXITCODE}"
  }
  $listPayload = $listOutput | ConvertFrom-Json
  $exists = @($listPayload.records | Where-Object { $_.alpha_id -eq $AlphaId }).Count -gt 0
  if (-not $exists) {
    Step "4.5) Auto-register missing alpha_id for evaluate"
    Run-Step "python -m apps.engine.cli --base-dir $BaseDir alpha register --alpha-id $AlphaId --name `"$AlphaName`" --strategy-id $AlphaId"
  }
}

if ([string]::IsNullOrWhiteSpace($Date)) {
  $Date = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd")
}

if ([string]::IsNullOrWhiteSpace($JournalPath)) {
  $JournalPath = "$BaseDir/live_trade_journal.jsonl"
}

$reportsDir = "$BaseDir/reports"
$qualityOut = "$reportsDir/trade_quality_${Date}_${Symbol}.json"
$ingestOut = "$reportsDir/alpha_live_ingest_${Date}_${Symbol}.json"
$compareOut = "$reportsDir/shadow_live_compare_${Date}_${Symbol}.json"
$bridgeHealthOut = "$reportsDir/mt5_bridge_health_${Date}.json"
$positionsOut = "$reportsDir/mt5_positions_${Date}_${Symbol}.json"

Set-Location $RepoRoot

Step "P1 Daily Run Parameters"
Write-Host "repo_root=$RepoRoot"
Write-Host "base_dir=$BaseDir"
Write-Host "date=$Date"
Write-Host "symbol=$Symbol"
Write-Host "alpha_id=$AlphaId"
Write-Host "alpha_name=$AlphaName"
Write-Host "journal_path=$JournalPath"
Write-Host "shadow_baseline_json=$ShadowBaselineJson"
Write-Host "apply_evaluate=$ApplyEvaluate"
Write-Host "mt5_terminal_path=$Mt5TerminalPath"
Write-Host "skip_ensure_alpha_registered=$SkipEnsureAlphaRegistered"
Write-Host "use_cli_ingest_only=$UseCliIngestOnly"
Write-Host "skip_bridge_healthcheck=$SkipBridgeHealthcheck"
Write-Host "bridge_health_strict=$BridgeHealthStrict"
Write-Host "skip_compare=$SkipCompare"
Write-Host "dry_run=$DryRun"

if (-not $SkipBridgeHealthcheck) {
  Step "0) Bridge health pre-check"
  $healthCmd = "python scripts/mt5_bridge_healthcheck.py --outbox-dir `"$BaseDir/mt5_outbox`" --receipt-dir `"$BaseDir/receipts`" --output `"$bridgeHealthOut`""
  if ($BridgeHealthStrict) {
    Run-Step $healthCmd
  } else {
    try {
      Run-Step $healthCmd
    } catch {
      Write-Host "WARN: bridge health pre-check failed (non-strict mode), continue daily pipeline." -ForegroundColor Yellow
      Write-Host $_.Exception.Message -ForegroundColor Yellow
    }
  }
}

Step "0.5) MT5 positions snapshot (best effort)"
if ([string]::IsNullOrWhiteSpace($Mt5TerminalPath)) {
  if (-not $DryRun) {
    Write-Host "WARN: mt5_terminal_path not provided, skip positions snapshot." -ForegroundColor Yellow
  } else {
    Write-Host "[dry-run] skip mt5 positions snapshot (no mt5_terminal_path)." -ForegroundColor Yellow
  }
} else {
  Run-Step "python scripts/mt5_positions_snapshot.py --mt5-terminal-path `"$Mt5TerminalPath`" --symbol $Symbol --output `"$positionsOut`""
}

Step "1) Build trade quality report"
Run-Step "python scripts/trade_quality_report.py --journal-path `"$JournalPath`" --date $Date --symbol $Symbol --output `"$qualityOut`""

if (-not $DryRun) {
  $qualityPayload = Get-Content -Path $qualityOut -Raw | ConvertFrom-Json
  if ([int]$qualityPayload.total -eq 0) {
    Write-Host "WARN: live journal has zero rows for date=$Date symbol=$Symbol (check bridge worker/runtime filters)." -ForegroundColor Yellow
  }
}

if (-not $UseCliIngestOnly) {
  Step "2) Ingest live journal into alpha performance store (script)"
  Run-Step "python scripts/ingest_live_journal_to_alpha.py --base-dir $BaseDir --journal-path `"$JournalPath`" --date $Date --alpha-id $AlphaId --symbol $Symbol --output `"$ingestOut`""
}

Step "3) CLI ingest-live-bridge for alpha workflow parity"
Run-Step "python -m apps.engine.cli --base-dir $BaseDir alpha ingest-live-bridge --alpha-id $AlphaId --journal-path `"$JournalPath`" --date $Date --symbol $Symbol"

Step "4) Show latest alpha performance snapshot"
Run-Step "python -m apps.engine.cli --base-dir $BaseDir alpha performance --alpha-id $AlphaId"

Ensure-AlphaRegistered

Step "5) Evaluate alpha gate"
if ($ApplyEvaluate) {
  Run-Step "python -m apps.engine.cli --base-dir $BaseDir alpha evaluate --alpha-id $AlphaId --apply"
} else {
  Run-Step "python -m apps.engine.cli --base-dir $BaseDir alpha evaluate --alpha-id $AlphaId"
}

if (-not $SkipCompare) {
  Step "6) Build shadow vs live compare report"
  Run-Step "python scripts/shadow_live_compare_report.py --base-dir $BaseDir --date $Date --symbol $Symbol --journal-path `"$JournalPath`" --shadow-baseline-json `"$ShadowBaselineJson`" --output `"$compareOut`""
}

Step "DONE"
Write-Host "P1 daily run completed." -ForegroundColor Green
