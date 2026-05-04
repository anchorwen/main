@echo off
REM ============================================================
REM Quant OS — Scheduled Daily Recap
REM Runs live_daily_recap.py and appends to EVOLUTION_PLAN.md
REM ============================================================
setlocal

set ROOT=D:\future
set LOG_DIR=%ROOT%\data\logs
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

set DATE_STAMP=%DATE:~0,4%-%DATE:~5,2%-%DATE:~8,2%
set LOG_FILE=%LOG_DIR%\daily_recap_%DATE_STAMP%.log

cd /d %ROOT%

echo [%DATE_STAMP% %TIME%] Starting automated daily recap... >> "%LOG_FILE%"

python scripts/live_daily_recap.py ^
  --base-dir data ^
  --symbol XAUUSDc ^
  --evolution-plan EVOLUTION_PLAN.md ^
  --decisions-dir data/decisions ^
  --feature-store-dir data/feature_store ^
  --brains-dir configs/brains ^
  --output data/reports/daily_recap.json ^
  >> "%LOG_FILE%" 2>&1

set EXIT_CODE=%ERRORLEVEL%

if not exist "data\reports" mkdir data\reports
echo [%DATE_STAMP% %TIME%] Recap finished, exit code: %EXIT_CODE% >> "%LOG_FILE%"

exit /b %EXIT_CODE%
