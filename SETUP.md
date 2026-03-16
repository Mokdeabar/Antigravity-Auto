# Supervisor AI : Complete Setup Guide

> **Version:** V74 · **Last Updated:** March 2026  
> **Platform:** Windows 10/11 (64-bit) only  
> **Difficulty:** Beginner-friendly : no experience required

---

## 🚀 Choose Your Setup Method

You have **three options** to get set up. Pick whichever suits you best:

| Method | Best For | How to Start |
|---|---|---|
| **🖥️ Interactive Wizard** | Visual learners, step-by-step | Double-click **`SETUP.bat`** in the project folder |
| **⚡ PowerShell Installer** | Fastest automated setup | Right-click **`INSTALL.ps1`** → "Run with PowerShell" |
| **📖 Manual Guide** | Full control, experienced users | Follow the guide below ↓ |

### Interactive Wizard (`SETUP.bat`)
Double-click `SETUP.bat` in the project folder. It will:
1. Automatically check what's already installed on your system
2. Open a visual step-by-step wizard in your browser
3. Show green ✅ / red ❌ banners on each step so you know what to skip

### PowerShell Installer (`INSTALL.ps1`)
Right-click `INSTALL.ps1` → **"Run with PowerShell"**. It will:
1. Scan your system and show what's installed
2. Offer to install missing tools automatically (`winget`)
3. Walk you through authentication and sandbox setup
4. Launch the Command Centre when done

> [!TIP]
> If you're new to computers, start with the **Interactive Wizard** (`SETUP.bat`). It includes a terminal basics guide and explains everything visually.

### Manual Guide (This Document)
If you prefer to do everything yourself, follow the step-by-step instructions below.

---

This guide walks you through **everything** from a fresh Windows install to a running Supervisor Command Centre. Follow every step in order. Do **not** skip sections.

---

## Table of Contents

1. [System Requirements](#1-system-requirements)
2. [Enable BIOS Virtualisation](#2-enable-bios-virtualisation)
3. [Enable WSL2 (Windows Subsystem for Linux)](#3-enable-wsl2)
4. [Install Git](#4-install-git)
5. [Install Python 3.13+](#5-install-python)
6. [Install Node.js 20+ LTS](#6-install-nodejs)
7. [Install Docker Desktop](#7-install-docker-desktop)
8. [Install & Authenticate the Google Gemini CLI](#8-install-gemini-cli)
9. [Install Ollama (Optional : Local LLM)](#9-install-ollama-optional)
10. [Set Up the Project Folder Structure](#10-set-up-project-folder-structure)
11. [Install Python Dependencies](#11-install-python-dependencies)
12. [Build the Docker Sandbox Image](#12-build-docker-sandbox-image)
13. [First Run : Launch the Command Centre](#13-first-run)
14. [Verify Everything Works](#14-verify-everything-works)
15. [Configuration Reference](#15-configuration-reference)
16. [Troubleshooting](#16-troubleshooting)

---

## 1. System Requirements

Before you begin, confirm your PC meets **all** of these:

| Requirement | Minimum | Recommended |
|---|---|---|
| **OS** | Windows 10 64-bit build 19041+ | Windows 11 23H2+ |
| **CPU** | 64-bit with SLAT & virtualisation | Intel i5/AMD Ryzen 5+ |
| **RAM** | 8 GB | 16 GB+ |
| **Disk** | 10 GB free | 30 GB+ free (Docker images + models) |
| **Internet** | Required for setup | Required for Gemini API calls |

### How to Check Your Windows Version

1. Press **Win + R**, type `winver`, press Enter.
2. You need **Windows 10 version 22H2 (build 19045)** or later, or **Windows 11**.

### How to Check if Virtualisation is Enabled

1. Press **Ctrl + Shift + Esc** to open Task Manager.
2. Click the **Performance** tab → **CPU**.
3. Look for **"Virtualisation: Enabled"** at the bottom-right.
4. If it says **"Disabled"**, you must enable it in BIOS first (see next section).

---

## 2. Enable BIOS Virtualisation

> [!IMPORTANT]
> Docker Desktop and WSL2 **will not work** without this. This is the #1 reason for setup failures.

If Task Manager shows "Virtualisation: Disabled":

1. **Restart** your PC.
2. During boot, press the BIOS key repeatedly. Common keys:
   - **Dell:** F2
   - **HP:** F10 or Esc
   - **Lenovo:** F1 or F2
   - **ASUS:** F2 or Del
   - **Acer:** F2 or Del
   - **MSI:** Del
3. Navigate to **Advanced** → **CPU Configuration** (varies by manufacturer).
4. Find the virtualisation setting:
   - **Intel CPUs:** Look for **"Intel Virtualization Technology"** or **"VT-x"** → Set to **Enabled**
   - **AMD CPUs:** Look for **"SVM Mode"** or **"AMD-V"** → Set to **Enabled**
5. **Save & Exit** (usually F10).
6. After reboot, re-check Task Manager → Performance → CPU → confirm "Virtualisation: Enabled".

---

## 3. Enable WSL2

WSL2 is required by Docker Desktop. Open **PowerShell as Administrator**:

1. Press **Win**, type `PowerShell`, right-click → **Run as administrator**.
2. Run this single command:
   ```powershell
   wsl --install
   ```
3. This enables both **"Windows Subsystem for Linux"** and **"Virtual Machine Platform"** features.
4. **Restart your PC** when prompted.
5. After reboot, open PowerShell (admin) again and run:
   ```powershell
   wsl --update
   wsl --set-default-version 2
   ```
6. Verify:
   ```powershell
   wsl --version
   ```
   You should see WSL version **2.1.5** or higher.

> [!TIP]
> If `wsl --install` fails, enable the features manually:
> ```powershell
> dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart
> dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart
> ```
> Then restart and run `wsl --set-default-version 2`.

---

## 4. Install Git

Git is used by the Supervisor for version control checkpoints.

1. Download from: **https://git-scm.com/downloads/win**
2. Run the installer. Accept all defaults : just click **Next** through every screen.
3. Verify in a **new** Command Prompt or PowerShell:
   ```
   git --version
   ```
   Expected output: `git version 2.47.0` or similar.

**Or install via winget** (open PowerShell as admin):
```powershell
winget install Git.Git --accept-source-agreements --accept-package-agreements
```

> [!NOTE]
> Close and reopen any terminal after installing Git so the PATH updates.

---

## 5. Install Python

The Supervisor requires **Python 3.11 or newer** (3.13 recommended).

### Option A: Download Installer (Recommended for beginners)

1. Go to: **https://www.python.org/downloads/**
2. Click the big yellow **"Download Python 3.13.x"** button.
3. Run the installer.

> [!CAUTION]
> **CRITICAL:** On the very first screen of the installer, check the box that says **"Add python.exe to PATH"**. If you miss this, nothing will work.

4. Click **"Install Now"** (not "Customize").
5. Wait for installation to complete. Click **Close**.

### Option B: Install via winget

```powershell
winget install Python.Python.3.13 --accept-source-agreements --accept-package-agreements
```

### Verify

Open a **new** Command Prompt or PowerShell (close any existing ones first):

```
python --version
```

Expected: `Python 3.13.x`

```
pip --version
```

Expected: `pip 24.x.x from ...`

---

## 6. Install Node.js

The Gemini CLI is an npm package, so you need Node.js.

### Option A: Download Installer (Recommended)

1. Go to: **https://nodejs.org/**
2. Download the **LTS** version (should be 20.x or 22.x).
3. Run the installer. Accept all defaults.

### Option B: Install via winget

```powershell
winget install OpenJS.NodeJS.LTS --accept-source-agreements --accept-package-agreements
```

### Verify

Open a **new** terminal:

```
node --version
```

Expected: `v20.x.x` or `v22.x.x`

```
npm --version
```

Expected: `10.x.x` or higher

---

## 7. Install Docker Desktop

Docker is used to create isolated sandbox containers where code changes are executed safely.

### Step 1: Download & Install

1. Go to: **https://docs.docker.com/desktop/install/windows-install/**
2. Click **"Docker Desktop for Windows : AMD64"** (or ARM64 if you have an ARM device).
3. Run the installer `Docker Desktop Installer.exe`.
4. When prompted:
   - ✅ **Use WSL 2 instead of Hyper-V** : make sure this is checked
   - ✅ **Add shortcut to desktop** : optional but handy
5. Click **OK** and let it install.
6. **Restart your PC** when prompted.

### Step 2: First Launch

1. After reboot, Docker Desktop should start automatically. If not, find "Docker Desktop" in the Start menu and open it.
2. **Accept the Docker Subscription Service Agreement** when prompted.
3. You may skip signing in : a Docker Hub account is **not required** for this project.
4. Wait for the Docker engine to fully start. The whale icon in the system tray will stop animating when ready.

### Step 3: Verify

Open a **new** terminal:

```
docker --version
```

Expected: `Docker version 27.x.x` or higher

```
docker info
```

This should print a wall of info about the Docker engine. If you see an error like "Cannot connect to the Docker daemon", Docker Desktop hasn't fully started yet : wait a minute and try again.

> [!WARNING]
> Docker Desktop must be **running** every time you use Supervisor. It does not run as a background service by default. To auto-start it:
> 1. Open Docker Desktop → ⚙️ Settings → **General**
> 2. Check **"Start Docker Desktop when you sign in to Windows"**

---

## 8. Install & Authenticate the Google Gemini CLI

The Gemini CLI is the brain of the Supervisor : it sends prompts to Google's Gemini models.

### Step 1: Install the CLI

Open a terminal (Command Prompt or PowerShell):

```
npm install -g @google/gemini-cli
```

Wait for the install to complete. Verify:

```
gemini --version
```

If `gemini` is not found, try:
```
%APPDATA%\npm\gemini.cmd --version
```

> [!NOTE]
> The Supervisor automatically adds `%APPDATA%\npm` to the PATH at runtime, so even if `gemini` isn't found globally, it will work when you launch via `Command Centre.bat`.

### Step 2: Authenticate (First Run)

1. Open a terminal and run:
   ```
   gemini
   ```
2. Select **"Login with Google"** when prompted.
3. A browser window will open. Sign in with your **Google account**.
4. Grant the requested permissions.
5. Return to the terminal : you should see a confirmation that authentication succeeded.
6. Press **Ctrl + C** to exit Gemini.

Your credentials are cached locally at:
```
%LOCALAPPDATA%\gemini\credentials.json
```

### Free Tier Limits

With a free Google account, you get:
- **60 requests per minute**
- **1,000 requests per day**
- Access to **Gemini 2.5 Pro** and **Gemini 3** models
- **1 million token** context window
- **No API key required** : authentication is via your Google account

> [!TIP]
> If you have a **Google AI Pro** ($19.99/mo) or **Google AI Ultra** ($249.99/mo) subscription, sign in with the Google account associated with that plan for higher limits.

---

## 9. Install Ollama (Optional · Legacy)

> [!NOTE]
> **V64 update:** Ollama has been replaced by **Gemini Lite Intelligence**, which uses the Gemini CLI directly. Ollama is no longer required and is kept only for legacy support. You can safely skip this section.

1. Download from: **https://ollama.com/download**
2. Run `OllamaSetup.exe`. If SmartScreen warns you, click **"More info" → "Run anyway"**.
3. Ollama will install and start automatically (runs in the system tray).
4. Open a terminal and pull the required models:

```
ollama pull llama3
ollama pull llava
```

> `llama3` is used for text tasks (~4.7 GB download).  
> `llava` is used for visual QA / screenshot analysis (~4.7 GB download).

### Verify

```
ollama list
```

Expected output:
```
NAME          ID           SIZE    MODIFIED
llama3:latest ...          4.7 GB  ...
llava:latest  ...          4.7 GB  ...
```

> [!NOTE]
> If you skip Ollama, the Supervisor will still work : it just won't have access to the local LLM brain. All tasks will go through the Gemini CLI instead.

---

## 10. Set Up the Project Folder Structure

The Supervisor expects a specific folder layout on your Desktop.

### Step 1: Create the Experiments Folder

Create this folder path **exactly** (case-sensitive):

```
C:\Users\<YOUR_USERNAME>\Desktop\Experiments\
```

Replace `<YOUR_USERNAME>` with your actual Windows username. For example:
```
C:\Users\John\Desktop\Experiments\
```

### Step 2: Extract the Supervisor

1. Extract the Supervisor zip file into the Experiments folder.
2. You should end up with:
   ```
   C:\Users\<YOUR_USERNAME>\Desktop\Experiments\Antigravity Auto\
   ```

### Step 3: Verify Folder Contents

Your `Antigravity Auto` folder should contain at minimum:

```
Antigravity Auto\
├── Command Centre.bat          ← Double-click this to run
├── Launch Supervisor.bat
├── requirements.txt
├── README.md
├── SETUP.md                    ← This file
├── supervisor\                 ← Core Python package
│   ├── main.py
│   ├── launcher.py
│   ├── config.py
│   ├── api_server.py
│   ├── Dockerfile.sandbox
│   ├── ui\                     ← Command Centre web UI
│   └── ... (50+ Python files)
└── tests\
```

### Step 4: Create Project Folders

The Supervisor builds projects in their own folders under `Experiments`. For each project you want to supervise, create a folder:

```
C:\Users\<YOUR_USERNAME>\Desktop\Experiments\<Project Name>\
```

For example:
```
C:\Users\<YOUR_USERNAME>\Desktop\Experiments\PROS Trainer Operating System\
```

> [!IMPORTANT]
> The project folder does **not** need to contain any files. The Supervisor can create everything from scratch. But the folder **must exist** before you point the Command Centre at it.

---

## 11. Install Python Dependencies

Open a terminal and navigate to the Supervisor folder:

```
cd "C:\Users\<YOUR_USERNAME>\Desktop\Experiments\Antigravity Auto"
```

Install the required Python packages:

```
pip install --upgrade -r requirements.txt
```

### What Gets Installed

| Package | Purpose |
|---|---|
| `aiohttp` | Async HTTP for Ollama brain communication |
| `fastapi` | Command Centre API server |
| `uvicorn` | ASGI server for FastAPI |
| `websockets` | Real-time WebSocket Glass Brain |
| `pydantic` | Data validation |
| `starlette` | FastAPI dependency |
| `psutil` | System health monitoring (CPU, memory) |
| `beautifulsoup4` | HTML parsing for workspace analysis |
| `tzdata` | Windows timezone data |

### Verify

```
python -c "import fastapi, uvicorn, aiohttp, psutil; print('All packages OK')"
```

Expected: `All packages OK`

---

## 12. Build the Docker Sandbox Image

The Supervisor executes code changes inside a Docker container. You must build this image **once** before first use.

Open a terminal in the Supervisor folder:

```
cd "C:\Users\<YOUR_USERNAME>\Desktop\Experiments\Antigravity Auto\supervisor"
```

Build the image:

```
docker build -t supervisor-sandbox -f Dockerfile.sandbox .
```

> [!NOTE]
> This will take **3-10 minutes** on the first run. It downloads Debian, Python 3, Node.js 20, PHP, and installs linting/formatting tools. The final image is ~300 MB.

### Verify

```
docker images supervisor-sandbox
```

Expected output:
```
REPOSITORY           TAG       IMAGE ID       CREATED          SIZE
supervisor-sandbox   latest    abc123def456   X minutes ago    ~300MB
```

> [!CAUTION]
> If the build fails with a network error, check that Docker Desktop is running and that you have internet access. Then retry the build command.

---

## 13. First Run

Everything is installed. Time to launch!

### Option A: Double-Click (Recommended)

1. Make sure **Docker Desktop is running** (whale icon in system tray).
2. Navigate to `C:\Users\<YOUR_USERNAME>\Desktop\Experiments\Antigravity Auto\`
3. Double-click **`Command Centre.bat`**

The batch file will:
- ✅ Verify Python, Node.js, Gemini CLI, Docker are installed
- ✅ Start WSL and Docker services if needed
- ✅ Check Ollama availability
- ✅ Install/update Python dependencies
- ✅ Launch the Command Centre API server on **http://localhost:8420**
- ✅ Auto-open your browser to the dashboard

### Option B: Manual Launch

```
cd "C:\Users\<YOUR_USERNAME>\Desktop\Experiments\Antigravity Auto"
set PYTHONIOENCODING=utf-8
python supervisor\launcher.py
```

### What You Should See

```
  ╔══════════════════════════════════════════════════╗
  ║        ⚡ Supervisor AI : V74 Command Centre      ║
  ║                                                    ║
  ║   http://localhost:8420                             ║
  ║                                                    ║
  ║   Standing by. Select a project in the UI.         ║
  ╚══════════════════════════════════════════════════╝
```

Your browser opens to the **Command Centre dashboard**. From there:

1. Enter your **project path** (e.g., `C:\Users\John\Desktop\Experiments\PROS Trainer Operating System`)
2. Enter your **goal** (a description of what you want built)
3. Click **Launch**

---

## 14. Verify Everything Works

Run through this checklist to confirm all systems are operational:

```
✅ Virtualisation enabled       →  Task Manager > Performance > CPU
✅ WSL2 installed               →  wsl --version
✅ Git installed                →  git --version
✅ Python 3.11+                 →  python --version
✅ pip working                  →  pip --version
✅ Node.js 20+                  →  node --version
✅ npm working                  →  npm --version
✅ Docker Desktop running       →  docker info
✅ Sandbox image built          →  docker images supervisor-sandbox
✅ Gemini CLI installed         →  gemini --version (or %APPDATA%\npm\gemini.cmd --version)
✅ Gemini CLI authenticated     →  Run 'gemini' and verify it connects
✅ Ollama running (optional)    →  ollama list
✅ Python packages installed    →  python -c "import fastapi; print('OK')"
✅ Command Centre starts        →  Double-click "Command Centre.bat"
✅ Browser opens to dashboard   →  http://localhost:8420
```

---

## 15. Configuration Reference

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `COMMAND_CENTRE_PORT` | `8420` | API server port |
| `PYTHONIOENCODING` | `utf-8` | Console encoding (set by batch) |

### Key Config File: `supervisor/config.py`

Important values (all have sensible defaults : usually no changes needed):

| Setting | Default | What It Does |
|---|---|---|
| `CONCURRENCY` | `3` | Max parallel Gemini CLI tasks |
| `SANDBOX_MOUNT_MODE` | `"copy"` | File mount strategy for containers |
| `PROMPT_SIZE_MAX_CHARS` | `1000000` | Max prompt size sent to Gemini |
| `PROMPT_SIZE_WARN_CHARS` | `500000` | Warning threshold for prompt size |
| `GEMINI_CLI_MODEL` | `"auto"` | Default Gemini model (routes to best Gemini 3+ model) |

### Ports Used

| Port | Used By | Direction |
|---|---|---|
| `8420` | Command Centre API + UI | Host → Browser |
| `3000` | Dev server preview (inside Docker) | Container → mapped to random host port |
| `11434` | Ollama API (if installed) | Host → localhost |

---

## 16. Troubleshooting

### "Python not found" / "pip not found"

**Cause:** Python was installed without adding to PATH.  
**Fix:**
1. Re-run the Python installer → **Modify** → check "Add Python to environment variables".
2. Or add manually: Settings → System → About → Advanced system settings → Environment Variables → Path → add `C:\Users\<YOU>\AppData\Local\Programs\Python\Python313\` and the `\Scripts\` subfolder.
3. Close and reopen all terminals.

### "Docker daemon not responding"

**Cause:** Docker Desktop hasn't fully started.  
**Fix:**
1. Look at the whale icon in the system tray. If it's still animating, wait.
2. Open Docker Desktop directly and wait for the "Docker Desktop is running" message.
3. If stuck, restart Docker Desktop: right-click tray icon → **Restart**.
4. Nuclear option: Restart your PC, then open Docker Desktop.

### "Virtualisation is disabled"

**Cause:** BIOS virtualisation (VT-x or SVM) is turned off.  
**Fix:** See [Section 2](#2-enable-bios-virtualisation). This **requires** a BIOS change and reboot.

### "Error building sandbox image"

**Cause:** Usually a network issue during Docker build.  
**Fix:**
1. Check internet connection.
2. Ensure Docker Desktop is running.
3. Retry: `docker build -t supervisor-sandbox -f Dockerfile.sandbox .`
4. If NodeSource download fails, you may need to configure Docker DNS: Docker Desktop → ⚙️ Settings → Docker Engine → add `"dns": ["8.8.8.8", "8.8.4.4"]` → Apply & Restart.

### "Gemini CLI: command not found"

**Cause:** npm global bin not in PATH.  
**Fix:** The `Command Centre.bat` auto-adds `%APPDATA%\npm` to PATH. If running manually:
```
set "PATH=%APPDATA%\npm;%PATH%"
gemini --version
```

### "WSL2 installation failed" / "0x80370102"

**Cause:** Virtualisation not enabled or Windows features missing.  
**Fix:**
1. Enable BIOS virtualisation (Section 2).
2. Enable Windows features manually:
   ```powershell
   dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart
   dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart
   ```
3. Restart PC.
4. Download the WSL2 kernel update: https://aka.ms/wsl2kernel

### "Port 8420 already in use"

**Cause:** A previous Supervisor session didn't shut down cleanly.  
**Fix:** The launcher auto-kills stale processes. If it persists:
```
netstat -ano | findstr "8420"
taskkill /F /PID <PID_FROM_ABOVE>
```

### "Ollama not found" warning

**Cause:** Ollama is not installed.  
**Impact:** Non-critical. **V64+:** Ollama has been replaced by Gemini Lite Intelligence. The Supervisor works fully without it.  
**Fix:** This warning can be safely ignored. Install Ollama (Section 9) only if you have a specific use case for local models.

### "Prompt Guard: truncating" warnings in logs

**Cause:** Older config with small prompt limits.  
**Fix:** Check `supervisor/config.py` : `PROMPT_SIZE_MAX_CHARS` should be `200000` (not `15000`).

### Docker containers keep accumulating

**Fix:** Clean up stopped containers periodically:
```
docker container prune -f
docker image prune -f
```

### "Permission denied" errors on Windows

**Fix:** Run Command Prompt or PowerShell as **Administrator** for Docker/WSL commands.

---

## Quick-Start Checklist (TL;DR)

For experienced users who just need the commands:

```powershell
# 1. Enable WSL2
wsl --install  # then restart PC
wsl --update
wsl --set-default-version 2

# 2. Install tools (via winget)
winget install Git.Git
winget install Python.Python.3.13
winget install OpenJS.NodeJS.LTS
winget install Docker.DockerDesktop
# Restart PC after Docker install

# 3. Install Gemini CLI
npm install -g @google/gemini-cli
gemini  # authenticate with Google account, then Ctrl+C

# 4. Optional: Install Ollama
# Download from https://ollama.com/download
ollama pull llama3
ollama pull llava

# 5. Setup project
cd "C:\Users\%USERNAME%\Desktop\Experiments\Antigravity Auto"
pip install --upgrade -r requirements.txt
cd supervisor
docker build -t supervisor-sandbox -f Dockerfile.sandbox .

# 6. Launch
cd ..
"Command Centre.bat"
```

---

> **Need help?** Check the `supervisor\supervisor.log` file for detailed error messages. The Command Centre dashboard also shows real-time system health and error counts.
