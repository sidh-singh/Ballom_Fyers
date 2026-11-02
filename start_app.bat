@echo off
REM === fyers_live — Engine Process (LIVE only) ===
REM Modelled on Alcadeias start_btcusd.bat for server / Jenkins runs. No demo mode.
REM One process runs ALL enabled symbols (per-symbol worker threads inside the engine).
REM Usage: start_app.bat

REM === Python 3.11 (fyers-apiv3's aiohttp pin has no 3.13 wheel) ===
REM Edit PYTHON_ROOT if Python is installed elsewhere on this machine.
set "PYTHON_ROOT=C:\Users\Administrator\AppData\Local\Programs\Python\Python311"
set "PYTHON_EXE=%PYTHON_ROOT%\python.exe"

REM --- Ensure UTF-8 (Windows console + Jenkins compatibility) ---
chcp 65001 > nul
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

echo ============================================================
echo   STARTING FYERS_LIVE ENGINE  (LIVE — real orders when funded)
echo ============================================================
echo.

REM === Install dependencies once (flag file) ===
if not exist ".\deps_installed.flag" (
    echo Installing dependencies...
    "%PYTHON_EXE%" -m ensurepip --upgrade
    "%PYTHON_EXE%" -m pip install --upgrade pip setuptools wheel
    "%PYTHON_EXE%" -m pip install -r requirements_fyers.txt
    echo Dependencies installed > ".\deps_installed.flag"
) else (
    echo Dependencies already installed, skipping...
)

"%PYTHON_EXE%" -X utf8 -u application.py

echo.
echo ============= FYERS_LIVE ENGINE STOPPED =============
