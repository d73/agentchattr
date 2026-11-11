@echo off
REM agentchattr — starts the server only
cd /d "%~dp0.."

REM Auto-create venv and install deps on first run
call "%~dp0ensure_venv.bat"
if errorlevel 1 exit /b 1

"%VENV_PY%" "%CD%\run.py"
echo.
echo === Server exited with code %ERRORLEVEL% ===
pause
