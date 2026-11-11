@echo off
REM agentchattr - stops the server, waits for it to exit, restarts it and waits until it is listening
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0server_ctl.ps1" restart
exit /b %ERRORLEVEL%
