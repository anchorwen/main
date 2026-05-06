# DEPRECATED — use: python scripts/send_live_order.py directly.
param(
  [string]$BaseDir = "data",
  [string]$Mt5TerminalPath = "D:\MetaTrader 5\terminal64.exe",
  [string]$Symbol = "XAUUSDc",
  [ValidateSet("long", "short")]
  [string]$Side = "long",
  [double]$StopLoss,
  [double]$TakeProfit,
  [string]$IntentId = "",
  [string]$CorrelationId = "",
  [switch]$ProcessOnce
)

$ErrorActionPreference = "Stop"

if ($StopLoss -le 0 -or $TakeProfit -le 0) {
  throw "StopLoss and TakeProfit must be positive numbers."
}

if ([string]::IsNullOrWhiteSpace($IntentId)) {
  $IntentId = "live_open_" + [guid]::NewGuid().ToString("N")
}
if ([string]::IsNullOrWhiteSpace($CorrelationId)) {
  $CorrelationId = "live_corr_" + [guid]::NewGuid().ToString("N")
}

Set-Location "D:\cursor"

Write-Host "==== Dispatch live order handoff ====" -ForegroundColor Cyan
python scripts/send_live_order.py `
  --base-dir $BaseDir `
  --mt5-terminal-path $Mt5TerminalPath `
  --symbol $Symbol `
  --side $Side `
  --stop-loss $StopLoss `
  --take-profit $TakeProfit `
  --intent-id $IntentId `
  --correlation-id $CorrelationId

if ($ProcessOnce) {
  Write-Host "==== Process outbox once ====" -ForegroundColor Cyan
  python scripts/mt5_bridge_worker.py `
    --outbox-dir "$BaseDir/mt5_outbox" `
    --receipt-dir "$BaseDir/receipts" `
    --archive-dir "$BaseDir/mt5_outbox_processed" `
    --default-volume 0.01 `
    --deviation 20 `
    --magic 90001 `
    --once
}

Write-Host "send_live_order completed." -ForegroundColor Green
