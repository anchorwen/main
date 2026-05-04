param(
  [string]$RepoRoot = "D:\cursor",
  [string]$BaseDir = "data",
  [string]$Symbol = "XAUUSDc",
  [string]$AlphaId = "alpha_xau_live",
  [string]$AlphaName = "",
  [string]$ShadowBaselineJson = "data/replays/v9_shadow_baselines/neutral_stability/neutral_case.baseline.json",
  [int]$LoopSleepSeconds = 20,
  [int]$PolicyIntervalSeconds = 60,
  [double]$DefaultVolume = 0.01,
  [string]$Mt5TerminalPath = "",
  [switch]$SkipProbeSpread,
  [switch]$SkipPolicy,
  [switch]$ApplyEvaluate,
  [switch]$SkipCompare,
  [switch]$DryRun
)

$ErrorActionPreference = "Stop"

function Step([string]$Message) {
  Write-Host ""
  Write-Host "==== $Message ====" -ForegroundColor Cyan
}

function Ensure-Dir([string]$Path) {
  if (-not (Test-Path -LiteralPath $Path)) {
    New-Item -ItemType Directory -Path $Path -Force | Out-Null
  }
}

function Build-BridgeArgs() {
  return @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", (Join-Path $RepoRoot "scripts/run_bridge_forever.ps1"),
    "-RepoRoot", $RepoRoot,
    "-BaseDir", $BaseDir,
    "-DefaultVolume", "$DefaultVolume"
  )
}

function Start-BridgeSupervisor([string]$LogPath) {
  $argList = Build-BridgeArgs
  if ($DryRun) {
    Write-Host "[dry-run] start bridge supervisor: powershell $($argList -join ' ')" -ForegroundColor Yellow
    return $null
  }
  $errLogPath = "$LogPath.err"
  return Start-Process -FilePath "powershell" -ArgumentList $argList -RedirectStandardOutput $LogPath -RedirectStandardError $errLogPath -PassThru -WindowStyle Hidden
}

function Invoke-LiveDispatchPolicy([string]$JsonPath, [string]$LogPath) {
  $policyScript = Join-Path $RepoRoot "scripts/live_dispatch_policy.py"
  $journalPath = Join-Path $BaseDir "live_trade_journal.jsonl"
  $calendarPath = Join-Path $BaseDir "config/market_calendar.json"
  $flagPath = Join-Path $BaseDir "live_dispatch_block.flag"
  $pyArgs = @(
    $policyScript,
    "--base-dir", $BaseDir,
    "--journal-path", $journalPath,
    "--calendar-path", $calendarPath,
    "--flag-path", $flagPath,
    "--symbol", $Symbol,
    "--output", $JsonPath
  )
    if (-not $SkipProbeSpread) {
    $pyArgs += "--probe-spread"
    if (-not [string]::IsNullOrWhiteSpace($Mt5TerminalPath)) {
      $pyArgs += @("--mt5-terminal-path", $Mt5TerminalPath)
    }
  }
  if ($DryRun) {
    Write-Host "[dry-run] python $($pyArgs -join ' ') | Tee-Object $LogPath" -ForegroundColor Yellow
    return 0
  }
  $output = & python @pyArgs *>&1
  $code = $LASTEXITCODE
  $output | Tee-Object -FilePath $LogPath -Append | Out-Host
  return $code
}

function Invoke-P1DailyRun([string]$DateKey, [string]$LogPath) {
  $args = @(
    "-NoProfile",
    "-ExecutionPolicy", "Bypass",
    "-File", (Join-Path $RepoRoot "scripts/p1_daily_run.ps1"),
    "-RepoRoot", $RepoRoot,
    "-BaseDir", $BaseDir,
    "-Date", $DateKey,
    "-Symbol", $Symbol,
    "-AlphaId", $AlphaId,
    "-UseCliIngestOnly"
  )
  if (-not [string]::IsNullOrWhiteSpace($Mt5TerminalPath)) {
    $args += @("-Mt5TerminalPath", $Mt5TerminalPath)
  }
  if (-not [string]::IsNullOrWhiteSpace($AlphaName)) {
    $args += @("-AlphaName", $AlphaName)
  }
  if ($ApplyEvaluate) {
    $args += "-ApplyEvaluate"
  }
  if ($SkipCompare) {
    $args += "-SkipCompare"
  }
  if ($DryRun) {
    Write-Host "[dry-run] run p1 daily: powershell $($args -join ' ')" -ForegroundColor Yellow
    return 0
  }

  $output = & powershell @args *>&1
  $code = $LASTEXITCODE
  $output | Tee-Object -FilePath $LogPath -Append | Out-Host
  return $code
}

Set-Location $RepoRoot
$opsLogDir = Join-Path $BaseDir "reports/ops_logs"
$opsStateDir = Join-Path $BaseDir "reports/ops_state"
Ensure-Dir $opsLogDir
Ensure-Dir $opsStateDir

$runKey = "$AlphaId-$Symbol".ToLower()
$stateFile = Join-Path $opsStateDir "p1_last_run_$runKey.txt"
$bridgeLog = Join-Path $opsLogDir "bridge_supervisor.log"
$dailyLog = Join-Path $opsLogDir "p1_daily_run.log"
$policyJson = Join-Path $opsLogDir "live_dispatch_policy.json"
$policyLog = Join-Path $opsLogDir "live_dispatch_policy.log"

Step "Live Ops Autopilot Parameters"
Write-Host "repo_root=$RepoRoot"
Write-Host "base_dir=$BaseDir"
Write-Host "symbol=$Symbol"
Write-Host "alpha_id=$AlphaId"
Write-Host "alpha_name=$AlphaName"
Write-Host "apply_evaluate=$ApplyEvaluate"
Write-Host "skip_compare=$SkipCompare"
Write-Host "loop_sleep_seconds=$LoopSleepSeconds"
Write-Host "policy_interval_seconds=$PolicyIntervalSeconds"
Write-Host "default_volume=$DefaultVolume"
Write-Host "skip_probe_spread=$SkipProbeSpread"
Write-Host "skip_policy=$SkipPolicy"
Write-Host "state_file=$stateFile"
Write-Host "bridge_log=$bridgeLog"
Write-Host "daily_log=$dailyLog"
Write-Host "policy_json=$policyJson"
Write-Host "dry_run=$DryRun"

$bridgeProc = $null
$lastRunDate = ""
$lastPolicyUtc = $null
if (Test-Path -LiteralPath $stateFile) {
  $lastRunDate = (Get-Content -LiteralPath $stateFile -Raw).Trim()
}

while ($true) {
  $todayUtc = (Get-Date).ToUniversalTime().ToString("yyyy-MM-dd")
  $nowUtc = [DateTime]::UtcNow

  if (-not $SkipPolicy) {
    $elapsed = if ($null -eq $lastPolicyUtc) { [double]::PositiveInfinity } else { ($nowUtc - $lastPolicyUtc).TotalSeconds }
    if ($DryRun -or $null -eq $lastPolicyUtc -or $elapsed -ge $PolicyIntervalSeconds) {
      Step "Live dispatch policy (UTC calendar + journal + optional spread)"
      $pc = Invoke-LiveDispatchPolicy -JsonPath $policyJson -LogPath $policyLog
      if (-not $DryRun -and $pc -ne 0) {
        Write-Host "live_dispatch_policy: dispatch_blocked_or_alert exit_code=$pc (see $policyJson)" -ForegroundColor Yellow
      }
      $lastPolicyUtc = [DateTime]::UtcNow
    }
  }

  if ($null -eq $bridgeProc -or $bridgeProc.HasExited) {
    Step "Bridge supervisor not running, starting/restarting"
    if ($null -ne $bridgeProc -and $bridgeProc.HasExited) {
      Write-Host "previous_bridge_exit_code=$($bridgeProc.ExitCode)" -ForegroundColor Yellow
    }
    $bridgeProc = Start-BridgeSupervisor -LogPath $bridgeLog
    if ($null -ne $bridgeProc) {
      Write-Host "bridge_pid=$($bridgeProc.Id)"
    }
  }

  if ($lastRunDate -ne $todayUtc) {
    Step "Trigger P1 daily run for UTC date $todayUtc"
    $code = Invoke-P1DailyRun -DateKey $todayUtc -LogPath $dailyLog
    if ($code -eq 0) {
      $lastRunDate = $todayUtc
      if (-not $DryRun) {
        Set-Content -LiteralPath $stateFile -Value $todayUtc -Encoding utf8
      }
      Write-Host "p1_daily_run status=success date=$todayUtc" -ForegroundColor Green
    } else {
      Write-Host "p1_daily_run status=failed exit_code=$code date=$todayUtc (will retry on next loop)" -ForegroundColor Yellow
    }
  }

  if ($DryRun) {
    Step "Dry run complete"
    break
  }
  Start-Sleep -Seconds $LoopSleepSeconds
}
