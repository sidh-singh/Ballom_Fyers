@echo off
REM === Ballom Health Check — start BOTH monitor + dashboard (Windows) ===
REM Usage: start_all.bat [port]     (dashboard default 9000)
echo Starting health monitor in a new window...
start "Ballom Health Monitor" cmd /k start_monitor.bat
timeout /t 3 > nul
echo Starting dashboard...
call start_dashboard.bat %~1
