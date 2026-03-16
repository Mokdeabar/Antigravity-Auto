@echo off
chcp 65001 >nul 2>nul
title Supervisor AI - V65 Command Centre
cd /d "%~dp0"

REM ── Ensure npm global bin is in PATH ──
set "PATH=%APPDATA%\npm;%PATH%"

REM ══════════════════════════════════════════
REM  0. V64: Set PowerShell execution policy
REM     Gemini CLI needs RemoteSigned to run
REM     scripts. -Scope CurrentUser = no admin.
REM ══════════════════════════════════════════
%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
    "$current = Get-ExecutionPolicy -Scope CurrentUser; " ^
    "if ($current -ne 'RemoteSigned' -and $current -ne 'Unrestricted' -and $current -ne 'Bypass') { " ^
    "  Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser -Force; " ^
    "  Write-Host '  [OK] PowerShell execution policy set to RemoteSigned.' " ^
    "} else { " ^
    "  Write-Host '  [OK] PowerShell execution policy already OK.' " ^
    "}"

:START
cls
echo.
echo   ========================================
echo     Supervisor AI - V65 Command Centre
echo   ========================================
echo.

REM ══════════════════════════════════════════
REM  1. Check Python (120s timeout)
REM ══════════════════════════════════════════
python --version >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo   [!] Python not found. Installing via winget (120s timeout^)...
    %SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
        "$p = Start-Process -FilePath 'winget' -ArgumentList 'install','Python.Python.3.13','--accept-source-agreements','--accept-package-agreements' -PassThru -NoNewWindow; " ^
        "try { $p | Wait-Process -Timeout 120 } catch { $p | Stop-Process -Force; Write-Host '  [!] TIMEOUT: winget hung after 120s — killed.'; exit 1 }; " ^
        "exit $p.ExitCode"
    if %ERRORLEVEL% NEQ 0 (
        echo   [!] Python install failed or timed out.
        echo   [!] Install manually: https://www.python.org/downloads/
        echo   Press any key to retry...
        pause >nul
    ) else (
        echo   [OK] Python installed.
    )
    goto START
)
echo   [OK] Python found.

REM ══════════════════════════════════════════
REM  2. Check Node.js (120s timeout)
REM ══════════════════════════════════════════
node --version >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo   [!] Node.js not found. Installing via winget (120s timeout^)...
    %SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
        "$p = Start-Process -FilePath 'winget' -ArgumentList 'install','OpenJS.NodeJS.LTS','--accept-source-agreements','--accept-package-agreements' -PassThru -NoNewWindow; " ^
        "try { $p | Wait-Process -Timeout 120 } catch { $p | Stop-Process -Force; Write-Host '  [!] TIMEOUT: winget hung after 120s — killed.'; exit 1 }; " ^
        "exit $p.ExitCode"
    REM Refresh PATH so npm is available immediately
    set "PATH=%ProgramFiles%\nodejs;%APPDATA%\npm;%PATH%"
    if %ERRORLEVEL% NEQ 0 (
        echo   [!] Node.js install failed or timed out.
        echo   [!] Install manually: https://nodejs.org/
        echo   Press any key to retry...
        pause >nul
    ) else (
        echo   [OK] Node.js installed.
    )
    goto START
)
echo   [OK] Node.js found.

REM ══════════════════════════════════════════
REM  3. Check Gemini CLI (90s timeout)
REM ══════════════════════════════════════════
where gemini >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    if not exist "%APPDATA%\npm\gemini.cmd" (
        echo   [!] Gemini CLI not found. Installing (90s timeout^)...
        %SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
            "$p = Start-Process -FilePath 'npm' -ArgumentList 'install','-g','@google/gemini-cli' -PassThru -NoNewWindow; " ^
            "try { $p | Wait-Process -Timeout 90 } catch { $p | Stop-Process -Force; Write-Host '  [!] TIMEOUT: npm hung after 90s — killed.'; exit 1 }; " ^
            "exit $p.ExitCode"
        if %ERRORLEVEL% NEQ 0 (
            echo   [!] Gemini CLI install failed or timed out.
            echo   [!] Install manually: npm install -g @google/gemini-cli
            echo   Press any key to retry...
            pause >nul
            goto START
        )
        echo   [OK] Gemini CLI installed.
    )
)
echo   [OK] Gemini CLI found.

REM ══════════════════════════════════════════
REM  4. Check Docker CLI (180s timeout for install)
REM ══════════════════════════════════════════
docker --version >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo   [!] Docker not found. Installing Docker Desktop (180s timeout^)...
    echo.
    %SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
        "$p = Start-Process -FilePath 'winget' -ArgumentList 'install','Docker.DockerDesktop','--accept-source-agreements','--accept-package-agreements' -PassThru -NoNewWindow; " ^
        "try { $p | Wait-Process -Timeout 180 } catch { $p | Stop-Process -Force; Write-Host '  [!] TIMEOUT: winget hung after 180s — killed.'; exit 1 }; " ^
        "exit $p.ExitCode"
    echo.
    if %ERRORLEVEL% NEQ 0 (
        echo   [!] Docker install failed or timed out.
        echo   [!] Install manually: https://docs.docker.com/desktop/install/windows-install/
        echo   Press any key to retry...
        pause >nul
        goto START
    )
    echo   ============================================
    echo   [OK] Docker Desktop installed.
    echo   [!] You MUST restart your PC, then
    echo       double-click this script again.
    echo   ============================================
    pause
    exit /b
)
echo   [OK] Docker found.

REM ══════════════════════════════════════════
REM  5. WSL + Docker Service Prerequisites
REM ══════════════════════════════════════════
echo   [..] Checking WSL and Docker services...

REM ── Start WSL Service ──
%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
  "try { " ^
  "  foreach ($svc in @('WslService','LxssManager')) { " ^
  "    $s = Get-Service -Name $svc -ErrorAction SilentlyContinue; " ^
  "    if ($s -and $s.Status -ne 'Running') { " ^
  "      Set-Service -Name $svc -StartupType Automatic -ErrorAction SilentlyContinue; " ^
  "      Start-Service -Name $svc -ErrorAction SilentlyContinue; " ^
  "      Write-Host \"  [OK] Started $svc\" " ^
  "    } elseif ($s) { " ^
  "      Write-Host \"  [OK] $svc running.\" " ^
  "    } " ^
  "  } " ^
  "} catch { Write-Host '  [i] WSL service check skipped.' }"

REM ── Start HNS ──
%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
  "try { " ^
  "  $s = Get-Service -Name hns -ErrorAction SilentlyContinue; " ^
  "  if ($s -and $s.Status -ne 'Running') { " ^
  "    Set-Service -Name hns -StartupType Automatic -ErrorAction SilentlyContinue; " ^
  "    Start-Service -Name hns -ErrorAction SilentlyContinue; " ^
  "    Write-Host '  [OK] Started HNS.' " ^
  "  } elseif ($s) { " ^
  "    Write-Host '  [OK] HNS running.' " ^
  "  } " ^
  "} catch { Write-Host '  [i] HNS check skipped.' }"

REM ── Start Docker Desktop Service ──
%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe -NoProfile -ExecutionPolicy Bypass -Command ^
  "try { " ^
  "  $s = Get-Service -Name 'com.docker.service' -ErrorAction SilentlyContinue; " ^
  "  if ($s -and $s.Status -ne 'Running') { " ^
  "    Set-Service -Name 'com.docker.service' -StartupType Automatic -ErrorAction SilentlyContinue; " ^
  "    Start-Service -Name 'com.docker.service' -ErrorAction SilentlyContinue; " ^
  "    Write-Host '  [OK] Started Docker service.' " ^
  "  } elseif ($s) { " ^
  "    Write-Host '  [OK] Docker service running.' " ^
  "  } " ^
  "} catch { Write-Host '  [i] Docker service check skipped.' }"

REM ── Ensure Docker daemon is responsive ──
"%ProgramFiles%\Docker\Docker\resources\bin\docker.exe" info >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo   [!] Docker daemon not responding. Cleaning up background processes...
    
    REM 1. Cleanly shutdown WSL (often the primary blocker)
    %SystemRoot%\System32\wsl.exe --shutdown >nul 2>&1
    
    REM 2. Force kill the entire process tree of Docker Desktop and all its backends
    %SystemRoot%\System32\taskkill.exe /F /T /IM "Docker Desktop.exe" >nul 2>&1
    %SystemRoot%\System32\taskkill.exe /F /T /IM "com.docker.backend.exe" >nul 2>&1
    %SystemRoot%\System32\taskkill.exe /F /T /IM "com.docker.build.exe" >nul 2>&1
    %SystemRoot%\System32\taskkill.exe /F /T /IM "com.docker.proxy.exe" >nul 2>&1
    %SystemRoot%\System32\taskkill.exe /F /T /IM "vpnkit.exe" >nul 2>&1
    %SystemRoot%\System32\taskkill.exe /F /T /IM "wsl.exe" >nul 2>&1
    
    REM 2b. Secondary aggressive sweep using PowerShell
    %SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "Get-Process -Name 'Docker Desktop', 'com.docker.*', 'vpnkit', 'wsl' -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue" >nul 2>&1
    
    REM 3. Restart the core Windows Service
    %SystemRoot%\System32\sc.exe stop com.docker.service >nul 2>&1
    %SystemRoot%\System32\timeout.exe /t 2 /nobreak >nul 2>&1
    %SystemRoot%\System32\sc.exe start com.docker.service >nul 2>&1
    %SystemRoot%\System32\timeout.exe /t 2 /nobreak >nul 2>&1
    
    echo   [!] Starting Docker Desktop...
    start "" "C:\Program Files\Docker\Docker\Docker Desktop.exe" 2>nul
    
    echo   [!] Waiting 45 seconds for Docker to initialise ^(Attempt 1/3^)...
    %SystemRoot%\System32\timeout.exe /t 45 /nobreak >nul
    "%ProgramFiles%\Docker\Docker\resources\bin\docker.exe" info >nul 2>nul
    if %ERRORLEVEL% EQU 0 goto DOCKER_READY
    
    echo   [!] Waiting another 45 seconds for Docker to initialise ^(Attempt 2/3^)...
    %SystemRoot%\System32\timeout.exe /t 45 /nobreak >nul
    "%ProgramFiles%\Docker\Docker\resources\bin\docker.exe" info >nul 2>nul
    if %ERRORLEVEL% EQU 0 goto DOCKER_READY
    
    echo   [!] Waiting another 45 seconds for Docker to initialise ^(Attempt 3/3^)...
    %SystemRoot%\System32\timeout.exe /t 45 /nobreak >nul
    "%ProgramFiles%\Docker\Docker\resources\bin\docker.exe" info >nul 2>nul
    if %ERRORLEVEL% NEQ 0 (
        echo.
        echo   [!] Docker still not ready after 135 seconds. Check that:
        echo       - Virtualisation is ENABLED in BIOS
        echo       - Docker Desktop is fully loaded
        echo.
        echo   Press any key to retry...
        pause >nul
        goto START
    )
)

:DOCKER_READY
echo   [OK] Docker daemon running.

REM ══════════════════════════════════════════
REM  5b. Ollama (DEPRECATED in V64)
REM    V64: Ollama replaced by Gemini Lite
REM    Intelligence. This check is kept for
REM    informational purposes only.
REM ══════════════════════════════════════════
where ollama >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    echo   [i] Ollama found. ^(V64: No longer required — Gemini Lite replaces local LLM.^)
) else (
    echo   [i] Ollama not installed ^(V64: not required — Gemini Lite handles Q+A^).
)


REM ══════════════════════════════════════════
REM  6. Install Python dependencies
REM ══════════════════════════════════════════
echo   [OK] Checking Python packages...
python -m pip install -q --upgrade -r requirements.txt 2>nul
echo   [OK] Dependencies ready.
echo.
REM V42: Auto-skip upgrade check (runs silently, user can upgrade manually)
REM To upgrade manually: pip install --upgrade -r requirements.txt

REM ══════════════════════════════════════════
REM  7. Launch Command Centre
REM ══════════════════════════════════════════
echo.
echo   All checks passed! Launching V65 Command Centre...
echo.
set PYTHONIOENCODING=utf-8
python supervisor\launcher.py

echo.
echo   Command Centre stopped.
exit

REM ══════════════════════════════════════════
REM  Subroutine: Upgrade outdated pip packages
REM ══════════════════════════════════════════
:UPGRADE_PACKAGES
echo   Upgrading pip...
python -m pip install --upgrade pip 2>nul
echo   Upgrading packages and dependencies...
python -m pip install --upgrade --upgrade-strategy eager -r requirements.txt
echo.
python -m pip list --outdated 2>nul | findstr /v "^Package " | findstr /v "^---" >nul 2>nul
if %ERRORLEVEL% NEQ 0 (
    echo   [OK] All packages up to date.
) else (
    echo   [i] Some packages still show newer versions but cannot be safely
    echo       upgraded due to dependency constraints. This is normal.
)
goto :eof

