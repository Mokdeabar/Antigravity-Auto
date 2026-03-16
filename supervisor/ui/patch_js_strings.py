import re

html_path = r'c:\Users\mokde\Desktop\Experiments\Antigravity Auto\supervisor\ui\index.html'

with open(html_path, 'r', encoding='utf-8') as f:
    text = f.read()

replacements = {
    r"addLogLine\('INFO', '\[SYS\] UPLINK ESTABLISHED'\);": r"addLogLine('INFO', 'Connection established.');",
    r"const label = ok \? 'Connected' : \(wsRetryCount > 1 \? `Server offline — start with: python -m supervisor` : 'Connecting…'\);": r"const label = ok ? 'Connected' : (wsRetryCount > 1 ? `Offline - Run python -m supervisor` : 'Connecting…');",
    r"addLogLine\('INFO', `\[Q\] QUEUED: \"\${data\.instruction\.text}\"`\);": r"addLogLine('INFO', `Queued: \"${data.instruction.text}\"`);",
    r"addLogLine\('WARNING', '\[SYS\] HALT SIGNAL ACKNOWLEDGED — SAVING STATE\.\.\.'\);": r"addLogLine('WARNING', 'Stopping session — saving state...');",
    r"btn\.textContent = '\[ SHUTTING DOWN\.\.\. \]';": r"btn.textContent = 'Shutting Down...';",
    r"stopBtn\.textContent = '\[ SAFE TO TERMINATE \]';": r"stopBtn.textContent = 'Safe to Close';",
    r"stopBtn\.textContent = '\[ SHUTTING DOWN\.\.\. \] Please wait';": r"stopBtn.textContent = 'Shutting Down... Please wait';",
    r"const icon = n\.status === 'complete' \? '\[\*\]' : n\.status === 'running' \? '\[\.\.\.\]' : n\.status === 'failed' \? '\[X\]' : '·';": r"const icon = n.status === 'complete' ? '✓' : n.status === 'running' ? '⟳' : n.status === 'failed' ? '✕' : '·';",
    r"icon = '\[OK\]';": r"icon = '✓';",
    r"icon = '\[\.\.\.\]';": r"icon = '⟳';",
    r"icon = '\[ERR\]';": r"icon = '✕';",
    r"icon = '⏸️';": r"icon = '⏸';",
    r"icon = '\[-\]';": r"icon = '·';",
    r"addLogLine\('INFO', `\[SYS\] MANUAL_OVERRIDE REQUEUED TASK: \${taskId}`\);": r"addLogLine('INFO', `Task requeued manually: ${taskId}`);",
    r"addLogLine\('ERROR', `\[ERR\] FAILED TO REQUEUE \${taskId}: \${data\.error}`\);": r"addLogLine('ERROR', `Failed to requeue ${taskId}: ${data.error}`);",
    r"// CHRONOLOGICAL MUTATION LOG": "Recent Changes",
    r"// FLAT FILE MUTATION INDEX": "Tracked Files",
    r"<div class=\"empty-state\"><span class=\"icon\">//</span> File changes appear here as the AI works…</div>": r"<div class=\"empty-state\">File changes will appear here...</div>",
    r"<div class=\"empty-state\"><span class=\"icon\">\[\.\.\.\]</span> Events appear as the AI works…</div>": r"<div class=\"empty-state\">System events will appear here...</div>",
    r"const icon = typeIcons\[ev\.type\] \|\| '▸';": r"const icon = typeIcons[ev.type] || '•';",
    r"addLogLine\('INFO', `\[SYS\] RENDER_BUFFER UPDATED → localhost:\${port}`\);": r"addLogLine('INFO', `Preview updated: localhost:${port}`);",
    r"\[ RECOVERABLE STATE DETECTED \]": "Recoverable Session Found",
    r"showToast\('\[WARN\] OBJECTIVE DIRECTIVE REQUIRED'\);": r"showToast('Project directive is required.');",
    r"\('btn-launch'\)\.textContent = '\[ INIT\.\.\. \]';": r"('btn-launch').textContent = 'Initializing...';",
    r"showToast\(`\[SYS\] UPLINK INITIALIZED: \${selectedProject\.name}`\);": r"showToast(`Session started: ${selectedProject.name}`);",
    r"\('btn-launch'\)\.textContent = '\[ EXECUTE \]';": r"('btn-launch').textContent = 'Start Session';",
    r"showToast\('\[WARN\] WORKSPACE IDENTIFIER REQUIRED'\);": r"showToast('Workspace name is required.');",
    r"showToast\(`\[SYS\] WORKSPACE CREATED: \${data\.name}`\);": r"showToast(`Workspace created: ${data.name}`);",
    r"showToast\(`\[WARN\] \${err\.error \|\| 'Launch failed'}`\);": r"showToast(`${err.error || 'Launch failed'}`);",
    r"showToast\(`\[ERR\] NET_FAILURE: \${e\.message}`\);": r"showToast(`Network error: ${e.message}`);",
    r"showToast\(`\[WARN\] \${err\.error}`\);": r"showToast(`${err.error}`);",
    r"showToast\(`\[ERR\] \${e\.message}`\);": r"showToast(`Error: ${e.message}`);",
    r"btn\.textContent = '\[ HALT \]';": r"btn.textContent = 'Stop';",
    r"showToast\('\[SYS\] HALT REQUESTED — SAVING STATE\.\.\.'\);": r"showToast('Stop requested — saving state...');"
}

for old, new_ in replacements.items():
    text = re.sub(old, new_, text)

with open(html_path, 'w', encoding='utf-8') as f:
    f.write(text)

print("Strings patched!")
