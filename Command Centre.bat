@echo off
title Supervisor AI - V35 Command Centre
cd /d "%~dp0"

:START
cls
echo.
echo   ========================================
echo     Supervisor AI - V35 Command Centre
echo   ========================================
echo.

REM ══════════════════════════════════════════
REM  1. Check Python (use --version, not where)
REM ══════════════════════════════════════════
python --version >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo   [!] Python not found. Installing via winget...
    winget install Python.Python.3.13 --accept-source-agreements --accept-package-agreements
    echo.
    echo   [!] Python installed. Press any key to retry...
    pause >nul
    goto START
)
echo   [OK] Python found.

REM ══════════════════════════════════════════
REM  2. Check Docker CLI
REM ══════════════════════════════════════════
docker --version >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo   [!] Docker not found. Installing Docker Desktop...
    echo.
    winget install Docker.DockerDesktop --accept-source-agreements --accept-package-agreements
    echo.
    echo   ============================================
    echo   [!] Docker Desktop installed.
    echo   [!] You MUST restart your PC, then:
    echo       1. Open Docker Desktop from Start Menu
    echo       2. Wait for it to finish starting
    echo       3. Double-click this script again
    echo   ============================================
    pause
    exit /b
)
echo   [OK] Docker found.

REM ── Ensure Docker daemon is running ──
docker info >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo   [!] Docker is installed but not running.
    echo   [!] Starting Docker Desktop...
    start "" "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    echo   [!] Waiting 30 seconds for Docker to start...
    timeout /t 30 /nobreak >nul
    docker info >nul 2>nul
    if %ERRORLEVEL% NEQ 0 (
        echo.
        echo   [!] Docker still not ready. Press any key to retry...
        pause >nul
        goto START
    )
)
echo   [OK] Docker daemon running.

REM ══════════════════════════════════════════
REM  3. Install Python dependencies
REM ══════════════════════════════════════════
echo   [OK] Checking Python packages...
python -m pip install -q --upgrade -r requirements.txt 2>nul
echo   [OK] Dependencies ready.
echo.
echo   Checking for package updates...
python -m pip list --outdated 2>nul | findstr /v "^Package " | findstr /v "^---" >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    echo   [i] Upgradable packages found:
    python -m pip list --outdated 2>nul
    echo.
    set /p UPGRADE="  Upgrade all? (y/N): "
    if /i "%UPGRADE%"=="y" (
        python -m pip install -q --upgrade -r requirements.txt 2>nul
        echo   [OK] Upgraded.
    )
) else (
    echo   [OK] All packages up to date.
)

REM ══════════════════════════════════════════
REM  4. Launch Command Centre
REM ══════════════════════════════════════════
echo.
echo   Launching V35 Command Centre...
echo.
python supervisor\launcher.py

echo.
echo   Command Centre stopped. Press any key to restart...
pause >nul
goto START
