@echo off
REM ================================================================================
REM  FYERS_LIVE ENGINE launcher (LIVE only). Forever-restart loop.
REM  Requires Python 3.11 (fyers-apiv3's aiohttp pin has no 3.13 wheel).
REM ================================================================================
chcp 65001 >nul 2>&1
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

set "PY=py -3.11"
%PY% --version >nul 2>&1
if errorlevel 1 set "PY=python"

echo ================================================================================
echo              FYERS_LIVE ENGINE   (LIVE)   -   %PY%
echo ================================================================================

if not exist ".\deps_installed.flag" (
    echo [INFO] Installing dependencies...
    %PY% -m pip install -r requirements_fyers.txt
    if errorlevel 1 ( echo [ERROR] dependency install failed & pause & exit /b 1 )
    echo installed > ".\deps_installed.flag"
)

:FOREVER
%PY% -X utf8 -u application.py
echo.
echo [WARNING] Engine exited — restarting in 10 seconds (Ctrl+C to abort)...
timeout /t 10 /nobreak >nul
goto FOREVER
