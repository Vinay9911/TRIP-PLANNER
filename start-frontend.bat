@echo off
REM ===========================================================================
REM  Start the web interface. Run start-backend.bat first, in another window.
REM
REM  Port 3000 is checked before starting, and that check is the point of this
REM  file rather than a nicety. Next.js does not fail when 3000 is taken - it
REM  quietly moves to 3001 and prints the new address in among its startup
REM  output. Two things then go wrong at once: the browser tab still open on
REM  localhost:3000 shows a STALE build with none of your changes, and the API
REM  rejects every request from :3001 because CORS_ORIGINS does not list it.
REM  The result looks like the app is broken when it is simply somewhere else.
REM ===========================================================================

cd /d "%~dp0frontend"

netstat -ano | findstr /r /c:"TCP.*:3000 .*LISTENING" >nul 2>&1
if not errorlevel 1 (
    echo.
    echo   Port 3000 is already in use.
    echo.
    echo   That is almost always an older copy of this site still running from
    echo   a previous session - often one whose window was closed without
    echo   Ctrl+C, which leaves the process behind.
    echo.
    echo   Leaving it there means you would keep seeing the OLD version at
    echo   localhost:3000 while the new one starts on 3001.
    echo.
    set /p KILLIT="  Stop it and continue? [Y/n] "
    if /i "%KILLIT%"=="n" goto :skipkill
    for /f "tokens=5" %%p in ('netstat -ano ^| findstr /r /c:"TCP.*:3000 .*LISTENING"') do (
        echo   Stopping process %%p...
        taskkill /f /pid %%p >nul 2>&1
    )
    timeout /t 2 /nobreak >nul
    echo   Done.
    :skipkill
)

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
echo Starting the web interface. Next.js prints the real address below -
echo read it rather than assuming 3000, in case the port moved.
echo Press Ctrl+C to stop ^(closing the window leaves the process running^).
echo.

call npm run dev
pause
