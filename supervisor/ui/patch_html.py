import re

html_path = r'c:\Users\mokde\Desktop\Experiments\Antigravity Auto\supervisor\ui\index.html'

with open(html_path, 'r', encoding='utf-8') as f:
    full_text = f.read()

# Replace body contents from <div class="launcher-screen"... to </div> \n <!-- Toast -->
# To be safe, look for exactly <!-- ════════════ ... --> up to <!-- Toast -->
# Use Regex with re.DOTALL

launcher_html = """    <!-- ══════════════════════════════════════════════════════
         SCREEN 1: PROJECT MATRIX (V40 TACTICAL)
         ══════════════════════════════════════════════════════ -->
    <div class="launcher-screen" id="launcher-screen">
        <header class="header">
            <div class="header-left">
                <span class="logo">
                    <svg class="icon-svg" viewBox="0 0 24 24"><path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/></svg>
                    SUPERVISOR
                </span>
                <span class="version-badge">SYS.V74</span>
            </div>
            <div class="connection-status">
                <span class="status-dot" id="conn-dot-launcher"></span>
                <span id="conn-text-launcher">Uplink Pend...</span>
            </div>
        </header>

        <div class="launcher-body">
            <div class="launcher-hero">
                <h1>Command Centre</h1>
                <p>Select a workspace to initiate the autonomous uplink.<br>The system will compile, execute, and monitor the designated objective.</p>
            </div>

            <!-- Create New -->
            <div class="create-section">
                <input type="text" class="create-input" id="new-project-name" placeholder="INIT.NEW_WORKSPACE..." autocomplete="off">
                <button class="create-btn" onclick="createProject()">[+] Instantiate</button>
            </div>

            <div class="projects-label" id="projects-label">Scanning local storage...</div>
            <div class="projects-grid" id="projects-grid">
                <div class="empty-state">
                    <svg class="icon-svg" style="font-size:2rem;margin-bottom:8px;" viewBox="0 0 24 24"><path d="M12 2v20M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></svg><br>
                    SCANNING WORKSPACE REGISTRY
                </div>
            </div>

            <!-- Launch Panel -->
            <div class="launch-panel" id="launch-panel">
                <h3>
                    <svg class="icon-svg" style="color:var(--accent-indigo)" viewBox="0 0 24 24"><path fill="currentColor" d="M14 2H6c-1.1 0-1.99.9-1.99 2L4 20c0 1.1.89 2 1.99 2H18c1.1 0 2-.9 2-2V8l-6-6zM6 20V4h7v5h5v11H6z"/></svg>
                    <span id="launch-project-name">---</span>
                </h3>
                <div class="launch-field">
                    <label>// OBJECTIVE DIRECTIVE</label>
                    <textarea id="launch-goal" placeholder="ENTER PRIMARY MISSION PARAMETERS..." rows="3"></textarea>
                </div>
                <div class="launch-field">
                    <label>// PRE-FLIGHT (OPTIONAL)</label>
                    <textarea id="launch-instructions" placeholder="ENTER INSTRUCTION OVERRIDES..." rows="2"></textarea>
                </div>
                <div class="launch-actions">
                    <button class="btn-cancel" onclick="cancelLaunch()">[ ABORT ]</button>
                    <button class="btn-launch" id="btn-launch" onclick="launchProject()">[ EXECUTE ]</button>
                </div>
            </div>
        </div>
    </div>
"""

dashboard_html = """    <!-- ══════════════════════════════════════════════════════
         SCREEN 2: LIVING DASHBOARD (V40 TACTICAL)
         ══════════════════════════════════════════════════════ -->
    <div class="dashboard-screen" id="dashboard-screen">
        <canvas id="particle-canvas" style="position:fixed;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:0;opacity:0.3;"></canvas>

        <header class="header" style="position:relative;z-index:2;">
            <div class="header-left">
                <span class="logo">
                    <svg class="icon-svg" viewBox="0 0 24 24"><path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5"/></svg>
                    SUPERVISOR
                </span>
                <span class="version-badge">SYS.V74</span>
                <span class="stat-pill" id="pill-status">STATUS: ---</span>
                <span class="stat-pill" id="pill-uptime">UPTIME: 0s</span>
                <span class="stat-pill" id="pill-model">LLM: ---</span>
            </div>
            <div class="connection-status">
                <span class="status-dot" id="conn-dot"></span>
                <span id="conn-text">Uplink Pend...</span>
                <button class="stop-btn" id="stop-btn" onclick="requestStop()">[ STOP_SIG ]</button>
            </div>
        </header>

        <main class="main" style="position:relative;z-index:1;">
            <!-- Content Left -->
            <div class="content-column">
                <div class="tab-bar" id="tab-bar">
                    <button class="tab active" data-tab="brain" onclick="switchTab('brain')">SYS.LOG</button>
                    <button class="tab" data-tab="changes" onclick="switchTab('changes')">DIFF<span class="tab-badge" id="badge-changes"></span></button>
                    <button class="tab" data-tab="tasks" onclick="switchTab('tasks')">DAG<span class="tab-badge" id="badge-tasks"></span></button>
                    <button class="tab" data-tab="preview" onclick="switchTab('preview')">VIEW<span class="tab-badge" id="badge-preview"></span></button>
                    <button class="tab" data-tab="timeline" onclick="switchTab('timeline')">TIME<span class="tab-badge" id="badge-timeline"></span></button>
                </div>

                <div class="content-area">
                    <!-- TAB: LOG (Brain) -->
                    <div class="tab-panel active" id="panel-brain">
                        <div class="panel-header">
                            <span class="panel-title">// CORE STREAM</span>
                            <div style="display:flex;gap:4px;align-items:center;">
                                <input type="text" class="log-filter" id="log-filter" placeholder="FILTER_LOGS..." oninput="filterLogs()">
                                <span style="font-family:var(--font-mono);font-size:0.65rem;color:var(--text-dim);margin-right:8px;" id="log-count">0L</span>
                                <button onclick="copyAllLogs()" class="mini-btn">COPY</button>
                                <button onclick="clearLogs()" class="mini-btn">CLEAR</button>
                            </div>
                        </div>
                        <div class="terminal" id="terminal"></div>
                    </div>
                    <!-- TAB: CHANGES -->
                    <div class="tab-panel" id="panel-changes">
                        <div class="panel-header">
                            <span class="panel-title">// FILE MUTATIONS</span>
                            <div style="display:flex;gap:4px;">
                                <button class="mini-btn active" id="btn-proj-changes" onclick="showChangeView('project')">PROJ</button>
                                <button class="mini-btn" id="btn-sup-changes" onclick="showChangeView('supervisor')">SYS</button>
                            </div>
                        </div>
                        <div class="changes-content" id="changes-content">
                            <div class="empty-state">AWAITING FILE MUTATIONS...</div>
                        </div>
                    </div>
                    <!-- TAB: TASKS -->
                    <div class="tab-panel" id="panel-tasks">
                        <div class="panel-header">
                            <span class="panel-title">// EXECUTION GRAPH</span>
                            <span id="tasks-progress" style="font-size:0.65rem;font-family:var(--font-mono);color:var(--text-dim);"></span>
                        </div>
                        <div class="task-list" id="task-list">
                            <div class="empty-state">BUILDING DIRECTED ACYCLIC GRAPH...</div>
                        </div>
                    </div>
                    <!-- TAB: PREVIEW -->
                    <div class="tab-panel" id="panel-preview">
                        <div class="panel-header">
                            <span class="panel-title">// RENDER BUFFER <span id="preview-status" style="margin-left:8px;color:var(--text-dim);">[OFFLINE]</span></span>
                            <div style="display:flex;gap:4px;align-items:center;">
                                <span class="status-dot" id="preview-dot" style="margin-right:8px;"></span>
                                <button onclick="reloadPreview()" class="mini-btn">RELOAD</button>
                                <button onclick="openPreviewTab()" class="mini-btn">UNDOCK</button>
                            </div>
                        </div>
                        <div id="preview-container">
                            <div class="preview-placeholder">
                                <svg class="icon-svg" style="font-size:3rem;margin-bottom:16px;opacity:0.3;" viewBox="0 0 24 24"><path fill="currentColor" d="M21 2H3c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h7v2H8v2h8v-2h-2v-2h7c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm0 14H3V4h18v12z"/></svg>
                                AWAITING DEV_SERVER PORT MAPPING...
                                <span style="font-size:0.65rem;color:var(--text-dim);margin-top:12px;" id="preview-port-info">PORT: NONE</span>
                            </div>
                        </div>
                    </div>
                    <!-- TAB: TIMELINE -->
                    <div class="tab-panel" id="panel-timeline">
                        <div class="panel-header">
                            <span class="panel-title">// EVENT CHRONICLE <span id="timeline-count" style="margin-left:8px;color:var(--text-dim);">0</span></span>
                        </div>
                        <div class="timeline-content" id="timeline-content">
                            <div class="empty-state">AWAITING TELEMETRY...</div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Sidebar -->
            <div class="sidebar">
                <div class="sidebar-section">
                    <div class="sidebar-label">DIR.OBJ //</div>
                    <div class="sidebar-value" id="s-goal">---</div>
                    <div class="sidebar-value" id="s-last-action" style="margin-top:8px;font-size:0.7rem;color:var(--accent-cyan);display:none;"></div>
                </div>

                <div class="sidebar-section">
                    <div class="sidebar-label">TELEMETRY //</div>
                    <div class="metric-grid">
                        <div class="metric-box" onclick="switchTab('changes')" style="cursor:pointer">
                            <div class="metric-val" id="cnt-files">0</div><div class="metric-lbl">MUTATIONS</div>
                        </div>
                        <div class="metric-box" onclick="switchTab('tasks')" style="cursor:pointer">
                            <div class="metric-val" id="cnt-tasks">0</div><div class="metric-lbl">DAG.COMP</div>
                        </div>
                        <div class="metric-box" onclick="switchTab('brain')" style="cursor:pointer">
                            <div class="metric-val" id="cnt-errors">0</div><div class="metric-lbl">ERRORS</div>
                        </div>
                        <div class="metric-box">
                            <div class="metric-val" id="cnt-loops">0</div><div class="metric-lbl">CYCLES</div>
                        </div>
                    </div>
                </div>

                <div class="sidebar-section" id="health-card">
                    <div class="sidebar-label">SYS.HEALTH //</div>
                    <div class="health-row"><span>CPU_UTIL</span><span class="health-val" id="val-cpu">0%</span></div>
                    <div class="health-row"><span>MEM_UTIL</span><span class="health-val" id="val-mem">0%</span></div>
                    <div class="health-row"><span>CONTAINER</span><span class="status-dot" id="health-docker"></span></div>
                    <div class="health-row"><span>LLM.LOCAL</span><span class="status-dot" id="health-ollama"></span></div>
                    <div id="health-meta" style="font-size:0.6rem;color:var(--text-dim);margin-top:8px;display:none;"></div>
                </div>

                <div class="sidebar-section dag-panel" id="dag-panel" style="display:none;">
                    <div class="sidebar-label">DAG.STATE // <span id="dag-status" style="float:right;">---</span></div>
                    <div class="dag-bar"><div class="dag-fill" id="dag-fill"></div></div>
                    <div id="dag-summary" style="font-size:0.6rem;color:var(--text-dim);margin-top:4px;"></div>
                    <div id="dag-lanes" style="font-size:0.6rem;color:var(--accent-cyan);margin-top:4px;"></div>
                </div>
            </div>
        </main>

        <footer class="footer" style="position:relative;z-index:2;">
            <div class="cli-prompt">&gt;_</div>
            <input type="text" class="instruction-input" id="instruction-input" placeholder="AWAITING MANUAL OVERRIDE..." autocomplete="off">
            <button class="send-btn" id="send-btn" onclick="sendInstruction()">[ TRANSMIT ]</button>
            <div id="queue-indicator" style="display:none;"></div>
        </footer>
    </div>
"""

pattern = r'<!-- ══════════════════════════════════════════════════════\n         SCREEN 1: PROJECT LAUNCHER.*?<!-- Toast -->'

replacement = launcher_html + '\n' + dashboard_html + '\n    <!-- Toast -->'

new_html, count = re.subn(pattern, replacement, full_text, flags=re.DOTALL)

with open(html_path, 'w', encoding='utf-8') as f:
    f.write(new_html)

print(f"Replaced HTML sections. Matches: {count}")
