@echo off
setlocal enabledelayedexpansion
title Supervisor V14 Emergency Rollback

echo [Antigravity Supervisor] Emergency Rollback Initiated...
set DIR=%~dp0
set BACKUP_DIR=%DIR%\supervisor\_evolution_backups

if not exist "%BACKUP_DIR%" (
    echo [ERROR] No backup directory found at %BACKUP_DIR%.
    pause
    exit /b 1
)

echo Finding latest backup...
for /f "delims=" %%I in ('dir /b /a-d /o-d /tw "%BACKUP_DIR%\*" 2^>nul') do (
    set LATEST_FILE=%%I
    goto :Found
)

:Found
if "%LATEST_FILE%"=="" (
    echo [ERROR] No backups exist in the backup directory.
    pause
    exit /b 1
)

echo Detected latest backup: %LATEST_FILE%
set "BASE_NAME=%LATEST_FILE:~0,-20%"

echo Restoring %BASE_NAME% from %BACKUP_DIR%\%LATEST_FILE%...
copy /y "%BACKUP_DIR%\%LATEST_FILE%" "%DIR%\supervisor\%BASE_NAME%"

if %ERRORLEVEL% equ 0 (
    echo.
    echo [SUCCESS] Rollback complete. Supervisor restored.
) else (
    echo.
    echo [ERROR] Failed to overwrite %BASE_NAME%.
)

pause
