param(
  [string]$BaseDir = "data",
  [string]$Mt5TerminalPath = "D:\MetaTrader 5\terminal64.exe"
)

$ErrorActionPreference = "Stop"
Set-Location "D:\cursor"

Write-Host "==== Rollback to live-read-only ====" -ForegroundColor Red
python -m apps.engine.cli `
  --env production `
  --base-dir $BaseDir `
  --live-read-only `
  --mt5-terminal-path $Mt5TerminalPath `
  status

Write-Host "Rollback command applied." -ForegroundColor Green
