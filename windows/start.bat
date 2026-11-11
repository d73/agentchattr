@echo off
REM agentchattr — starts the server only
cd /d "%~dp0.."

REM Auto-create venv and install deps on first run
call "%~dp0ensure_venv.bat"
if errorlevel 1 exit /b 1
call .venv\Scripts\activate.bat

python "%CD%\run.py"
echo.
echo === Server exited with code %ERRORLEVEL% ===
pause
