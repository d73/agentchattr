@echo off
REM agentchattr - stops the server only (agent wrappers keep running and reconnect)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0server_ctl.ps1" stop
exit /b %ERRORLEVEL%
