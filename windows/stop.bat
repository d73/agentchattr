@echo off
REM agentchattr — stops the server only (agent wrappers keep running and reconnect)
set PORT=8300
set FOUND=0
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /R /C:":%PORT% .*LISTENING"') do (
    set FOUND=1
    taskkill /PID %%P /F >nul 2>nul
    echo Stopped server process %%P
)
if "%FOUND%"=="0" echo No server listening on port %PORT%.
exit /b 0
