@echo off
chcp 65001 >nul 2>&1
title Supervisor AI — Setup Wizard
echo.
echo   ══════════════════════════════════════════════════
echo   ⚡  Supervisor AI — Setup Wizard
echo   ══════════════════════════════════════════════════
echo.
echo   Running system checks...
echo.

:: Run the checker (same directory)
call "%~dp0check_setup.bat" >nul 2>&1

echo.
echo   ✓ Checks complete. Opening Setup Wizard...
echo.

:: Open the HTML wizard in the default browser
start "" "%~dp0SETUP.html"

echo   The wizard is open in your browser.
echo   You can close this window.
echo.
pause
