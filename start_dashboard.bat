@echo off
REM ================================================================================
REM  FYERS_LIVE DASHBOARD launcher (read-only, separate process).
REM  Usage: start_dashboard.bat [port]   (default 8050)
REM ================================================================================
chcp 65001 >nul 2>&1
set PYTHONUTF8=1

set "PY=py -3.11"
%PY% --version >nul 2>&1
if errorlevel 1 set "PY=python"

set "PORT=%1"
if "%PORT%"=="" set "PORT=8050"

%PY% -c "import dash" >nul 2>&1
if errorlevel 1 (
    echo [INFO] Installing dash + plotly...
    %PY% -m pip install dash plotly
)

echo Dashboard -> http://127.0.0.1:%PORT%
%PY% -X utf8 -u dashboard.py %PORT%
if errorlevel 1 pause
