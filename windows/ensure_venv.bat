@echo off
REM agentchattr - makes sure .venv exists and has the required packages.
REM Called by the start scripts from the project root. Exits non-zero (after a pause) if setup fails.
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    python -m venv .venv
    if errorlevel 1 (
        echo ERROR: could not create .venv. Is Python installed and on PATH?
        pause
        exit /b 1
    )
)
".venv\Scripts\python.exe" -c "import fastapi, uvicorn, mcp" >nul 2>nul
if errorlevel 1 (
    echo Installing dependencies from requirements.txt...
    ".venv\Scripts\python.exe" -m pip install -r requirements.txt
    if errorlevel 1 (
        echo ERROR: dependency install failed. See the messages above.
        pause
        exit /b 1
    )
)
exit /b 0
