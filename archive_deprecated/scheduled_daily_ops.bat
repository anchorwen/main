@echo off
REM ============================================================
REM Quant OS — Scheduled Daily Operations
REM Runs daily_ops.py (shadow, feedback, governance, champion,
REM   retraining, recap, alpha) via Windows Task Scheduler.
REM
REM Register:
REM   schtasks /Create /SC DAILY /TN "QuantOS\DailyOps" ^
REM     /TR "D:\future\scripts\scheduled_daily_ops.bat" ^
REM     /ST 22:00
REM
REM Test run:
REM   schtasks /Run /TN "QuantOS\DailyOps"
REM ============================================================
set ROOT=D:\future
set LOG_DIR=%ROOT%\data\logs

if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"

REM Derive date stamp from environment (works on all Windows locales)
set DATE_STAMP=%DATE:~-4%-%DATE:~-10,-8%-%DATE:~-7,-5%
if "%DATE_STAMP:~0,1%"==" " set DATE_STAMP=0%DATE_STAMP:~1%
set LOG_FILE=%LOG_DIR%\daily_ops_%DATE_STAMP%.log

cd /d %ROOT%
if errorlevel 1 (
    echo ERROR: cannot cd to %ROOT% >> "%LOG_FILE%"
    exit /b 1
)

REM Prevent concurrent runs via lock file
set LOCK=%ROOT%\data\daily_ops_running.flag
if exist "%LOCK%" (
    echo [%DATE% %TIME%] SKIP: previous run still in progress >> "%LOG_FILE%"
    exit /b 0
)
echo RUNNING > "%LOCK%"

echo ============================================================ >> "%LOG_FILE%"
echo [%DATE% %TIME%] Quant OS Daily Operations starting >> "%LOG_FILE%"
echo ============================================================ >> "%LOG_FILE%"

python scripts/daily_ops.py --base-dir data --output data/reports/daily_ops.json --mt5-terminal-path "D:\MetaTrader 5\terminal64.exe" >> "%LOG_FILE%" 2>&1

set EXIT_CODE=%ERRORLEVEL%

del "%LOCK%" 2>nul

echo ------------------------------------------------------------ >> "%LOG_FILE%"
echo [%DATE% %TIME%] Finished — exit code %EXIT_CODE% >> "%LOG_FILE%"

if %EXIT_CODE% equ 0 (
    echo Result: OK, no actions, no errors >> "%LOG_FILE%"
) else if %EXIT_CODE% equ 1 (
    echo Result: ACTIONS APPLIED — review data\reports\daily_ops.json >> "%LOG_FILE%"
) else if %EXIT_CODE% equ 2 (
    echo Result: ERRORS — check log for step failures >> "%LOG_FILE%"
)

exit /b %EXIT_CODE%
