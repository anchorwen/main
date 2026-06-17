@echo off
REM Setup hourly data integrity audit via Windows Task Scheduler
REM DQAF-20260616-005/GAP4: automated silent monitoring
REM
REM Usage: Run once as Administrator to install the scheduled task.
REM        The task runs silently every hour at :05 past the hour.
REM        Alerts only on Sev1/Sev2 — "no news is good news".

set TASK_NAME=QuantOS_Hourly_Audit
set SCRIPT_PATH=%~dp0audit_data_integrity.py
set PYTHON_PATH=python

echo Creating scheduled task: %TASK_NAME%
echo Script: %SCRIPT_PATH%
echo.

schtasks /create /tn "%TASK_NAME%" /tr "cd /d %~dp0.. && %PYTHON_PATH% %SCRIPT_PATH% --quiet --alert" /sc HOURLY /mo 1 /st 00:05 /f

if %ERRORLEVEL% EQU 0 (
    echo [OK] Task created successfully.
    echo Run 'schtasks /query /tn %TASK_NAME%' to verify.
) else (
    echo [ERROR] Failed to create task. Run as Administrator.
    pause
)
