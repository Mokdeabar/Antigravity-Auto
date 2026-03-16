# ═══════════════════════════════════════════════════════════════
# Supervisor AI : Interactive Setup Installer
# ═══════════════════════════════════════════════════════════════
# Right-click this file → "Run with PowerShell"
# Or: open PowerShell as Admin → type: .\INSTALL.ps1
# ═══════════════════════════════════════════════════════════════

$ErrorActionPreference = "SilentlyContinue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$Host.UI.RawUI.WindowTitle = "Supervisor AI : Setup Installer"

# ── Colour helpers ──
function Write-Header($text) { Write-Host "`n  $text" -ForegroundColor Cyan }
function Write-OK($text)     { Write-Host "  ✓ $text" -ForegroundColor Green }
function Write-Warn($text)   { Write-Host "  ⚠ $text" -ForegroundColor Yellow }
function Write-Fail($text)   { Write-Host "  ✗ $text" -ForegroundColor Red }
function Write-Info($text)   { Write-Host "  · $text" -ForegroundColor Gray }
function Write-Step($n,$text){ Write-Host "`n  [$n] $text" -ForegroundColor Magenta }

function Ask-YesNo($prompt, $default = "Y") {
    $hint = if ($default -eq "Y") { "[Y/n]" } else { "[y/N]" }
    $ans = Read-Host "  $prompt $hint"
    if ([string]::IsNullOrWhiteSpace($ans)) { $ans = $default }
    return $ans.Trim().ToUpper() -eq "Y"
}

function Pause-Step {
    Write-Host ""
    Read-Host "  Press ENTER to continue"
}

# Admin check
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)

# ═══════════════════════════════════════════════════════════════
#  WELCOME
# ═══════════════════════════════════════════════════════════════

Clear-Host
Write-Host ""
Write-Host "  ══════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host "  ⚡  Supervisor AI : Interactive Setup Installer" -ForegroundColor Cyan
Write-Host "  ══════════════════════════════════════════════════" -ForegroundColor Cyan
Write-Host ""
Write-Host "  This installer will check your system and install" -ForegroundColor White
Write-Host "  everything you need to run Supervisor AI." -ForegroundColor White
Write-Host ""
Write-Host "  For each step, you can:" -ForegroundColor Gray
Write-Host "    • Press ENTER to accept the default (shown in [brackets])" -ForegroundColor Gray
Write-Host "    • Type Y or N to choose" -ForegroundColor Gray
Write-Host "    • Press Ctrl+C at any time to quit" -ForegroundColor Gray
Write-Host ""

if (-not $isAdmin) {
    Write-Warn "You're NOT running as Administrator."
    Write-Warn "Some installs (WSL, Docker) need admin rights."
    Write-Host ""
    Write-Host "  ┌──────────────────────────────────────────────────┐" -ForegroundColor Yellow
    Write-Host "  │  HOW TO RUN AS ADMINISTRATOR:                    │" -ForegroundColor Yellow
    Write-Host "  │                                                  │" -ForegroundColor Yellow
    Write-Host "  │  1. Press the Windows key on your keyboard       │" -ForegroundColor Yellow
    Write-Host "  │  2. Type: PowerShell                             │" -ForegroundColor Yellow
    Write-Host "  │  3. Right-click 'Windows PowerShell'             │" -ForegroundColor Yellow
    Write-Host "  │  4. Click 'Run as administrator'                 │" -ForegroundColor Yellow
    Write-Host "  │  5. Click 'Yes' on the popup                     │" -ForegroundColor Yellow
    Write-Host "  │  6. Type: cd '$PSScriptRoot'                     │" -ForegroundColor Yellow
    Write-Host "  │  7. Type: .\INSTALL.ps1                          │" -ForegroundColor Yellow
    Write-Host "  └──────────────────────────────────────────────────┘" -ForegroundColor Yellow
    Write-Host ""
    if (-not (Ask-YesNo "Continue anyway without admin? (some steps may fail)")) {
        exit 0
    }
}

# ═══════════════════════════════════════════════════════════════
#  TERMINAL BASICS (Dummy's Guide)
# ═══════════════════════════════════════════════════════════════

Write-Host ""
Write-Host "  ┌──────────────────────────────────────────────────┐" -ForegroundColor DarkCyan
Write-Host "  │       📖  QUICK GUIDE: Using This Window         │" -ForegroundColor DarkCyan
Write-Host "  │                                                  │" -ForegroundColor DarkCyan
Write-Host "  │  This blue/black window is called a 'terminal'.  │" -ForegroundColor DarkCyan
Write-Host "  │  Here's what you need to know:                   │" -ForegroundColor DarkCyan
Write-Host "  │                                                  │" -ForegroundColor DarkCyan
Write-Host "  │  • Text scrolls UP as new lines appear           │" -ForegroundColor DarkCyan
Write-Host "  │  • When it asks a question, type your answer     │" -ForegroundColor DarkCyan
Write-Host "  │    and press ENTER                               │" -ForegroundColor DarkCyan
Write-Host "  │  • To PASTE text: right-click anywhere in the    │" -ForegroundColor DarkCyan
Write-Host "  │    window (or press Ctrl+V)                      │" -ForegroundColor DarkCyan
Write-Host "  │  • If it seems stuck, press ENTER : it may be    │" -ForegroundColor DarkCyan
Write-Host "  │    waiting for input                             │" -ForegroundColor DarkCyan
Write-Host "  │  • To cancel/stop: press Ctrl+C                  │" -ForegroundColor DarkCyan
Write-Host "  │  • DON'T close this window until it's done!      │" -ForegroundColor DarkCyan
Write-Host "  │                                                  │" -ForegroundColor DarkCyan
Write-Host "  │  Some steps will open OTHER windows (browsers,   │" -ForegroundColor DarkCyan
Write-Host "  │  installers). Complete those, then come back      │" -ForegroundColor DarkCyan
Write-Host "  │  here and press ENTER.                           │" -ForegroundColor DarkCyan
Write-Host "  └──────────────────────────────────────────────────┘" -ForegroundColor DarkCyan

Pause-Step

# ═══════════════════════════════════════════════════════════════
#  STEP 1: System Scan
# ═══════════════════════════════════════════════════════════════

Write-Step 1 "Scanning your system..."
Write-Host ""

$checks = @{}

# Python
$py = & python --version 2>&1
if ($LASTEXITCODE -eq 0 -and $py -match "Python") {
    Write-OK "Python: $py"
    $checks["python"] = $true
} else {
    Write-Fail "Python: not found"
    $checks["python"] = $false
}

# pip
$pip = & pip --version 2>&1
if ($LASTEXITCODE -eq 0 -and $pip -match "pip") {
    $pipVer = ($pip -split " ")[1]
    Write-OK "pip: $pipVer"
    $checks["pip"] = $true
} else {
    Write-Fail "pip: not found"
    $checks["pip"] = $false
}

# Node
$node = & node --version 2>&1
if ($LASTEXITCODE -eq 0 -and $node -match "v") {
    Write-OK "Node.js: $node"
    $checks["node"] = $true
} else {
    Write-Fail "Node.js: not found"
    $checks["node"] = $false
}

# npm
$npm = & npm --version 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-OK "npm: $npm"
    $checks["npm"] = $true
} else {
    Write-Fail "npm: not found"
    $checks["npm"] = $false
}

# Git
$git = & git --version 2>&1
if ($LASTEXITCODE -eq 0 -and $git -match "git version") {
    Write-OK "Git: $git"
    $checks["git"] = $true
} else {
    Write-Fail "Git: not found"
    $checks["git"] = $false
}

# Docker
$docker = & docker --version 2>&1
if ($LASTEXITCODE -eq 0 -and $docker -match "Docker") {
    Write-OK "Docker: $docker"
    $checks["docker"] = $true
} else {
    Write-Fail "Docker: not found"
    $checks["docker"] = $false
}

# Docker daemon
$dockerInfo = & docker info 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-OK "Docker daemon: running"
    $checks["docker_daemon"] = $true
} else {
    Write-Warn "Docker daemon: not running (start Docker Desktop)"
    $checks["docker_daemon"] = $false
}

# WSL
$wsl = & wsl --version 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-OK "WSL: installed"
    $checks["wsl"] = $true
} else {
    Write-Fail "WSL: not found"
    $checks["wsl"] = $false
}

# Gemini CLI
$env:PATH = "$env:APPDATA\npm;$env:PATH"
$gemini = & gemini --version 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-OK "Gemini CLI: $gemini"
    $checks["gemini"] = $true
} else {
    Write-Fail "Gemini CLI: not found"
    $checks["gemini"] = $false
}

# Ollama
$ollama = & ollama --version 2>&1
if ($LASTEXITCODE -eq 0) {
    Write-OK "Ollama: $ollama"
    $checks["ollama"] = $true
} else {
    Write-Info "Ollama: not found (optional)"
    $checks["ollama"] = $false
}

# Sandbox image
$sandbox = & docker images supervisor-sandbox --format "{{.Repository}}" 2>&1
if ($sandbox -match "supervisor-sandbox") {
    Write-OK "Sandbox image: built"
    $checks["sandbox"] = $true
} else {
    Write-Fail "Sandbox image: not built"
    $checks["sandbox"] = $false
}

# Python packages
$pkgs = & python -c "import fastapi, uvicorn, aiohttp, psutil; print('ok')" 2>&1
if ($pkgs -match "ok") {
    Write-OK "Python packages: all installed"
    $checks["packages"] = $true
} else {
    Write-Fail "Python packages: missing"
    $checks["packages"] = $false
}

# Count
$passed = ($checks.Values | Where-Object { $_ -eq $true }).Count
$total  = $checks.Count
Write-Host ""
Write-Host "  Result: $passed / $total checks passed" -ForegroundColor $(if ($passed -eq $total) { "Green" } else { "Yellow" })

if ($passed -eq $total) {
    Write-Host ""
    Write-OK "Everything is already installed! You're ready to go."
    Write-Host ""
    if (Ask-YesNo "Launch Command Centre now?") {
        Start-Process "$PSScriptRoot\Command Centre.bat"
    }
    exit 0
}

Pause-Step

# ═══════════════════════════════════════════════════════════════
#  STEP 2: WSL2
# ═══════════════════════════════════════════════════════════════

if (-not $checks["wsl"]) {
    Write-Step 2 "Install WSL2 (Windows Subsystem for Linux)"
    Write-Info "WSL2 is required by Docker Desktop."
    Write-Info "This will download and enable WSL2 on your system."
    Write-Host ""

    if (-not $isAdmin) {
        Write-Warn "WSL install requires Administrator privileges."
        Write-Host ""
        Write-Host "  ┌──────────────────────────────────────────────────┐" -ForegroundColor Yellow
        Write-Host "  │  WHAT TO DO:                                     │" -ForegroundColor Yellow
        Write-Host "  │                                                  │" -ForegroundColor Yellow
        Write-Host "  │  1. Press the Windows key                        │" -ForegroundColor Yellow
        Write-Host "  │  2. Type: PowerShell                             │" -ForegroundColor Yellow
        Write-Host "  │  3. Right-click it → 'Run as administrator'     │" -ForegroundColor Yellow
        Write-Host "  │  4. In that window, type:                        │" -ForegroundColor Yellow
        Write-Host "  │     wsl --install                                │" -ForegroundColor Yellow
        Write-Host "  │  5. Press ENTER and wait for it to finish        │" -ForegroundColor Yellow
        Write-Host "  │  6. Restart your PC when it asks                 │" -ForegroundColor Yellow
        Write-Host "  │  7. After restart, run this installer again      │" -ForegroundColor Yellow
        Write-Host "  └──────────────────────────────────────────────────┘" -ForegroundColor Yellow
        Pause-Step
    } else {
        if (Ask-YesNo "Install WSL2 now?") {
            Write-Info "Running: wsl --install"
            Write-Info "(This may take a few minutes...)"
            & wsl --install
            Write-Host ""
            Write-Warn "You may need to RESTART your PC after this."
            Write-Warn "After restarting, run this installer again."
            Pause-Step
        }
    }
} else {
    Write-Step 2 "WSL2"
    Write-OK "Already installed : skipping."
}

# ═══════════════════════════════════════════════════════════════
#  STEP 3: Git
# ═══════════════════════════════════════════════════════════════

if (-not $checks["git"]) {
    Write-Step 3 "Install Git"
    Write-Info "Git is used for version control and code checkpoints."
    Write-Host ""

    if (Ask-YesNo "Install Git automatically via winget?") {
        Write-Info "Running: winget install Git.Git"
        Write-Info "(An installer window may appear : accept defaults)"
        & winget install Git.Git --accept-source-agreements --accept-package-agreements
        Write-Host ""
        Write-OK "Git installed. You may need to restart this window."
        Write-Info "After install, close and re-open PowerShell for git to work."
    } else {
        Write-Host ""
        Write-Host "  ┌──────────────────────────────────────────────────┐" -ForegroundColor Yellow
        Write-Host "  │  MANUAL INSTALL:                                 │" -ForegroundColor Yellow
        Write-Host "  │                                                  │" -ForegroundColor Yellow
        Write-Host "  │  1. Open your web browser                        │" -ForegroundColor Yellow
        Write-Host "  │  2. Go to: https://git-scm.com/downloads/win    │" -ForegroundColor Yellow
        Write-Host "  │  3. Download the installer                       │" -ForegroundColor Yellow
        Write-Host "  │  4. Run it : click Next through every screen     │" -ForegroundColor Yellow
        Write-Host "  │  5. Come back here and press ENTER               │" -ForegroundColor Yellow
        Write-Host "  └──────────────────────────────────────────────────┘" -ForegroundColor Yellow
        if (Ask-YesNo "Open the download page in your browser?") {
            Start-Process "https://git-scm.com/downloads/win"
        }
        Pause-Step
    }
} else {
    Write-Step 3 "Git"
    Write-OK "Already installed : skipping."
}

# ═══════════════════════════════════════════════════════════════
#  STEP 4: Python
# ═══════════════════════════════════════════════════════════════

if (-not $checks["python"]) {
    Write-Step 4 "Install Python 3.13"
    Write-Info "Python is the core runtime for Supervisor AI."
    Write-Host ""
    Write-Host "  ┌──────────────────────────────────────────────────┐" -ForegroundColor Red
    Write-Host "  │  🚨 CRITICAL: When the installer opens:          │" -ForegroundColor Red
    Write-Host "  │                                                  │" -ForegroundColor Red
    Write-Host "  │  CHECK THE BOX: 'Add python.exe to PATH'        │" -ForegroundColor Red
    Write-Host "  │                                                  │" -ForegroundColor Red
    Write-Host "  │  It's at the BOTTOM of the first screen.         │" -ForegroundColor Red
    Write-Host "  │  If you miss this, NOTHING will work later!      │" -ForegroundColor Red
    Write-Host "  └──────────────────────────────────────────────────┘" -ForegroundColor Red
    Write-Host ""

    if (Ask-YesNo "Install Python automatically via winget?") {
        Write-Info "Running: winget install Python.Python.3.13"
        & winget install Python.Python.3.13 --accept-source-agreements --accept-package-agreements
        Write-Host ""
        Write-OK "Python installed."
        Write-Warn "Close and re-open this window for python to be available."
    } else {
        if (Ask-YesNo "Open python.org in your browser?") {
            Start-Process "https://www.python.org/downloads/"
        }
        Write-Host ""
        Write-Host "  Install Python, then come back and press ENTER." -ForegroundColor Gray
        Pause-Step
    }
} else {
    Write-Step 4 "Python"
    Write-OK "Already installed : skipping."
}

# ═══════════════════════════════════════════════════════════════
#  STEP 5: Node.js
# ═══════════════════════════════════════════════════════════════

if (-not $checks["node"]) {
    Write-Step 5 "Install Node.js"
    Write-Info "Node.js is needed for the Gemini CLI."
    Write-Host ""

    if (Ask-YesNo "Install Node.js automatically via winget?") {
        Write-Info "Running: winget install OpenJS.NodeJS.LTS"
        & winget install OpenJS.NodeJS.LTS --accept-source-agreements --accept-package-agreements
        Write-Host ""
        Write-OK "Node.js installed."
        Write-Warn "Close and re-open this window for node/npm to work."
    } else {
        if (Ask-YesNo "Open nodejs.org in your browser?") {
            Start-Process "https://nodejs.org/"
        }
        Write-Host ""
        Write-Host "  Download the LTS version. Install, then press ENTER." -ForegroundColor Gray
        Pause-Step
    }
} else {
    Write-Step 5 "Node.js"
    Write-OK "Already installed : skipping."
}

# ═══════════════════════════════════════════════════════════════
#  STEP 6: Docker Desktop
# ═══════════════════════════════════════════════════════════════

if (-not $checks["docker"]) {
    Write-Step 6 "Install Docker Desktop"
    Write-Info "Docker runs code in safe sandboxed containers."
    Write-Host ""
    Write-Host "  ┌──────────────────────────────────────────────────┐" -ForegroundColor Yellow
    Write-Host "  │  IMPORTANT: During Docker install:                │" -ForegroundColor Yellow
    Write-Host "  │                                                  │" -ForegroundColor Yellow
    Write-Host "  │  • CHECK 'Use WSL 2 instead of Hyper-V'         │" -ForegroundColor Yellow
    Write-Host "  │  • You'll need to RESTART your PC after install  │" -ForegroundColor Yellow
    Write-Host "  │  • After restart, open Docker Desktop from the   │" -ForegroundColor Yellow
    Write-Host "  │    Start menu and wait for it to finish loading  │" -ForegroundColor Yellow
    Write-Host "  │  • You DON'T need to sign in : skip that step   │" -ForegroundColor Yellow
    Write-Host "  └──────────────────────────────────────────────────┘" -ForegroundColor Yellow

    if (Ask-YesNo "Open Docker Desktop download page?") {
        Start-Process "https://docs.docker.com/desktop/install/windows-install/"
    }
    Write-Host ""
    Write-Host "  Download and install Docker Desktop." -ForegroundColor Gray
    Write-Host "  Restart your PC if asked, then run this installer again." -ForegroundColor Gray
    Pause-Step
} elseif (-not $checks["docker_daemon"]) {
    Write-Step 6 "Docker Desktop"
    Write-OK "Docker is installed but the daemon isn't running."
    Write-Host ""
    Write-Host "  ┌──────────────────────────────────────────────────┐" -ForegroundColor Yellow
    Write-Host "  │  WHAT TO DO:                                     │" -ForegroundColor Yellow
    Write-Host "  │                                                  │" -ForegroundColor Yellow
    Write-Host "  │  1. Press the Windows key                        │" -ForegroundColor Yellow
    Write-Host "  │  2. Type: Docker Desktop                         │" -ForegroundColor Yellow
    Write-Host "  │  3. Click to open it                             │" -ForegroundColor Yellow
    Write-Host "  │  4. Wait for the whale icon to stop animating    │" -ForegroundColor Yellow
    Write-Host "  │  5. Come back here and press ENTER               │" -ForegroundColor Yellow
    Write-Host "  └──────────────────────────────────────────────────┘" -ForegroundColor Yellow
    Pause-Step
} else {
    Write-Step 6 "Docker Desktop"
    Write-OK "Already installed and running : skipping."
}

# ═══════════════════════════════════════════════════════════════
#  STEP 7: Gemini CLI
# ═══════════════════════════════════════════════════════════════

if (-not $checks["gemini"]) {
    Write-Step 7 "Install Gemini CLI"
    Write-Info "The Gemini CLI sends prompts to Google's AI."
    Write-Host ""

    # Check if npm is available now
    $npmNow = & npm --version 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Warn "npm is not available. Install Node.js first (Step 5)."
        Write-Host ""
        Write-Host "  ┌──────────────────────────────────────────────────┐" -ForegroundColor Yellow
        Write-Host "  │  If you just installed Node.js, you need to:     │" -ForegroundColor Yellow
        Write-Host "  │                                                  │" -ForegroundColor Yellow
        Write-Host "  │  1. CLOSE this window                            │" -ForegroundColor Yellow
        Write-Host "  │  2. Open a NEW PowerShell window                 │" -ForegroundColor Yellow
        Write-Host "  │  3. Run this installer again                     │" -ForegroundColor Yellow
        Write-Host "  └──────────────────────────────────────────────────┘" -ForegroundColor Yellow
        Pause-Step
    } else {
        if (Ask-YesNo "Install Gemini CLI now?") {
            Write-Info "Running: npm install -g @google/gemini-cli"
            & npm install -g "@google/gemini-cli"
            Write-Host ""
            Write-OK "Gemini CLI installed."
            Write-Host ""
            Write-Header "Now let's authenticate with Google:"
            Write-Host ""
            Write-Host "  ┌──────────────────────────────────────────────────┐" -ForegroundColor Cyan
            Write-Host "  │  WHAT WILL HAPPEN:                               │" -ForegroundColor Cyan
            Write-Host "  │                                                  │" -ForegroundColor Cyan
            Write-Host "  │  1. A 'gemini' session will start below          │" -ForegroundColor Cyan
            Write-Host "  │  2. Select 'Login with Google' (use arrow keys)  │" -ForegroundColor Cyan
            Write-Host "  │  3. Your browser will open : sign in             │" -ForegroundColor Cyan
            Write-Host "  │  4. Grant permissions when asked                 │" -ForegroundColor Cyan
            Write-Host "  │  5. Come back to this window                     │" -ForegroundColor Cyan
            Write-Host "  │  6. Press Ctrl+C to exit gemini                  │" -ForegroundColor Cyan
            Write-Host "  │  7. Then press ENTER to continue the installer   │" -ForegroundColor Cyan
            Write-Host "  └──────────────────────────────────────────────────┘" -ForegroundColor Cyan
            Write-Host ""
            if (Ask-YesNo "Start authentication now?") {
                & gemini
                Write-Host ""
                Write-OK "Authentication complete."
            }
        }
    }
} else {
    Write-Step 7 "Gemini CLI"
    Write-OK "Already installed : skipping."
}

# ═══════════════════════════════════════════════════════════════
#  STEP 8: Ollama (Optional)
# ═══════════════════════════════════════════════════════════════

Write-Step 8 "Install Ollama (Optional)"

if (-not $checks["ollama"]) {
    Write-Info "Ollama runs AI models locally on your PC."
    Write-Info "It's recommended but NOT required : you can skip it."
    Write-Host ""

    if (Ask-YesNo "Install Ollama?" "N") {
        if (Ask-YesNo "Open download page in browser?") {
            Start-Process "https://ollama.com/download"
        }
        Write-Host ""
        Write-Host "  ┌──────────────────────────────────────────────────┐" -ForegroundColor Cyan
        Write-Host "  │  STEPS:                                          │" -ForegroundColor Cyan
        Write-Host "  │                                                  │" -ForegroundColor Cyan
        Write-Host "  │  1. Download OllamaSetup.exe                     │" -ForegroundColor Cyan
        Write-Host "  │  2. Run it (if SmartScreen warns: More info →    │" -ForegroundColor Cyan
        Write-Host "  │     Run anyway)                                  │" -ForegroundColor Cyan
        Write-Host "  │  3. Come back here and press ENTER               │" -ForegroundColor Cyan
        Write-Host "  └──────────────────────────────────────────────────┘" -ForegroundColor Cyan
        Pause-Step

        Write-Info "Pulling AI models (4-5 GB each, may take a while)..."
        if (Ask-YesNo "Pull llama3 model now?") {
            & ollama pull llama3
        }
        if (Ask-YesNo "Pull llava model now?") {
            & ollama pull llava
        }
    } else {
        Write-Info "Skipped : you can install Ollama later if you want."
    }
} else {
    Write-OK "Already installed : skipping."
}

# ═══════════════════════════════════════════════════════════════
#  STEP 9: Python Packages
# ═══════════════════════════════════════════════════════════════

if (-not $checks["packages"]) {
    Write-Step 9 "Install Python Packages"
    Write-Info "Installing required Python libraries..."
    Write-Host ""

    $reqFile = Join-Path $PSScriptRoot "requirements.txt"
    if (Test-Path $reqFile) {
        if (Ask-YesNo "Install Python packages from requirements.txt?") {
            Write-Info "Running: pip install --upgrade -r requirements.txt"
            & pip install --upgrade -r $reqFile
            Write-Host ""
            # Verify
            $verify = & python -c "import fastapi, uvicorn, aiohttp, psutil; print('ok')" 2>&1
            if ($verify -match "ok") {
                Write-OK "All packages installed successfully."
            } else {
                Write-Fail "Some packages failed. Try running manually:"
                Write-Host "    pip install --upgrade -r requirements.txt" -ForegroundColor Yellow
            }
        }
    } else {
        Write-Warn "requirements.txt not found at: $reqFile"
        Write-Host ""
        Write-Host "  ┌──────────────────────────────────────────────────┐" -ForegroundColor Yellow
        Write-Host "  │  WHAT TO DO:                                     │" -ForegroundColor Yellow
        Write-Host "  │                                                  │" -ForegroundColor Yellow
        Write-Host "  │  1. Press the Windows key                        │" -ForegroundColor Yellow
        Write-Host "  │  2. Type: PowerShell (and open it)               │" -ForegroundColor Yellow
        Write-Host "  │  3. Type: cd 'PATH_TO_PROJECT_FOLDER'            │" -ForegroundColor Yellow
        Write-Host "  │  4. Type: pip install -r requirements.txt        │" -ForegroundColor Yellow
        Write-Host "  │  5. Press ENTER and wait                         │" -ForegroundColor Yellow
        Write-Host "  └──────────────────────────────────────────────────┘" -ForegroundColor Yellow
    }
} else {
    Write-Step 9 "Python Packages"
    Write-OK "Already installed : skipping."
}

# ═══════════════════════════════════════════════════════════════
#  STEP 10: Docker Sandbox Image
# ═══════════════════════════════════════════════════════════════

if (-not $checks["sandbox"]) {
    Write-Step 10 "Build Docker Sandbox Image"
    Write-Info "This creates the secure container for running code."
    Write-Info "Takes 3-10 minutes depending on your internet speed."
    Write-Host ""

    # Check Docker is running
    $dockerRunning = & docker info 2>&1
    if ($LASTEXITCODE -ne 0) {
        Write-Warn "Docker is not running. Please start Docker Desktop first."
        Write-Host ""
        Write-Host "  ┌──────────────────────────────────────────────────┐" -ForegroundColor Yellow
        Write-Host "  │  1. Press the Windows key                        │" -ForegroundColor Yellow
        Write-Host "  │  2. Type: Docker Desktop                         │" -ForegroundColor Yellow
        Write-Host "  │  3. Open it and wait for it to finish loading    │" -ForegroundColor Yellow
        Write-Host "  │  4. Come back here and press ENTER               │" -ForegroundColor Yellow
        Write-Host "  └──────────────────────────────────────────────────┘" -ForegroundColor Yellow
        Pause-Step
    }

    $dockerfile = Join-Path $PSScriptRoot "supervisor\Dockerfile.sandbox"
    if (Test-Path $dockerfile) {
        if (Ask-YesNo "Build the sandbox image now? (3-10 min)") {
            Write-Info "Building... (this will take a few minutes)"
            $buildDir = Join-Path $PSScriptRoot "supervisor"
            & docker build -t supervisor-sandbox -f $dockerfile $buildDir
            if ($LASTEXITCODE -eq 0) {
                Write-OK "Sandbox image built successfully!"
            } else {
                Write-Fail "Build failed. Check the error above."
                Write-Host "  You can retry later by running:" -ForegroundColor Yellow
                Write-Host "    cd supervisor" -ForegroundColor Yellow
                Write-Host "    docker build -t supervisor-sandbox -f Dockerfile.sandbox ." -ForegroundColor Yellow
            }
        }
    } else {
        Write-Warn "Dockerfile.sandbox not found at: $dockerfile"
    }
} else {
    Write-Step 10 "Docker Sandbox Image"
    Write-OK "Already built : skipping."
}

# ═══════════════════════════════════════════════════════════════
#  DONE!
# ═══════════════════════════════════════════════════════════════

Write-Host ""
Write-Host "  ══════════════════════════════════════════════════" -ForegroundColor Green
Write-Host "  🎉  Setup Complete!" -ForegroundColor Green
Write-Host "  ══════════════════════════════════════════════════" -ForegroundColor Green
Write-Host ""
Write-Host "  You can now launch Supervisor AI by double-clicking:" -ForegroundColor White
Write-Host "    Command Centre.bat" -ForegroundColor Cyan
Write-Host ""
Write-Host "  Or re-run the visual setup guide:" -ForegroundColor Gray
Write-Host "    SETUP.bat" -ForegroundColor Gray
Write-Host ""

if (Ask-YesNo "Launch Command Centre now?") {
    $bat = Join-Path $PSScriptRoot "Command Centre.bat"
    if (Test-Path $bat) {
        Start-Process $bat
    } else {
        Write-Warn "'Command Centre.bat' not found in $PSScriptRoot"
    }
}

Write-Host ""
Write-Host "  Thanks for using Supervisor AI! 🚀" -ForegroundColor Cyan
Write-Host ""

# Write check_results for the HTML wizard too
& "$PSScriptRoot\check_setup.bat" 2>$null

Pause-Step
