@echo off
chcp 65001 >nul 2>&1
setlocal EnableDelayedExpansion

:: ─────────────────────────────────────────────────────────────
:: Supervisor AI — Setup Checker & Interactive Auto-Installer
:: Runs all prerequisite checks, offers to install missing tools,
:: and writes results to check_results.json & check_results.js
:: ─────────────────────────────────────────────────────────────

set "RESULTS_FILE=%~dp0check_results.json"
echo { > "%RESULTS_FILE%"

echo.
echo =======================================================
echo   Supervisor AI - Environment Setup Check
echo =======================================================
echo.

:: ── Python ──
set "PYTHON_OK=false"
set "PYTHON_VER=not found"
for /f "tokens=*" %%v in ('python --version 2^>^&1') do (
    set "PYTHON_VER=%%v"
    if not "!PYTHON_VER!" == "not found" set "PYTHON_OK=true"
)
if "!PYTHON_OK!"=="false" (
    echo [Missing] Python 3.13 is missing.
    set /p "INSTALL_PY=  ^> Install Python automatically? [Y/n] "
    if /I "!INSTALL_PY!"=="Y" (
        echo   Running: winget install Python.Python.3.13 ...
        winget install Python.Python.3.13 --accept-source-agreements --accept-package-agreements
        if !ERRORLEVEL! EQU 0 (
            set "PYTHON_OK=true"
            set "PYTHON_VER=installed via winget (restart needed)"
            echo   [OK] Python installed successfully.
        ) else (
            echo   [Error] Failed to install Python. Please install manually later.
        )
    ) else if /I "!INSTALL_PY!"=="" (
        echo   Running: winget install Python.Python.3.13 ...
        winget install Python.Python.3.13 --accept-source-agreements --accept-package-agreements
        if !ERRORLEVEL! EQU 0 (
            set "PYTHON_OK=true"
            set "PYTHON_VER=installed via winget (restart needed)"
            echo   [OK] Python installed successfully.
        ) else (
            echo   [Error] Failed to install Python. Please install manually later.
        )
    )
) else (
    echo [OK] Python: !PYTHON_VER!
)
echo   "python": {"ok": !PYTHON_OK!, "version": "!PYTHON_VER!"}, >> "%RESULTS_FILE%"

:: ── pip ──
set "PIP_OK=false"
set "PIP_VER=not found"
for /f "tokens=1,2" %%a in ('pip --version 2^>^&1') do (
    if "%%a"=="pip" (
        set "PIP_VER=%%b"
        set "PIP_OK=true"
    )
)
if "!PIP_OK!"=="true" (
    echo [OK] pip: !PIP_VER!
)
echo   "pip": {"ok": !PIP_OK!, "version": "!PIP_VER!"}, >> "%RESULTS_FILE%"

:: ── Node.js ──
set "NODE_OK=false"
set "NODE_VER=not found"
for /f "tokens=*" %%v in ('node --version 2^>^&1') do (
    set "NODE_VER=%%v"
    set "NODE_OK=true"
)
if "!NODE_OK!"=="false" (
    echo [Missing] Node.js is missing.
    set /p "INSTALL_NODE=  ^> Install Node.js automatically? [Y/n] "
    if /I "!INSTALL_NODE!"=="Y" (
        echo   Running: winget install OpenJS.NodeJS.LTS ...
        winget install OpenJS.NodeJS.LTS --accept-source-agreements --accept-package-agreements
        if !ERRORLEVEL! EQU 0 (
            set "NODE_OK=true"
            set "NODE_VER=installed via winget (restart needed)"
            echo   [OK] Node.js installed successfully.
        ) else (
            echo   [Error] Failed to install Node.js. Please install manually later.
        )
    ) else if /I "!INSTALL_NODE!"=="" (
         echo   Running: winget install OpenJS.NodeJS.LTS ...
         winget install OpenJS.NodeJS.LTS --accept-source-agreements --accept-package-agreements
         if !ERRORLEVEL! EQU 0 (
             set "NODE_OK=true"
             set "NODE_VER=installed via winget (restart needed)"
             echo   [OK] Node.js installed successfully.
         ) else (
             echo   [Error] Failed to install Node.js. Please install manually later.
         )
    )
) else (
    echo [OK] Node.js: !NODE_VER!
)
echo   "node": {"ok": !NODE_OK!, "version": "!NODE_VER!"}, >> "%RESULTS_FILE%"

:: ── npm ──
set "NPM_OK=false"
set "NPM_VER=not found"
for /f "tokens=*" %%v in ('npm --version 2^>^&1') do (
    set "NPM_VER=%%v"
    set "NPM_OK=true"
)
if "!NPM_OK!"=="true" (
    echo [OK] npm: !NPM_VER!
)
echo   "npm": {"ok": !NPM_OK!, "version": "!NPM_VER!"}, >> "%RESULTS_FILE%"

:: ── Git ──
set "GIT_OK=false"
set "GIT_VER=not found"
for /f "tokens=*" %%v in ('git --version 2^>^&1') do (
    set "GIT_VER=%%v"
    set "GIT_OK=true"
)
if "!GIT_OK!"=="false" (
    echo [Missing] Git is missing.
    set /p "INSTALL_GIT=  ^> Install Git automatically? [Y/n] "
    if /I "!INSTALL_GIT!"=="Y" (
        echo   Running: winget install Git.Git ...
        winget install Git.Git --accept-source-agreements --accept-package-agreements
        if !ERRORLEVEL! EQU 0 (
            set "GIT_OK=true"
            set "GIT_VER=installed via winget (restart needed)"
            echo   [OK] Git installed successfully.
        ) else (
            echo   [Error] Failed to install Git. Please install manually later.
        )
    ) else if /I "!INSTALL_GIT!"=="" (
        echo   Running: winget install Git.Git ...
        winget install Git.Git --accept-source-agreements --accept-package-agreements
        if !ERRORLEVEL! EQU 0 (
            set "GIT_OK=true"
            set "GIT_VER=installed via winget (restart needed)"
            echo   [OK] Git installed successfully.
        ) else (
            echo   [Error] Failed to install Git. Please install manually later.
        )
    )
) else (
    echo [OK] Git: !GIT_VER!
)
echo   "git": {"ok": !GIT_OK!, "version": "!GIT_VER!"}, >> "%RESULTS_FILE%"

:: ── Docker ──
set "DOCKER_OK=false"
set "DOCKER_VER=not found"
for /f "tokens=*" %%v in ('docker --version 2^>^&1') do (
    set "DOCKER_VER=%%v"
    set "DOCKER_OK=true"
)
if "!DOCKER_OK!"=="false" (
    echo [Missing] Docker Desktop is missing. 
    echo   ^> Please install manually using the instructions in SETUP.html.
) else (
    echo [OK] Docker: !DOCKER_VER!
)
echo   "docker": {"ok": !DOCKER_OK!, "version": "!DOCKER_VER!"}, >> "%RESULTS_FILE%"

:: ── Docker Daemon ──
set "DOCKERD_OK=false"
docker info >nul 2>&1 && set "DOCKERD_OK=true"
if "!DOCKERD_OK!"=="false" (
    if "!DOCKER_OK!"=="true" (
        echo [Warning] Docker Desktop is installed, but the daemon is not running. Start Docker Desktop!
    )
) else (
    echo [OK] Docker Daemon running.
)
echo   "docker_daemon": {"ok": !DOCKERD_OK!}, >> "%RESULTS_FILE%"

:: ── WSL ──
set "WSL_OK=false"
set "WSL_VER=not found"
for /f "tokens=*" %%v in ('wsl --version 2^>^&1') do (
    if "!WSL_OK!"=="false" (
        set "WSL_VER=%%v"
        set "WSL_OK=true"
    )
)
if "!WSL_OK!"=="false" (
    echo [Missing] WSL2 is missing. 
    echo   ^> Please run PowerShell as Administrator and use: wsl --install
) else (
    echo [OK] WSL installed.
)
echo   "wsl": {"ok": !WSL_OK!, "version": "!WSL_VER!"}, >> "%RESULTS_FILE%"

:: ── Gemini CLI ──
set "GEMINI_OK=false"
set "GEMINI_VER=not found"
set "PATH=%APPDATA%
pm;%PATH%"
for /f "tokens=*" %%v in ('gemini --version 2^>^&1') do (
    set "GEMINI_VER=%%v"
    set "GEMINI_OK=true"
)
if "!GEMINI_OK!"=="false" (
    echo [Missing] Gemini CLI is missing.
    set /p "INSTALL_GEM=  ^> Install Gemini CLI automatically? [Y/n] "
    if /I "!INSTALL_GEM!"=="Y" (
        echo   Running: npm install -g @google/gemini-cli ...
        call npm install -g "@google/gemini-cli" >nul 2>&1
        if !ERRORLEVEL! EQU 0 (
            set "GEMINI_OK=true"
            set "GEMINI_VER=installed via npm"
            echo   [OK] Gemini CLI installed successfully.
        ) else (
            echo   [Error] Failed to install Gemini CLI. Ensure Node/npm is installed.
        )
    ) else if /I "!INSTALL_GEM!"=="" (
         echo   Running: npm install -g @google/gemini-cli ...
         call npm install -g "@google/gemini-cli" >nul 2>&1
         if !ERRORLEVEL! EQU 0 (
             set "GEMINI_OK=true"
             set "GEMINI_VER=installed via npm"
             echo   [OK] Gemini CLI installed successfully.
         ) else (
             echo   [Error] Failed to install Gemini CLI. Ensure Node/npm is installed.
         )
    )
) else (
    echo [OK] Gemini CLI: !GEMINI_VER!
)
echo   "gemini": {"ok": !GEMINI_OK!, "version": "!GEMINI_VER!"}, >> "%RESULTS_FILE%"

:: ── Ollama ──
set "OLLAMA_OK=false"
set "OLLAMA_VER=not found"
for /f "tokens=*" %%v in ('ollama --version 2^>^&1') do (
    set "OLLAMA_VER=%%v"
    set "OLLAMA_OK=true"
)
if "!OLLAMA_OK!"=="true" (
    echo [OK] Ollama: !OLLAMA_VER!
)
echo   "ollama": {"ok": !OLLAMA_OK!, "version": "!OLLAMA_VER!"}, >> "%RESULTS_FILE%"

:: ── Sandbox Image ──
set "SANDBOX_OK=false"
docker images supervisor-sandbox --format "{{.Repository}}" 2>nul | findstr /i "supervisor-sandbox" >nul 2>&1 && set "SANDBOX_OK=true"
if "!SANDBOX_OK!"=="false" (
    if "!DOCKERD_OK!"=="true" (
        echo [Missing] Sandbox Docker image is not built.
        set /p "INSTALL_SND=  ^> Build supervisor-sandbox docker image now? (3-10m) [Y/n] "
        if /I "!INSTALL_SND!"=="Y" (
            echo   Building sandbox image... this may take a few minutes.
            docker build -t supervisor-sandbox -f "%~dp0supervisor\Dockerfile.sandbox" "%~dp0supervisor"
            if !ERRORLEVEL! EQU 0 (
                set "SANDBOX_OK=true"
                echo   [OK] Sandbox image built successfully.
            ) else (
                echo   [Error] Sandbox build failed. Check docker logs.
            )
        ) else if /I "!INSTALL_SND!"=="" (
            echo   Building sandbox image... this may take a few minutes.
            docker build -t supervisor-sandbox -f "%~dp0supervisor\Dockerfile.sandbox" "%~dp0supervisor"
            if !ERRORLEVEL! EQU 0 (
                set "SANDBOX_OK=true"
                echo   [OK] Sandbox image built successfully.
            ) else (
                echo   [Error] Sandbox build failed. Check docker logs.
            )
        )
    ) else (
        echo [Missing] Sandbox Docker image ^(Requires Docker Daemon to build^)
    )
) else (
    echo [OK] Sandbox Image built.
)
echo   "sandbox_image": {"ok": !SANDBOX_OK!}, >> "%RESULTS_FILE%"

:: ── Python Packages ──
set "PACKAGES_OK=false"
python -c "import fastapi, uvicorn, aiohttp, psutil; print('ok')" 2>nul | findstr /i "ok" >nul 2>&1 && set "PACKAGES_OK=true"
if "!PACKAGES_OK!"=="false" (
    if "!PYTHON_OK!"=="true" (
        echo [Missing] Python dependencies not installed.
        set /p "INSTALL_PKG=  ^> Install required Python packages? [Y/n] "
        if /I "!INSTALL_PKG!"=="Y" (
            echo   Running: pip install --upgrade -r requirements.txt ...
            pip install --upgrade -r "%~dp0requirements.txt"
            if !ERRORLEVEL! EQU 0 (
                set "PACKAGES_OK=true"
                echo   [OK] Python packages installed successfully.
            ) else (
                echo   [Error] Package install failed.
            )
        ) else if /I "!INSTALL_PKG!"=="" (
            echo   Running: pip install --upgrade -r requirements.txt ...
            pip install --upgrade -r "%~dp0requirements.txt"
            if !ERRORLEVEL! EQU 0 (
                set "PACKAGES_OK=true"
                echo   [OK] Python packages installed successfully.
            ) else (
                echo   [Error] Package install failed.
            )
        )
    ) else (
        echo [Missing] Python Packages ^(Requires Python to be installed first^)
    )
) else (
    echo [OK] Python Packages installed.
)
echo   "python_packages": {"ok": !PACKAGES_OK!}, >> "%RESULTS_FILE%"

:: ── Virtualisation (Task Manager method) ──
set "VIRT_OK=unknown"
echo   "virtualisation": {"ok": "!VIRT_OK!"}, >> "%RESULTS_FILE%"

:: ── Timestamp ──
echo   "checked_at": "%date% %time%" >> "%RESULTS_FILE%"
echo } ^>^> "%RESULTS_FILE%"

:: ── Also write a .js file so SETUP.html can auto-load via script tag ──
:: (fetch() is blocked on file:// due to CORS, but script tags work)
set "JS_FILE=%~dp0check_results.js"
echo window._checkResults = { > "%JS_FILE%"
echo   "python": {"ok": !PYTHON_OK!, "version": "!PYTHON_VER!"}, >> "%JS_FILE%"
echo   "pip": {"ok": !PIP_OK!, "version": "!PIP_VER!"}, >> "%JS_FILE%"
echo   "node": {"ok": !NODE_OK!, "version": "!NODE_VER!"}, >> "%JS_FILE%"
echo   "npm": {"ok": !NPM_OK!, "version": "!NPM_VER!"}, >> "%JS_FILE%"
echo   "git": {"ok": !GIT_OK!, "version": "!GIT_VER!"}, >> "%JS_FILE%"
echo   "docker": {"ok": !DOCKER_OK!, "version": "!DOCKER_VER!"}, >> "%JS_FILE%"
echo   "docker_daemon": {"ok": !DOCKERD_OK!}, >> "%JS_FILE%"
echo   "wsl": {"ok": !WSL_OK!, "version": "!WSL_VER!"}, >> "%JS_FILE%"
echo   "gemini": {"ok": !GEMINI_OK!, "version": "!GEMINI_VER!"}, >> "%JS_FILE%"
echo   "ollama": {"ok": !OLLAMA_OK!, "version": "!OLLAMA_VER!"}, >> "%JS_FILE%"
echo   "sandbox_image": {"ok": !SANDBOX_OK!}, >> "%JS_FILE%"
echo   "python_packages": {"ok": !PACKAGES_OK!}, >> "%JS_FILE%"
echo   "checked_at": "%date% %time%" >> "%JS_FILE%"
echo }; >> "%JS_FILE%"

echo.
echo =======================================================
echo   Setup Check Complete - Results saved to:
echo   %RESULTS_FILE%
echo =======================================================
echo.
:: Only pause if run directly (not from SETUP.bat)
echo %cmdcmdline% | findstr /i /c:"/c" >nul && (pause) || (timeout /t 5 >nul)
