@echo off
REM ---------------------------------------------------------------------------
REM  Runs Delivery Helper on this machine so the team can reach it over the network.
REM  Leave this window open while people are using it; close it to shut it down.
REM
REM  Set PYTHON to use a particular interpreter, e.g. one an application whitelist allows.
REM ---------------------------------------------------------------------------

cd /d "%~dp0app"
title Delivery Helper

if not defined PYTHON set PYTHON=python

for %%F in ("..\.env" "..\config\users.json" "..\config\project.json") do (
    if not exist "%%~F" (
        echo  ERROR: %%~F is missing.
        echo  Copy the matching .example file next to it and fill it in.
        echo.
        pause
        exit /b 1
    )
)

REM  Two copies bound to the same port both "work", and requests land on whichever
REM  answers - so the older one keeps serving its old code and old page long after an
REM  update. Clear the port first: whatever is on 9090 is a previous run of this app, and
REM  starting fresh is what the person double-clicking this file is asking for.
set "OLD_SERVER_PID="
for /f "tokens=5" %%P in ('netstat -ano ^| findstr /r /c:":9090 .*LISTENING"') do (
    if not "%%P"=="0" (
        echo  Stopping the copy already on port 9090 ^(PID %%P^)...
        taskkill /PID %%P /F >nul 2>&1
        set "OLD_SERVER_PID=%%P"
    )
)
if defined OLD_SERVER_PID (
    REM  The socket takes a moment to come free; binding too soon fails.
    ping -n 3 127.0.0.1 >nul
    netstat -ano | findstr /r /c:":9090 .*LISTENING" >nul
    if not errorlevel 1 (
        echo.
        echo  ERROR: something is still holding port 9090 and would not stop.
        echo  It may belong to another user. Find it and stop it by hand:
        echo      netstat -ano ^| findstr :9090
        echo      taskkill /PID ^<pid^> /F
        echo.
        pause
        exit /b 1
    )
    echo  Port 9090 is clear, starting a fresh copy.
    echo.
)

"%PYTHON%" -c "import flask, requests" >nul 2>&1
if errorlevel 1 (
    echo  Installing dependencies, one moment...
    "%PYTHON%" -m pip install --quiet --disable-pip-version-check flask requests waitress
)

"%PYTHON%" serve.py

echo.
echo  Delivery Helper has stopped.
pause
