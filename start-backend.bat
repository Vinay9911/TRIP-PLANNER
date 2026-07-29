@echo off
REM ===========================================================================
REM  Start the API. Double-click this, or run `start-backend.bat` in a terminal.
REM
REM  It creates the virtual environment and installs dependencies on first run,
REM  so there is nothing to set up by hand.
REM ===========================================================================

cd /d "%~dp0backend"

if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment ^(first run only, takes a few minutes^)...
    python -m venv .venv
    if errorlevel 1 (
        echo.
        echo Could not create the virtual environment. Is Python 3.11+ installed?
        echo Download it from https://www.python.org/downloads/
        pause
        exit /b 1
    )
    echo Installing dependencies...
    .venv\Scripts\python.exe -m pip install --quiet --upgrade pip
    .venv\Scripts\python.exe -m pip install --quiet -e ".[dev]"
)

if not exist "..\.env" (
    echo.
    echo No .env file found. Copy .env.example to .env and fill it in first.
    pause
    exit /b 1
)

echo.
echo Starting the API on http://localhost:8000
echo Interactive docs: http://localhost:8000/docs
echo Press Ctrl+C to stop.
echo.

REM run.py rather than uvicorn: uvicorn picks an event loop on Windows that
REM the database driver cannot use. See backend/run.py.
.venv\Scripts\python.exe run.py --port 8000
pause
