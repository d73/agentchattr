@echo off
REM agentchattr — stops the server, waits until it is gone, then restarts it (agents stay up)
setlocal enabledelayedexpansion
set PORT=8300
cd /d "%~dp0.."

call "%~dp0stop.bat"

echo Waiting for port %PORT% to be released...
set /a TRIES=0
:wait
netstat -ano | findstr /R /C:":%PORT% .*LISTENING" >nul
if errorlevel 1 goto stopped
set /a TRIES+=1
if !TRIES! GEQ 30 (
    echo Server did not stop after 30s. Aborting.
    exit /b 1
)
timeout /t 1 /nobreak >nul
goto wait

:stopped
if not exist ".venv" (
    python -m venv .venv
    .venv\Scripts\pip install -q -r requirements.txt >nul 2>nul
)
call .venv\Scripts\activate.bat
echo Starting server...
start "agentchattr server" /min python run.py
echo Server restarted. Refresh the web UI if it does not reconnect.
