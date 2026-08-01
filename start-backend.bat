@echo off
REM ===========================================================================
REM  Start the API. Double-click this, or run `start-backend.bat` in a terminal.
REM
REM  It creates the virtual environment and installs dependencies on first run,
REM  so there is nothing to set up by hand.
REM ===========================================================================

cd /d "%~dp0backend"

REM Checked before anything else. uvicorn's auto-reloader runs the app in a
REM child process, and closing this window instead of pressing Ctrl+C orphans
REM that child - it keeps port 8000 with no visible window attached to it. The
REM next run then fails to bind, or worse, appears to start while every request
REM is actually served by yesterday's code.
netstat -ano | findstr /r /c:"TCP.*:8000 .*LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo.
    echo   Port 8000 is already in use, most likely by an API server left
    echo   running from a previous session.
    echo.
    set /p KILLIT="  Stop it and continue? [Y/n] "
    if /i "%KILLIT%"=="n" goto :skipkill
    for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:"TCP.*:8000 .*LISTENING"') do (
        echo   Stopping process %%p...
        taskkill /f /pid %%p >nul 2>&1
    )
    timeout /t 2 /nobreak >nul
    echo   Done.
    :skipkill
)

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
echo Press Ctrl+C to stop ^(closing the window leaves the process running^).
echo.

REM run.py rather than uvicorn: uvicorn picks an event loop on Windows that
REM the database driver cannot use. See backend/run.py.
.venv\Scripts\python.exe run.py --port 8000
pause
