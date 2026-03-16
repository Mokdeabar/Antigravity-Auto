import re

html_path = r'c:\Users\mokde\Desktop\Experiments\Antigravity Auto\supervisor\ui\index.html'

with open(html_path, 'r', encoding='utf-8') as f:
    html = f.read()

new_body = """<body>

    <!-- ══════════════════════════════════════════════════════
         SCREEN 1: PROJECT MATRIX (CUPERTINO)
         ══════════════════════════════════════════════════════ -->
    <div class="launcher-screen" id="launcher-screen">
        <header class="header">
            <div class="header-left">
                <span class="logo">
                    <svg class="icon-svg" viewBox="0 0 24 24" style="color:var(--accent-blue);">
                        <path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5" />
                    </svg>
                    Supervisor
                </span>
                <span class="version-badge">v74</span>
            </div>
            <div class="connection-status">
                <span class="status-dot" id="conn-dot-launcher"></span>
                <span id="conn-text-launcher">Connecting...</span>
            </div>
        </header>

        <div class="launcher-body">
            <div class="launcher-hero">
                <h1>Welcome to Supervisor</h1>
                <p>Select a workspace to begin an autonomous session. The system will compile, execute, and monitor your objective seamlessly.</p>
            </div>

            <!-- Create New -->
            <div class="create-section">
                <input type="text" class="create-input" id="new-project-name" placeholder="Name your new workspace..."
                    autocomplete="off">
                <button class="create-btn" onclick="createProject()">Create Project</button>
            </div>

            <div class="projects-label" id="projects-label">Recent Workspaces</div>
            <div class="projects-grid" id="projects-grid">
                <div class="empty-state">
                    <svg class="icon-svg" style="font-size:2.5rem;margin-bottom:12px;color:var(--text-tertiary);" viewBox="0 0 24 24">
                        <path fill="currentColor" d="M12 2v20M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6" />
                    </svg><br>
                    Scanning Workspaces
                </div>
            </div>

            <!-- Launch Panel -->
            <div class="launch-panel" id="launch-panel">
                <h3>
                    <svg class="icon-svg" style="color:var(--accent-blue)" viewBox="0 0 24 24">
                        <path fill="currentColor"
                            d="M14 2H6c-1.1 0-1.99.9-1.99 2L4 20c0 1.1.89 2 1.99 2H18c1.1 0 2-.9 2-2V8l-6-6zM6 20V4h7v5h5v11H6z" />
                    </svg>
                    <span id="launch-project-name">---</span>
                </h3>
                <div class="launch-field">
                    <label>Project Directive</label>
                    <textarea id="launch-goal" placeholder="What would you like the system to accomplish?" rows="3"></textarea>
                </div>
                <div class="launch-field">
                    <label>Pre-flight Instructions (Optional)</label>
                    <textarea id="launch-instructions" placeholder="Provide any constraints or initial thoughts..." rows="2"></textarea>
                </div>
                <div class="launch-actions">
                    <button class="btn-cancel" onclick="cancelLaunch()">Cancel</button>
                    <button class="btn-launch" id="btn-launch" onclick="launchProject()">Start Session</button>
                </div>
            </div>
        </div>
    </div>

    <!-- ══════════════════════════════════════════════════════
         SCREEN 2: DASHBOARD
         ══════════════════════════════════════════════════════ -->
    <div class="dashboard-screen" id="dashboard-screen">

        <header class="header" style="position:relative;z-index:2;">
            <div class="header-left">
                <span class="logo">
                    <svg class="icon-svg" viewBox="0 0 24 24" style="color:var(--accent-blue);">
                        <path d="M12 2L2 7l10 5 10-5-10-5zM2 17l10 5 10-5M2 12l10 5 10-5" />
                    </svg>
                    Supervisor
                </span>
                <span class="version-badge">v74</span>
                <span class="stat-pill" id="pill-status">Status: ---</span>
                <span class="stat-pill" id="pill-uptime">Uptime: 0s</span>
                <span class="stat-pill" id="pill-model">LLM: ---</span>
            </div>
            <div class="connection-status">
                <span class="status-dot" id="conn-dot"></span>
                <span id="conn-text">Connecting...</span>
                <button class="stop-btn" id="stop-btn" onclick="requestStop()">Stop</button>
            </div>
        </header>

        <main class="main" style="position:relative;z-index:1;">
            <!-- Content Left -->
            <div class="content-column">
                <div class="tab-bar" id="tab-bar">
                    <button class="tab active" data-tab="brain" onclick="switchTab('brain')">Logs</button>
                    <button class="tab" data-tab="changes" onclick="switchTab('changes')">Changes<span class="tab-badge"
                            id="badge-changes"></span></button>
                    <button class="tab" data-tab="tasks" onclick="switchTab('tasks')">Graph<span class="tab-badge"
                            id="badge-tasks"></span></button>
                    <button class="tab" data-tab="preview" onclick="switchTab('preview')">Preview<span class="tab-badge"
                            id="badge-preview"></span></button>
                    <button class="tab" data-tab="timeline" onclick="switchTab('timeline')">Timeline<span class="tab-badge"
                            id="badge-timeline"></span></button>
                </div>

                <div class="content-area">
                    <!-- TAB: LOG (Brain) -->
                    <div class="tab-panel active" id="panel-brain">
                        <div class="panel-header">
                            <span class="panel-title">Activity Stream</span>
                            <div style="display:flex;gap:8px;align-items:center;">
                                <input type="text" class="log-filter" id="log-filter" placeholder="Filter logs..."
                                    oninput="filterLogs()">
                                <span
                                    style="font-family:var(--font-mono);font-size:0.75rem;color:var(--text-tertiary);margin-right:8px;"
                                    id="log-count">0 Lines</span>
                                <button onclick="copyAllLogs()" class="mini-btn">Copy</button>
                                <button onclick="clearLogs()" class="mini-btn">Clear</button>
                            </div>
                        </div>
                        <div class="terminal" id="terminal"></div>
                    </div>
                    <!-- TAB: CHANGES -->
                    <div class="tab-panel" id="panel-changes">
                        <div class="panel-header">
                            <span class="panel-title">File Changes</span>
                            <div style="display:flex;gap:4px;">
                                <button class="mini-btn active" id="btn-proj-changes"
                                    onclick="showChangeView('project')">Project</button>
                                <button class="mini-btn" id="btn-sup-changes"
                                    onclick="showChangeView('supervisor')">System</button>
                            </div>
                        </div>
                        <div class="changes-content" id="changes-content">
                            <div class="empty-state">No files have been modified yet.</div>
                        </div>
                    </div>
                    <!-- TAB: TASKS -->
                    <div class="tab-panel" id="panel-tasks">
                        <div class="panel-header">
                            <span class="panel-title">Execution Graph</span>
                            <span id="tasks-progress"
                                style="font-size:0.75rem;font-family:var(--font-mono);color:var(--text-tertiary);"></span>
                        </div>
                        <div class="task-list" id="task-list">
                            <div class="empty-state">Building directed acyclic graph...</div>
                        </div>
                    </div>
                    <!-- TAB: PREVIEW -->
                    <div class="tab-panel" id="panel-preview">
                        <div class="panel-header">
                            <span class="panel-title">Live Preview <span id="preview-status"
                                    style="margin-left:8px;color:var(--text-tertiary);font-weight:400;">Offline</span></span>
                            <div style="display:flex;gap:8px;align-items:center;">
                                <span class="status-dot" id="preview-dot" style="margin-right:8px;"></span>
                                <button onclick="reloadPreview()" class="mini-btn">Reload</button>
                                <button onclick="openPreviewTab()" class="mini-btn">Open in Browser</button>
                            </div>
                        </div>
                        <div id="preview-container">
                            <div class="preview-placeholder">
                                <svg class="icon-svg" style="font-size:3rem;margin-bottom:16px;opacity:0.3;color:var(--text-primary);"
                                    viewBox="0 0 24 24">
                                    <path fill="currentColor"
                                        d="M21 2H3c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h7v2H8v2h8v-2h-2v-2h7c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm0 14H3V4h18v12z" />
                                </svg>
                                Awaiting Dev Server
                                <span style="font-size:0.75rem;color:var(--text-tertiary);margin-top:12px;font-family:var(--font-mono);"
                                    id="preview-port-info">Port: None</span>
                            </div>
                        </div>
                    </div>
                    <!-- TAB: TIMELINE -->
                    <div class="tab-panel" id="panel-timeline">
                        <div class="panel-header">
                            <span class="panel-title">Event Chronicle <span id="timeline-count"
                                    style="margin-left:8px;color:var(--text-tertiary);font-weight:400;">0</span></span>
                        </div>
                        <div class="timeline-content" id="timeline-content">
                            <div class="empty-state">Awaiting telemetry data...</div>
                        </div>
                    </div>
                </div>
            </div>

            <!-- Sidebar -->
            <div class="sidebar">
                <div class="sidebar-section">
                    <div class="sidebar-label">Objective</div>
                    <div class="sidebar-value" id="s-goal">---</div>
                    <div class="sidebar-value" id="s-last-action"
                        style="margin-top:12px;font-size:0.8rem;color:var(--accent-blue);font-weight:500;display:none;"></div>
                </div>

                <div class="sidebar-section">
                    <div class="sidebar-label">Telemetry</div>
                    <div class="metric-grid">
                        <div class="metric-box" onclick="switchTab('changes')" style="cursor:pointer">
                            <div class="metric-val" id="cnt-files">0</div>
                            <div class="metric-lbl">Changes</div>
                        </div>
                        <div class="metric-box" onclick="switchTab('tasks')" style="cursor:pointer">
                            <div class="metric-val" id="cnt-tasks">0</div>
                            <div class="metric-lbl">DAG Nodes</div>
                        </div>
                        <div class="metric-box" onclick="switchTab('brain')" style="cursor:pointer">
                            <div class="metric-val" id="cnt-errors">0</div>
                            <div class="metric-lbl">Errors</div>
                        </div>
                        <div class="metric-box">
                            <div class="metric-val" id="cnt-loops">0</div>
                            <div class="metric-lbl">Cycles</div>
                        </div>
                    </div>
                </div>

                <div class="sidebar-section" id="health-card">
                    <div class="sidebar-label">System Health</div>
                    <div class="health-row"><span>CPU</span><span class="health-val" id="val-cpu">0%</span></div>
                    <div class="health-row"><span>Memory</span><span class="health-val" id="val-mem">0%</span></div>
                    <div class="health-row"><span>Container</span><span class="status-dot" id="health-docker"></span>
                    </div>
                    <div class="health-row"><span>Local LLM</span><span class="status-dot" id="health-ollama"></span>
                    </div>
                    <div id="health-meta" style="font-size:0.75rem;color:var(--text-tertiary);margin-top:12px;display:none;">
                    </div>
                </div>

                <div class="sidebar-section dag-panel" id="dag-panel" style="display:none;">
                    <div class="sidebar-label" style="display:flex;justify-content:space-between;align-items:center;">Execution State <span id="dag-status" style="font-family:var(--font-mono);color:var(--text-secondary);font-size:0.75rem;">---</span></div>
                    <div class="dag-bar">
                        <div class="dag-fill" id="dag-fill"></div>
                    </div>
                    <div id="dag-summary" style="font-size:0.75rem;color:var(--text-secondary);margin-top:8px;"></div>
                    <div id="dag-lanes" style="font-size:0.75rem;color:var(--accent-blue);margin-top:4px;font-weight:500;"></div>
                    <div id="dag-nodes" style="display:flex;flex-wrap:wrap;gap:8px;margin-top:12px;"></div>
                </div>
            </div>
        </main>

        <footer class="footer">
            <div class="cli-prompt">Prompt</div>
            <input type="text" class="instruction-input" id="instruction-input"
                placeholder="Enter message..." autocomplete="off">
            <button class="send-btn" id="send-btn" onclick="sendInstruction()">Send</button>
            <div id="queue-indicator" style="display:none;"></div>
        </footer>
    </div>

    <!-- Toast -->
    <div class="toast" id="toast"></div>"""

html = re.sub(r'<body>.*<!-- ═══════════════════════════════════════════════════════\n         JavaScript', f'{new_body}\n\n    <!-- ═══════════════════════════════════════════════════════\n         JavaScript', html, flags=re.DOTALL)

with open(html_path, 'w', encoding='utf-8') as f:
    f.write(html)

print("Successfully replaced HTML in index.html with Cupertino Strings")
