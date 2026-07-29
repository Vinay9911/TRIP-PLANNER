@echo off
REM ===========================================================================
REM  Run the test suite. Needs no internet, no database and no API keys.
REM ===========================================================================

cd /d "%~dp0backend"

if not exist ".venv\Scripts\python.exe" (
    echo No virtual environment yet. Run start-backend.bat once first.
    pause
    exit /b 1
)

REM PYTHONIOENCODING=utf-8 stops the Windows console crashing on the Japanese
REM and Arabic text in the multilingual tests.
set PYTHONIOENCODING=utf-8

echo Running tests...
echo.
.venv\Scripts\python.exe -m pytest -q
echo.
pause
