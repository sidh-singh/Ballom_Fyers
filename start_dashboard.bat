@echo off
REM === fyers_live — Dashboard Launcher (read-only, separate process) ===
REM Modelled on Alcadeias start_dashboard.bat. No demo mode.
REM Usage: start_dashboard.bat [port]   (default 8050)

set "PYTHON_ROOT=C:\Users\Administrator\AppData\Local\Programs\Python\Python311"
set "PYTHON_EXE=%PYTHON_ROOT%\python.exe"

chcp 65001 > nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

set "PORT=%~1"
if "%PORT%"=="" set "PORT=8050"

REM === Install dashboard deps once (flag file) ===
if not exist ".\dash_installed.flag" (
    echo Installing dashboard dependencies...
    "%PYTHON_EXE%" -m pip install dash plotly
    echo Dashboard dependencies installed > ".\dash_installed.flag"
) else (
    echo Dashboard dependencies already installed, skipping...
)

echo ============================================================
echo   STARTING FYERS_LIVE DASHBOARD
echo   Open browser at http://localhost:%PORT%
echo ============================================================
echo.

"%PYTHON_EXE%" -X utf8 -u dashboard.py %PORT%

echo.
echo ============= DASHBOARD STOPPED =============
