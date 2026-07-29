@echo off
REM ===========================================================================
REM  Check every API key and service with a real call, and report what works.
REM  Run this whenever something seems broken.
REM ===========================================================================

cd /d "%~dp0"

if not exist "backend\.venv\Scripts\python.exe" (
    echo No virtual environment yet. Run start-backend.bat once first.
    pause
    exit /b 1
)

set PYTHONIOENCODING=utf-8
backend\.venv\Scripts\python.exe scripts\verify_setup.py
echo.
pause
