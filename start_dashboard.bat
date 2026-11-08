@echo off
REM === Ballom Health Check — DASHBOARD ===
REM Usage: start_dashboard.bat [port]      (default 9000)
set "PYTHON_EXE=python"
chcp 65001 > nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1
set "PORT=%~1"
if "%PORT%"=="" set "PORT=9000"

if not exist ".\deps_installed.flag" (
    echo Installing dependencies...
    "%PYTHON_EXE%" -m pip install -r requirements.txt
    echo installed > ".\deps_installed.flag"
)

echo ============================================================
echo   HEALTH DASHBOARD  -^>  http://localhost:%PORT%
echo ============================================================
"%PYTHON_EXE%" -X utf8 -u dashboard.py %PORT%
echo ============= DASHBOARD STOPPED =============
