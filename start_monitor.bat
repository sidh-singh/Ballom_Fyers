@echo off
REM === Ballom Health Check — MONITOR (polls every server in ip_list.txt) ===
REM Usage: start_monitor.bat         (leave it running; it writes status to C:\Ballom_HealthCheck)
set "PYTHON_EXE=python"
chcp 65001 > nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

if not exist ".\deps_installed.flag" (
    echo Installing dependencies...
    "%PYTHON_EXE%" -m pip install -r requirements.txt
    echo installed > ".\deps_installed.flag"
)

echo ============================================================
echo   HEALTH MONITOR running (Ctrl-C to stop)
echo ============================================================
"%PYTHON_EXE%" -X utf8 -u monitor.py
echo ============= MONITOR STOPPED =============
