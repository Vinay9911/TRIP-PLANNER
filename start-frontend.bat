@echo off
REM ===========================================================================
REM  Start the web interface. Run start-backend.bat first, in another window.
REM ===========================================================================

cd /d "%~dp0frontend"

if not exist "node_modules" (
    echo Installing dependencies ^(first run only, takes a few minutes^)...
    call npm install
    if errorlevel 1 (
        echo.
        echo npm install failed. Is Node.js installed? https://nodejs.org
        pause
        exit /b 1
    )
)

if not exist ".env.local" (
    echo.
    echo No .env.local found. Copy frontend\.env.example to frontend\.env.local
    echo and fill it in first.
    pause
    exit /b 1
)

echo.
echo Starting the web interface on http://localhost:3000
echo Press Ctrl+C to stop.
echo.

call npm run dev
pause
