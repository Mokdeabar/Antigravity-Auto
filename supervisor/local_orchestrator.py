"""
local_orchestrator.py — V13 Omni-Brain Reasoning Layer (The Manager)

⚠️  DEPRECATED in V64: All Ollama functionality has been replaced by Gemini Lite Intelligence.
    The /api/lite/ask endpoint in api_server.py now handles Q&A using gemini-2.5-flash-lite
    with flash model fallback. This file is kept to avoid breaking any residual imports.
    No active callers remain.

Original purpose: Interfaced with a local Ollama model (defaulting to llama3).
Took raw snapshots, error logs, and the previous Worker output.
Outputs a strictly formulated, raw command string detailing the
exact goal for the Gemini CLI to accomplish.
"""

import asyncio
import json
import logging
import subprocess
import time
import urllib.request
import urllib.error
import sys
import os

from . import config

logger = logging.getLogger("supervisor.local_orchestrator")


class OllamaUnavailable(RuntimeError):
    """V32: Raised when Ollama fails to boot or respond. Non-fatal."""
    pass

async def ensure_ollama_running(model_name: str, host: str) -> None:
    """
    Autonomous Local LLM Bootstrapper.
    Guarantees the local Ollama inference engine is alive and serving before the main loop begins.
    """
    tags_url = f"{host.rstrip('/')}/api/tags"
    
    # 1. Ping test
    def _ping() -> bool:
        try:
            req = urllib.request.Request(tags_url)
            with urllib.request.urlopen(req, timeout=1.0) as response:
                return response.status == 200
        except Exception:
            return False

    if _ping():
        logger.info("🧠 Ollama engine is already running.")
    else:
        logger.warning("🧠 Ollama engine is offline. Attempting autonomous boot...")
        
        # --- ZERO-TOUCH INSTALLER ---
        import shutil
        import platform
        
        ollama_bin = shutil.which("ollama")
        
        # V37 SECURITY FIX: Check Windows fallback path before failing.
        # The supervisor MUST NOT auto-install system software (mandate constraint).
        if not ollama_bin and platform.system() == "Windows":
            fallback_path = os.path.expanduser("~\\AppData\\Local\\Programs\\Ollama\\ollama.exe")
            if os.path.exists(fallback_path):
                ollama_bin = fallback_path
                logger.info("🧠 Found Ollama at Windows fallback path: %s", ollama_bin)
        
        if not ollama_bin:
            logger.error("🧠 Ollama binary not found in PATH.")
            print(f"\n{config.ANSI_RED}=====================================================================")
            print("  Ollama is not installed.")
            print("")
            print("  Please install it manually:")
            if platform.system() == "Windows":
                print(f"    winget install Ollama.Ollama")
            elif platform.system() == "Darwin":
                print(f"    brew install ollama")
            else:
                print(f"    curl -fsSL https://ollama.com/install.sh | sh")
            print("")
            print("  After installing, restart the supervisor.")
            print(f"====================================================================={config.ANSI_RESET}\n")
            raise OllamaUnavailable(
                "Ollama is not installed. Install manually from https://ollama.com/ "
                "and restart the supervisor."
            )
        
        cmd_bin = ollama_bin
        
        try:
            if config.IS_WINDOWS:
                subprocess.Popen(
                    [cmd_bin, "serve"],
                    creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL
                )
            else:
                subprocess.Popen(
                    [cmd_bin, "serve"],
                    start_new_session=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL
                )
        except Exception as e:
            logger.error("CRITICAL: Failed to boot '%s serve': %s", cmd_bin, e)
            print(f"\n{config.ANSI_RED}CRITICAL ERROR: Failed to boot '{cmd_bin} serve': {e}{config.ANSI_RESET}")
            raise OllamaUnavailable(f"Failed to boot Ollama: {e}") from e
            
        # 2. Polling loop
        max_attempts = 15
        booted = False
        print(f"  {config.ANSI_YELLOW}🧠 Booting local Ollama engine...{config.ANSI_RESET}")
        for attempt in range(max_attempts):
            if _ping():
                booted = True
                logger.info("🧠 Ollama engine successfully booted.")
                print(f"  {config.ANSI_GREEN}✅ Ollama engine online.{config.ANSI_RESET}")
                break
            # V37 FIX (H-1): Use asyncio.sleep instead of blocking time.sleep.
            await asyncio.sleep(2.0)
            
        if not booted:
            err = "Local Ollama engine failed to boot within 30 seconds."
            logger.error(err)
            print(f"\n{config.ANSI_RED}=====================================================================")
            print(f"WARNING: {err}")
            print("Please check your local Ollama installation and ensure port 11434 is free.")
            print(f"====================================================================={config.ANSI_RESET}\n")
            raise OllamaUnavailable(err)

    # 3. Model Availability Check
    try:
        req = urllib.request.Request(tags_url)
        with urllib.request.urlopen(req, timeout=5.0) as response:
            data = json.loads(response.read().decode("utf-8"))
            
        models = [m.get("name", "") for m in data.get("models", [])]
        
        # Check both the text model and the vision model
        models_to_check = [model_name]
        vision_model = getattr(config, "OLLAMA_VISION_MODEL", "llava")
        if vision_model and vision_model != model_name:
            models_to_check.append(vision_model)
        
        for required_model in models_to_check:
            # Allow exact match or tag match (e.g. 'llama3:latest' satisfies 'llama3')
            if not any(m == required_model or m.startswith(f"{required_model}:") for m in models):
                logger.warning("🧠 Required model '%s' is missing. Pulling autonomously...", required_model)
                print(f"  {config.ANSI_YELLOW}⬇️ Required model '{required_model}' missing. Pulling (this may take a while)...{config.ANSI_RESET}")
                
                import shutil
                import platform
                import os
                pull_bin = shutil.which("ollama")
                if not pull_bin and platform.system() == "Windows":
                    pull_bin = os.path.expanduser("~\\AppData\\Local\\Programs\\Ollama\\ollama.exe")
                if not pull_bin:
                    pull_bin = "ollama"
                    
                subprocess.run([pull_bin, "pull", required_model], check=True)
                logger.info("🧠 Model '%s' pulled successfully.", required_model)
                print(f"  {config.ANSI_GREEN}✅ Model '{required_model}' pulled and ready.{config.ANSI_RESET}")
    except Exception as exc:
        logger.error("🧠 Failed to verify or pull model '%s': %s", model_name, exc)


class LocalManager:
    """Invokes the local Ollama model using its HTTP API."""

    def __init__(self, model_name: str = "llama3", host: str = "http://localhost:11434"):
        self.model_name = model_name
        self.host = host.rstrip("/")
        self._healthy = False  # V32: Track health state

    async def initialize(self) -> None:
        """V37 FIX (H-1): Async initialization. Call after construction."""
        await ensure_ollama_running(self.model_name, self.host)
        self._healthy = True

    def health_check(self) -> bool:
        """V32: Ping Ollama /api/ps to verify the server is responsive."""
        try:
            req = urllib.request.Request(f"{self.host}/api/ps")
            with urllib.request.urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    self._healthy = True
                    return True
        except Exception as exc:
            logger.warning("🧠 Ollama health check failed: %s", exc)
            self._healthy = False
        return False

    async def ask_local_model(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.1
    ) -> str:
        """
        V14 AGI: Universal Local LLM interface.
        Forces strict JSON output to prevent markdown hallucination.
        """
        logger.info("🧠 Routing to Local Ollama (%s)...", self.model_name)

        # V32: Health check before each request
        if not self._healthy:
            if not self.health_check():
                logger.warning("🧠 Ollama unhealthy — skipping local LLM call")
                return "{}"

        payload = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "options": {
                "temperature": temperature,
                "num_predict": 300
            },
            "format": "json",
            "stream": False,  # V32: Kept false for simplicity; streaming requires line-by-line parsing
            "keep_alive": "5m",  # V36: Unload from VRAM after 5min idle
        }

        # Destructive Command Blocklist
        destructive_keywords = [
            "rm -rf", "drop table", "truncate table", "delete from", 
            "mkfs", "format", ":(){:|:&};:", "sudo rm", "del /f /s /q", "rmdir /s /q"
        ]

        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(
                None,
                self._sync_http_post,
                f"{self.host}/api/chat",
                payload
            )
            
            response_text = result.get("message", {}).get("content", "").strip()
            
            # Blocklist check
            response_lower = response_text.lower()
            for bad_word in destructive_keywords:
                if bad_word in response_lower:
                    logger.warning("BLOCKED destructive command from local manager: %s", bad_word)
                    return '{"error": "blocked destructive command"}'
            
            return response_text
        except Exception as exc:
            logger.error("Local Manager failed to respond: %s", exc)
            return "{}"

    def _sync_http_post(self, url: str, data: dict, timeout: int = 120) -> dict:
        """V32: Reduced timeout from 300s→120s. 5-minute timeout was excessive for local inference."""
        req = urllib.request.Request(
            url,
            data=json.dumps(data).encode("utf-8"),
            headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    async def analyze_screenshot(
        self,
        image_path: str,
        prompt: str,
        temperature: float = 0.1,
    ) -> dict:
        """
        V14 Vision-First: Analyze a screenshot using a local vision-capable
        Ollama model (e.g. llava) before escalating to Gemini CLI.

        Uses Ollama's multimodal /api/chat with base64-encoded images.

        Returns:
            {"state": "WORKING|WAITING|CRASHED",
             "reason": "...",
             "confidence": "HIGH|MEDIUM|LOW"}
            Or empty dict on failure.
        """
        import base64

        vision_model = getattr(config, "OLLAMA_VISION_MODEL", "llava")
        vision_timeout = getattr(config, "OLLAMA_VISION_TIMEOUT", 120)

        logger.info("🧠👁️  Local vision analysis via %s ...", vision_model)

        # V30: Downscale image to 512x512 grayscale WebP for fast local inference
        # This prevents massive payloads from freezing the event loop and timing out.
        try:
            from PIL import Image
            import io

            with Image.open(image_path) as img:
                # Convert to grayscale and resize
                img = img.convert("L")  # Grayscale
                img.thumbnail((512, 512), Image.LANCZOS)

                # Compress as WebP
                buffer = io.BytesIO()
                img.save(buffer, format="WebP", quality=60)
                image_b64 = base64.b64encode(buffer.getvalue()).decode("utf-8")
                logger.info(
                    "🧠👁️  V30 downscaled: %dx%d grayscale WebP (%d bytes)",
                    img.width, img.height, len(buffer.getvalue()),
                )
        except ImportError:
            logger.debug("🧠👁️  PIL not available — using raw image")
            try:
                with open(image_path, "rb") as f:
                    image_b64 = base64.b64encode(f.read()).decode("utf-8")
            except Exception as exc:
                logger.error("🧠👁️  Failed to read screenshot: %s", exc)
                return {}
        except Exception as exc:
            logger.error("🧠👁️  Failed to downscale screenshot: %s", exc)
            # Fallback to raw image
            try:
                with open(image_path, "rb") as f:
                    image_b64 = base64.b64encode(f.read()).decode("utf-8")
            except Exception:
                return {}

        # Build the vision prompt requesting structured JSON
        system_prompt = (
            'You are a visual diagnostic AI analyzing a screenshot of an IDE. '
            'Reply with ONLY a JSON object in this exact format, nothing else:\n'
            '{"state": "WORKING", "reason": "what you see", "confidence": "HIGH"}\n\n'
            'Values for "state": WORKING, WAITING, or CRASHED.\n'
            'Values for "confidence": HIGH, MEDIUM, or LOW.\n\n'
            'HIGH confidence = you are very certain about the state.\n'
            'MEDIUM confidence = you can see some indicators but are not sure.\n'
            'LOW confidence = the image is unclear or ambiguous.\n'
        )

        payload = {
            "model": vision_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": prompt,
                    "images": [image_b64],
                },
            ],
            "options": {
                "temperature": temperature,
                "num_predict": 200,
            },
            "format": "json",
            "stream": False,
            "keep_alive": "5m",  # V36: Unload from VRAM after 5min idle
        }

        try:
            loop = asyncio.get_running_loop()
            result = await asyncio.wait_for(
                loop.run_in_executor(
                    None,
                    self._sync_http_post,
                    f"{self.host}/api/chat",
                    payload,
                    vision_timeout,
                ),
                timeout=vision_timeout + 5,  # asyncio guard slightly longer
            )

            response_text = result.get("message", {}).get("content", "").strip()
            logger.info("🧠👁️  Local vision raw response: %s", response_text[:200])

            # Robust JSON extraction — strip Markdown fences and find JSON object
            import re
            # Strip markdown code fences if present
            cleaned = re.sub(r"^```(?:json)?\s*\n?", "", response_text)
            cleaned = re.sub(r"\n?```\s*$", "", cleaned).strip()
            # Find the first JSON object via regex
            json_match = re.search(r'\{.*\}', cleaned, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group(0))
            else:
                logger.warning("🧠👁️  No JSON object found in local vision response")
                return {}
            state = data.get("state", "").upper()
            if state not in ("WORKING", "WAITING", "CRASHED"):
                logger.warning("🧠👁️  Invalid state from local vision: %s", state)
                return {}

            confidence = data.get("confidence", "LOW").upper()
            reason = data.get("reason", "No reason given")

            logger.info(
                "🧠👁️  Local vision result: state=%s, confidence=%s, reason=%s",
                state, confidence, reason[:80],
            )
            print(
                f"  {config.ANSI_CYAN}🧠👁️ Local vision: {state} "
                f"(confidence: {confidence}){config.ANSI_RESET}"
            )

            return {"state": state, "reason": reason, "confidence": confidence}

        except asyncio.TimeoutError:
            logger.warning("🧠👁️  Local vision timed out after %ds", vision_timeout)
            return {}
        except json.JSONDecodeError as exc:
            logger.warning("🧠👁️  Local vision returned invalid JSON: %s", exc)
            return {}
        except Exception as exc:
            logger.warning("🧠👁️  Local vision failed: %s", exc)
            return {}

    async def synthesize_followup(
        self, 
        chat_history: str, 
        system_goal: str,
        project_state: str = "",
        mandate: str = ""
    ) -> str:
        """
        Dynamically synthesize a context-aware follow-up instruction to unblock the agent.
        Takes the recent chat history, the state file, the mandate, and the overall
        objective, using the local Ollama LLM to generate the NEXT logical command.
        """
        system_prompt = (
            "You are the senior architect and project manager of an autonomous software engineering system. "
            "The AI coding agent has just finished its last task (or is stuck WAITING) and needs its "
            f"NEXT formal instruction to progress towards the ultimate goal: {system_goal}\n\n"
            "Analyze the RECENT CHAT HISTORY, the PROJECT_STATE, and the overarching MANDATE. "
            "Determine exactly what phase or task the agent just completed, and synthesize a "
            "SINGLE, DIRECT, AUTHORITATIVE instruction telling the agent what to build, test, or "
            "fix NEXT to move the project closer to 100% completion.\n\n"
            "RULES:\n"
            "1. Do NOT include pleasantries, explanations, or quotes.\n"
            "2. Ensure the instruction aligns with Total Automation (the agent must test its own work).\n"
            "3. If the chat history shows a completion message, assign the next phase.\n"
            "4. Reply ONLY with a JSON object in this exact format:\n"
            '{"instruction": "the direct command to the agent"}'
        )
        
        user_prompt = f"### RECENT CHAT HISTORY:\n{chat_history}\n\n"
        if mandate:
            user_prompt += f"### SUPERVISOR MANDATE:\n{mandate[:1500]}\n\n"
        if project_state:
            user_prompt += f"### PROJECT STATE.md:\n{project_state[:1500]}\n\n"
        
        try:
            response_json = await self.ask_local_model(system_prompt, user_prompt, temperature=0.2)
            data = json.loads(response_json)
            instruction = data.get("instruction", "")
            if instruction:
                return instruction
        except Exception as exc:
            logger.error("Failed to parse synthesized follow-up from local LLM: %s", exc)
            
        # V16: FATAL if the local layer is broken — DO NOT FALLBACK SILENTLY.
        # This forces the operator (or self-evolution) to fix the local model pipeline.
        print(f"\n  {config.ANSI_RED}=====================================================================")
        print("  CRITICAL ERROR: Local LLM failed to synthesize follow-up prompt.")
        print("  The supervisor's reasoning layer is offline or hallucinating.")
        print(f"  ====================================================================={config.ANSI_RESET}\n")
        raise RuntimeError("Local LLM Synthesis Engine Offline. Cannot proceed gracefully.")
