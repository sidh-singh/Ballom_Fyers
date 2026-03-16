@echo off
chcp 65001 >nul 2>&1
setlocal

:: ────────────────────────────────────────────────────────
::  Ballom FYR — Dashboard Launcher
::  Usage:
::    start_dashboard.bat              → auto-detect mode, port 8050
::    start_dashboard.bat demo         → force demo mode
::    start_dashboard.bat live         → force live mode
::    start_dashboard.bat demo 8060    → demo mode on port 8060
:: ────────────────────────────────────────────────────────

set "PYTHON=python"
set "MODE=%~1"
set "PORT=%~2"

:: Default mode is auto-detect (no arg → dashboard.py auto-detects)
if "%MODE%"=="" set "MODE="
:: Default port is 8050
if "%PORT%"=="" set "PORT=8050"

echo.
echo ╔═════════════════════════════════════════════════╗
echo ║        Ballom FYR — Dashboard Launcher          ║
echo ╚═════════════════════════════════════════════════╝
echo.

:: Check if Python is available
%PYTHON% --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found in PATH. Please install Python 3.11+
    pause
    exit /b 1
)

:: Check if dash is installed, auto-install if missing
%PYTHON% -c "import dash" >nul 2>&1
if errorlevel 1 (
    echo [INFO] Dash not found. Installing dash and plotly...
    %PYTHON% -m pip install dash plotly --quiet
    if errorlevel 1 (
        echo [ERROR] Failed to install dash/plotly. Please install manually:
        echo         pip install dash plotly
        pause
        exit /b 1
    )
    echo [OK] dash and plotly installed successfully.
)

:: Build command args
set "ARGS="
if not "%MODE%"=="" set "ARGS=%MODE%"
if not "%ARGS%"=="" (
    set "ARGS=%ARGS% %PORT%"
) else (
    set "ARGS=%PORT%"
)

echo [START] Launching dashboard...
echo         Mode: %MODE% (empty=auto-detect)
echo         Port: %PORT%
echo         URL:  http://127.0.0.1:%PORT%
echo.

%PYTHON% dashboard.py %ARGS%

if errorlevel 1 (
    echo.
    echo [ERROR] Dashboard exited with an error.
    pause
)

endlocal
