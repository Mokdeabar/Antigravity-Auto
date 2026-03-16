import re

html_path = r'c:\Users\mokde\Desktop\Experiments\Antigravity Auto\supervisor\ui\index.html'

with open(html_path, 'r', encoding='utf-8') as f:
    text = f.read()

replacements = [
    (r'🔌 Connected to Supervisor AI', r'[SYS] UPLINK ESTABLISHED'),
    (r'🔌', r''),
    (r'📬 Queued:', r'[Q] QUEUED:'),
    (r'🛑 Safe stop acknowledged — saving checkpoint…', r'[SYS] HALT SIGNAL ACKNOWLEDGED — SAVING STATE...'),
    (r'🛑 Stopping…', r'[ SHUTTING DOWN... ]'),
    (r'🛑 Stop requested — saving checkpoint…', r'[SYS] HALT REQUESTED — SAVING STATE...'),
    (r'🛑 Stop', r'[ HALT ]'),
    (r'✅ Ready to Close', r'[ SAFE TO TERMINATE ]'),
    (r'✅ Created:', r'[SYS] WORKSPACE CREATED:'),
    (r'🚀 Launched:', r'[SYS] UPLINK INITIALIZED:'),
    (r'👁️ Preview updated →', r'[SYS] RENDER_BUFFER UPDATED →'),
    (r'↻ Preview auto-refreshed \(files changed\)', r'[SYS] RENDER_BUFFER AUTO-REFRESHED'),
    (r'🛠️ Manually requeued task:', r'[SYS] MANUAL_OVERRIDE REQUEUED TASK:'),
    (r'❌ Failed to requeue', r'[ERR] FAILED TO REQUEUE'),
    (r'❌ API Error:', r'[ERR] API_FAILURE:'),
    (r'❌ Network error:', r'[ERR] NET_FAILURE:'),
    (r'⚠️ Please enter a goal', r'[WARN] OBJECTIVE DIRECTIVE REQUIRED'),
    (r'⚠️ Enter a project name', r'[WARN] WORKSPACE IDENTIFIER REQUIRED'),
    (r'⚠️ ', r'[WARN] '),
    (r'⏳ Launching…', r'[ INIT... ]'),
    (r'▶ Launch', r'[ EXECUTE ]'),
    (r'▶️ <b>Resume available</b>', r'[ RECOVERABLE STATE DETECTED ]'),
    (r'✅', r'[OK]'),
    (r'❌', r'[ERR]'),
    (r'○', r'[-]'),
    (r'⏳', r'[...]'),
    (r'✓', r'[*]'),
    (r'✗', r'[X]'),
    (r'✔️', r'[*]'),
    (r'⚡', r'//'),
    (r'🌐', r'//'),
    (r'🎨', r'//'),
    (r'🐍', r'//'),
    (r'⚙️', r'//'),
    (r'📝', r'//'),
    (r'📄', r'//'),
    (r'>🔌<', r'><'),
    (r'<span class="icon">🧩</span> Tasks appear when the AI decomposes your goal…', r'BUILDING DIRECTED ACYCLIC GRAPH...'),
    (r'<span class="icon">📝</span> File changes appear here as the AI works…', r'AWAITING FILE MUTATIONS...'),
    (r'<span class="icon">📝</span> No changes recorded yet', r'NO FILE MUTATIONS DETECTED'),
    (r'<span class="icon">📝</span> No changes for this view', r'NO MUTATIONS FOR CURRENT FILTER'),
    (r'<span class="icon">⏳</span> Events appear as the AI works…', r'AWAITING TELEMETRY...'),
    (r'📋 Change History', r'// CHRONOLOGICAL MUTATION LOG'),
    (r'📂 All Files Touched', r'// FLAT FILE MUTATION INDEX'),
    (r'Supervisor engine not running', r'SUPERVISOR ENGINE OFFLINE'),
    (r'Start the engine first:<br>', r'INITIATE ENGINE EXECUTABLE FIRST:<br>'),
]

for old, new in replacements:
    text = re.sub(old, new, text)

# Remove the animated hourglass and logs icon classes
text = text.replace(r'<span class="spinner-anim">&#x23F3;</span> ', r'[~] ')

with open(html_path, 'w', encoding='utf-8') as f:
    f.write(text)

print("JS strings patched!")
