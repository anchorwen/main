# Quant OS — Scheduled Daily Operations
# Runs daily_ops.py via Windows Task Scheduler.
#
# Register:
#   schtasks /Create /SC DAILY /TN "QuantOS\DailyOps" ^
#     /TR "powershell -NoProfile -ExecutionPolicy Bypass -File D:\future\scripts\scheduled_daily_ops.ps1" ^
#     /ST 22:00
#
# Test:
#   schtasks /Run /TN "QuantOS\DailyOps"

$ErrorActionPreference = "Stop"
$ROOT = "D:\future"
$LOG_DIR = Join-Path $ROOT "data\logs"
New-Item -ItemType Directory -Force -Path $LOG_DIR | Out-Null

$DATE_STAMP = (Get-Date).ToString("yyyy-MM-dd")
$LOG_FILE = Join-Path $LOG_DIR "daily_ops_${DATE_STAMP}.log"

# Prevent concurrent runs
$LOCK = Join-Path $ROOT "data\daily_ops_running.flag"
if (Test-Path $LOCK) {
    $msg = "[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] SKIP: previous run still in progress"
    Add-Content -Path $LOG_FILE -Value $msg
    exit 0
}
New-Item -ItemType File -Path $LOCK -Force | Out-Null

try {
    $msg = @"
============================================================
[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Quant OS Daily Operations starting
============================================================
"@
    Add-Content -Path $LOG_FILE -Value $msg

    Set-Location $ROOT

    $pyArgs = @(
        "scripts/daily_ops.py",
        "--base-dir", "data",
        "--output", "data/reports/daily_ops.json",
        "--mt5-terminal-path", "D:\MetaTrader 5\terminal64.exe"
    )
    $proc = Start-Process -FilePath "python" -ArgumentList $pyArgs -NoNewWindow -Wait -PassThru -RedirectStandardOutput $LOG_FILE -RedirectStandardError $LOG_FILE
    $EXIT_CODE = $proc.ExitCode

    $result = switch ($EXIT_CODE) {
        0 { "OK, no actions, no errors" }
        1 { "ACTIONS APPLIED — review data\reports\daily_ops.json" }
        2 { "ERRORS — check log for step failures" }
        default { "UNEXPECTED exit code $EXIT_CODE" }
    }
    $endMsg = @"
------------------------------------------------------------
[$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')] Finished — exit code $EXIT_CODE
Result: $result
"@
    Add-Content -Path $LOG_FILE -Value $endMsg
} finally {
    Remove-Item -Path $LOCK -Force -ErrorAction SilentlyContinue
}

exit $EXIT_CODE
