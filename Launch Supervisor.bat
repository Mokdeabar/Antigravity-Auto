@echo off
:: Fix broken PATH environments missing Windows binaries
set PATH=%SystemRoot%\System32;%SystemRoot%;%PATH%
chcp 65001 >nul

:: ═══════════════════════════════════════════════════════════════
::  SUPERVISOR AI — IMMORTAL DAEMON V7.2
::
::  A process-level immortality engine. This script makes the
::  supervisor literally unkillable short of closing the terminal.
::
::  INTELLIGENCE FEATURES:
::    • Exponential backoff: 5s → 10s → 20s → 40s → 80s → cap 120s
::    • Evolution fast-path: instant reboot on code self-mutation
::    • Crash fingerprinting: tracks consecutive vs. total crashes
::    • Health auto-reset: successful runs clear all crash memory
::    • Infinite recovery: NEVER asks for human input. Ever.
::    • Timestamp logging: every event is timestamped for forensics
::
::  TO STOP: Ctrl+C or close this terminal. Nothing else will.
:: ═══════════════════════════════════════════════════════════════

:: ── Crash Intelligence State ─────────────────────────────────
set CRASH_COUNT=0
set TOTAL_CRASHES=0
set BACKOFF=5
set MAX_BACKOFF=120

echo.
echo  ╔═══════════════════════════════════════════════════════╗
echo  ║   SUPERVISOR AI — IMMORTAL DAEMON V7.2               ║
echo  ║   Background Mode: FULLY AUTONOMOUS                  ║
echo  ║   Recovery: EXPONENTIAL BACKOFF                       ║
echo  ║   Termination: Ctrl+C ONLY                           ║
echo  ╚═══════════════════════════════════════════════════════╝
echo.
echo  [%date% %time%] Daemon started.
echo.

:SUPERVISOR_LOOP
cd /d "%~dp0"
echo  [%date% %time%] ▶ Starting supervisor (crashes: %CRASH_COUNT%, total: %TOTAL_CRASHES%, backoff: %BACKOFF%s)
python -m supervisor

:: Capture the exit code.
set EXIT_CODE=%errorlevel%
echo  [%date% %time%] ◼ Supervisor exited with code %EXIT_CODE%

:: ═══════════════════════════════════════════════════════════════
::  EXIT CODE 42: Self-Evolution / Gemini-Requested Retry
::
::  The supervisor mutated its own code or Gemini triaged a
::  transient error. This is the FAST PATH — minimal cooldown,
::  all crash state RESET because the code itself changed.
:: ═══════════════════════════════════════════════════════════════
if %EXIT_CODE% EQU 42 (
    echo.
    echo  ╔═══════════════════════════════════════════════════════╗
    echo  ║  🧬 EVOLUTION DETECTED — Code mutated.               ║
    echo  ║  Press N within 5 seconds to CANCEL reboot.          ║
    echo  ╚═══════════════════════════════════════════════════════╝
    echo.
    set CRASH_COUNT=0
    set BACKOFF=5
    choice /C YN /T 5 /D Y /M "  Continue with reboot? [Y=yes / N=cancel, restore backup]"
    if errorlevel 2 (
        echo.
        echo  [%date% %time%] ⛔ User cancelled reboot. Attempting to restore last backup...
        echo.
        :: Find and restore the newest .bak file for main.py
        set "NEWEST_BAK="
        for /f "delims=" %%F in ('dir /b /o-d "supervisor\_evolution_backups\*.bak" 2^>nul') do (
            if not defined NEWEST_BAK set "NEWEST_BAK=%%F"
        )
        if defined NEWEST_BAK (
            copy /Y "supervisor\_evolution_backups\%NEWEST_BAK%" "supervisor\main.py" >nul
            echo  [%date% %time%] ✅ Restored from: _evolution_backups\%NEWEST_BAK%
        ) else (
            echo  [%date% %time%] ⚠ No backup files found in _evolution_backups\
        )
        echo.
        echo  Reboot cancelled. Starting fresh in 3 seconds...
        timeout /t 3 /nobreak >nul
    )
    goto SUPERVISOR_LOOP
)

:: ═══════════════════════════════════════════════════════════════
::  EXIT CODE 0: Graceful Completion
::
::  Task finished cleanly. Reset ALL crash intelligence and
::  restart to pick up any new saved sessions.
:: ═══════════════════════════════════════════════════════════════
if %EXIT_CODE% EQU 0 (
    echo.
    echo  [%date% %time%] ✅ Task completed cleanly. Health: PERFECT
    echo  Restarting in 10s to pick up new sessions...
    echo.
    set CRASH_COUNT=0
    set TOTAL_CRASHES=0
    set BACKOFF=5
    timeout /t 10 /nobreak >nul
    goto SUPERVISOR_LOOP
)

:: ═══════════════════════════════════════════════════════════════
::  ANY OTHER EXIT CODE: Unexpected Crash
::
::  Exponential backoff: 5 → 10 → 20 → 40 → 80 → 120s (cap)
::  Tracks consecutive crashes and total lifetime crashes.
::  NEVER blocks for human input. Immortal by design.
:: ═══════════════════════════════════════════════════════════════
set /a CRASH_COUNT+=1
set /a TOTAL_CRASHES+=1

echo.
echo  ╔═══════════════════════════════════════════════════════╗
echo  ║  ⚠ CRASH #%CRASH_COUNT% (lifetime: %TOTAL_CRASHES%)                            ║
echo  ║  Exit code: %EXIT_CODE%                                        ║
echo  ║  Backoff: %BACKOFF% seconds                                  ║
echo  ╚═══════════════════════════════════════════════════════╝
echo.
echo  [%date% %time%] Cooling down %BACKOFF%s before retry...

timeout /t %BACKOFF% /nobreak >nul

:: ── Exponential backoff: double the wait, cap at MAX_BACKOFF ──
set /a BACKOFF=%BACKOFF%*2
if %BACKOFF% GTR %MAX_BACKOFF% set BACKOFF=%MAX_BACKOFF%

goto SUPERVISOR_LOOP
