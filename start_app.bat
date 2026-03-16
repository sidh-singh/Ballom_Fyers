@echo off
REM ================================================================================
REM === UPDATER (NIFTY) LAUNCHER — SHA + RSI signal analysis service
REM ================================================================================
REM Usage:  start_app.bat [demo|live]
REM
REM Starts the NIFTY updater in a forever-restart loop so it automatically
REM recovers from crashes.  Reads token from C:/Ballom_FYR/fyers_token.json
REM (written by dev_scanner branch).
REM ================================================================================

REM === Jenkins Unicode fix ===
chcp 65001 >nul 2>&1

REM === Force Python to use UTF-8 encoding ===
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

REM === Python path (change if your Python is installed elsewhere) ===
set "PYTHON_ROOT=C:\Users\Administrator\AppData\Local\Programs\Python\Python311"
set "PYTHON_EXE=%PYTHON_ROOT%\python.exe"

REM === Fallback: if the configured path doesn't exist, try system Python ===
if not exist "%PYTHON_EXE%" (
    echo [INFO] Configured Python not found at %PYTHON_EXE%
    echo [INFO] Falling back to system Python from PATH...
    set "PYTHON_EXE=python"
)

echo.
echo ================================================================================
echo              FYERS UPDATER (NIFTY) LAUNCHER  (dev_updater_nifty branch)
echo ================================================================================
echo Python: %PYTHON_EXE%
echo.

REM === Get mode argument ===
set "MODE=%1"
if "%MODE%"=="" (
    set "MODE=demo"
    echo [INFO] Mode not specified, defaulting to DEMO mode for safety
)

echo.
echo ================================================================================
echo Mode:     %MODE%
echo ================================================================================

REM === Safety warning for LIVE mode ===
if /i "%MODE%"=="live" goto :LIVE_WARNING
goto :DEMO_INFO

:LIVE_WARNING
echo.
echo ********************************************************************************
echo                           LIVE MODE ACTIVE
echo ********************************************************************************
echo.
echo    LIVE mode — reads real token from dev_scanner.
echo    Updater does NOT place orders, but signal_state.json
echo    is used by dev_trading for live trading decisions.
echo.
echo ********************************************************************************
echo.
goto :CONTINUE_SCRIPT

:DEMO_INFO
echo.
echo [DEMO MODE] Uses demo state directory — no real-money risk
echo.

:CONTINUE_SCRIPT

echo ============= STARTING UPDATER (NIFTY) =============

REM === Install dependencies on first run ===
if not exist ".\deps_installed.flag" goto :INSTALL_DEPS
goto :SKIP_DEPS

:INSTALL_DEPS
echo Installing dependencies...
"%PYTHON_EXE%" -m ensurepip --upgrade
"%PYTHON_EXE%" -m pip install --upgrade pip setuptools wheel
"%PYTHON_EXE%" -m pip install -r requirements_fyers.txt
echo Dependencies installed > ".\deps_installed.flag"
goto :RUN_SCRIPT

:SKIP_DEPS
echo Dependencies already installed, skipping...

:RUN_SCRIPT
echo.
echo ================================================================================
echo Launching updater (NIFTY) in %MODE% mode
echo ================================================================================
echo.

REM === Forever-restart loop — auto-recover from crashes ===
:FOREVER
"%PYTHON_EXE%" -X utf8 -u application.py %MODE%
echo.
echo [WARNING] Updater exited unexpectedly — restarting in 10 seconds...
echo           Press Ctrl+C to abort.
timeout /t 10 /nobreak >nul
goto :FOREVER
