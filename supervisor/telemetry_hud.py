"""
telemetry_hud.py — V12 Live Telemetry HUD.

Generates a beautifully formatted, auto-updating Markdown file inside the
workspace `.ag-supervisor/LIVE_HUD.md`. This file allows the user to watch
the internal brain and state of the Supervisor AI in real-time by keeping
the file open in a split pane in VS Code.
"""

import json
import logging
import time
from datetime import datetime
from pathlib import Path

from . import config
from .session_memory import SessionMemory
from .scheduler import CronScheduler

logger = logging.getLogger("supervisor.telemetry_hud")

def update_hud() -> str:
    """
    Compile all active statistics and overwrite `.ag-supervisor/LIVE_HUD.md`.
    Designed to be called rapidly by the CronScheduler.
    """
    try:
        project_path = config.get_project_path()
        if not project_path:
            return "No project path, skipping HUD update."
            
        state_dir = Path(project_path) / ".ag-supervisor"
        hud_file = state_dir / "LIVE_HUD.md"
        
        # Gather data
        mem = SessionMemory()
        
        # 1. Core Vitals
        status = mem.last_agent_status
        status_icon = "🟢" if status == "WORKING" else "🟡" if status == "WAITING" else "🔴"
        duration = mem.session_duration_minutes
        
        # 2. Omniscient Eye Status
        map_file = state_dir / "workspace_map.json"
        index_count = 0
        if map_file.exists():
            try:
                wm_data = json.loads(map_file.read_text(encoding="utf-8"))
                index_count = len(wm_data)
            except Exception:
                pass
                
        # 3. Model Failover Strategy
        active_model = "Unknown"
        try:
            from .retry_policy import get_failover_chain
            active_model = get_failover_chain().get_status()["active_model"]
        except Exception:
            pass
            
        # 4. Agent Council KB
        kb_file = state_dir / "council_knowledge.json"
        kb_count = 0
        if kb_file.exists():
            try:
                kb_data = json.loads(kb_file.read_text(encoding="utf-8"))
                kb_count = len(kb_data.get("resolutions", []))
            except Exception:
                pass

        # Build Markdown
        lines = [
            f"# 🧠 Supervisor V12 Flagship HUD",
            f"> Last updated: {datetime.now().strftime('%H:%M:%S')} (Auto-refreshes)",
            "",
            "## 🫀 Core Vitals",
            f"- **State:** {status_icon} `{status}`",
            f"- **Uptime:** `{duration:.1f} minutes`",
            f"- **Active Model:** `{active_model}`",
            "",
            "## 👁️ The Omniscient Eye (AST RAG)",
            f"- **Files Indexed:** `{index_count}`",
            "- **Status:** `ONLINE (Background Thread)`",
            "",
            "## 📚 Memory Cortex",
            # V37 FIX (L-7): Use public API instead of accessing private _data dict.
            f"- **Events in RAM:** `{mem.get_event_count()}`",
            f"- **Compaction Cycles:** `{mem.get_counter('compactions')}`",
            f"- **Council KB Entries:** `{kb_count}`",
            "",
            "## ⚡ Operational Metrics",
            f"- **Approvals:** `{mem.total_approvals}`",
            f"- **Vision Calls:** `{mem.get_counter('vision_calls')}`",
            f"- **Errors Crushed:** `{mem.get_counter('errors_resolved')}`",
        ]
        
        # Inject recent activity log
        events = mem.get_recent_events(5)
        if events:
            lines.extend([
                "",
                "## ⏱️ Live Action Log",
            ])
            for e in reversed(events):
                ts = datetime.fromtimestamp(e.get("timestamp", 0)).strftime('%H:%M:%S')
                etype = e.get("type", "unknown")
                detail = e.get("detail", "")[:80]
                lines.append(f"`[{ts}]` **{etype}** - {detail}")

        # Write atomically
        tmp_file = hud_file.with_suffix('.tmp')
        tmp_file.write_text("\n".join(lines), encoding="utf-8")
        tmp_file.replace(hud_file)
        
        return "HUD updated"
        
    except Exception as exc:
        logger.debug("Failed to update HUD: %s", exc)
        return f"HUD update failed: {exc}"
