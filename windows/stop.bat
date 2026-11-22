@echo off
REM agentchattr - stops the server only (agent wrappers keep running and reconnect)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0server_ctl.ps1" stop
set "RESULT=%ERRORLEVEL%"
if not "%RESULT%"=="0" (
    echo.
    echo Server control failed with exit code %RESULT%. See details above.
    pause
)
exit /b %RESULT%
