@echo off
color 0a
title Trading Bot API & Dashboard

:: Change to the directory where the batch file is located
cd /d "%~dp0"

echo =======================================================
echo    Trading Bot - API & Dashboard Launcher
echo =======================================================
echo.

:: 1. Start the API Server in a new window so it runs in the background
echo Starting FastAPI Backend (Port 8000)...
start "Trading Bot API" cmd /k "C:\Users\hp\AppData\Local\Programs\Python\Python313\python.exe -m uvicorn api.server:app --port 8000"

:: Wait 3 seconds for the API to boot
timeout /t 3 /nobreak >nul

:: 2. Start the Next.js dashboard in the current window
echo Starting Next.js Dashboard (Port 3000)...
cd dashboard
npm run dev
