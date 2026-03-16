import re
import os

path = r'C:\Users\mokde\Desktop\Experiments\Antigravity Auto\SETUP.html'

with open(path, 'r', encoding='utf-8') as f:
    html = f.read()

# Replace Em/En Dashes with just text or hyphen
html = html.replace(' — ', ' : ')
html = html.replace(' - ', ' : ')
html = html.replace('—', '-')

# Replace "You should see" with "You should see something like" where versions are present
html = html.replace('<span class="label">✅ You should see:</span>WSL version:', '<span class="label">✅ You should see something like:</span>WSL version:')
html = html.replace('<span class="label">✅ You should see:</span>git version 2.47.1.windows.1', '<span class="label">✅ You should see something like:</span>git version 2.53.0.windows.1')
html = html.replace('<span class="label">✅ You should see:</span>Python 3.13.2', '<span class="label">✅ You should see something like:</span>Python 3.14.3')
html = html.replace('<h2><span class="num">5</span> Install Python 3.13</h2>', '<h2><span class="num">5</span> Install Python 3.14</h2>')
html = html.replace('winget install Python.Python.3.13', 'winget install Python.Python.3.14')

# Node & npm
html = html.replace('<span class="label">✅ You should see:</span>v22.12.0 (or v20.x)', '<span class="label">✅ You should see something like:</span>v25.7.0 (or v24.x LTS)')
html = html.replace('<span class="label">✅ You should see:</span>10.9.0 (any 9+ version is fine)', '<span class="label">✅ You should see something like:</span>11.11.0 (any recent version is fine)')

# Docker
html = html.replace('<span class="label">✅ You should see:</span>Docker version 27.4.0', '<span class="label">✅ You should see something like:</span>Docker version 27.x.x')
html = html.replace('<span class="label">✅ You should see (first few lines):</span>', '<span class="label">✅ You should see something like (first few lines):</span>')

# API Key & Gemini
html = html.replace('<span class="label">✅ You should see:</span>? Login with Google', '<span class="label">✅ You should see something like:</span>? Login with Google')
html = html.replace('<span class="label">✅ You should see:</span>REPOSITORY', '<span class="label">✅ You should see something like:</span>REPOSITORY')


# Fix early line breaks: we'll match the prompt carefully
# In HTML, it looks like:
# <span class="label">✅ You should see something like:</span>? Login with Google (Use arrow
# keys)
# ❯ Login with Google
html = html.replace('? Login with Google (Use arrow\n                    keys)', '? Login with Google (Use arrow keys)')
html = html.replace('? Login with Google (Use arrow\nkeys)', '? Login with Google (Use arrow keys)')
# The WSL one:
html = html.replace('<span class="label">✅ You should see something like:</span>WSL version:\n                    2.4.4.0', '<span class="label">✅ You should see something like:</span>WSL version: 2.x.x.x')

# EMOJIS -> CUSTOM SVGs
svg_map = {
    '⚡': '<svg width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="title-icon" style="color:#0071e3;vertical-align:-4px;margin-right:8px;display:inline-block"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"></polygon></svg>',
    '💡': '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="icon" style="vertical-align:-4px;display:inline-block"><path d="M15 14c.2-1 .7-1.7 1.5-2.5 1-.9 1.5-2.2 1.5-3.5A6 6 0 0 0 6 8c0 1 .2 2.2 1.5 3.5.7.9 1.2 1.5 1.5 2.5"/><path d="M9 18h6"/><path d="M10 22h4"/></svg>',
    '🚨': '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="icon" style="vertical-align:-4px;display:inline-block;color:var(--red)"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
    '⚠️': '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="icon" style="vertical-align:-4px;display:inline-block;color:var(--orange)"><polygon points="12 2 22 22 2 22"></polygon><line x1="12" y1="8" x2="12" y2="12"></line><line x1="12" y1="16" x2="12.01" y2="16"></line></svg>',
    '📖': '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="icon" style="vertical-align:-3px;display:inline-block"><path d="M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z"/><path d="M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z"/></svg>',
    '🔍': '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="icon" style="vertical-align:-3px;display:inline-block"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>',
    '📋': '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="icon" style="vertical-align:-3px;display:inline-block"><path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"/><rect x="8" y="2" width="8" height="4" rx="1" ry="1"/></svg>',
    '⬇️': '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="icon" style="vertical-align:-3px;display:inline-block"><line x1="12" y1="5" x2="12" y2="19"/><polyline points="19 12 12 19 5 12"/></svg>',
    '🛡️': '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="icon" style="vertical-align:-3px;display:inline-block"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>',
    '📂': '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="icon" style="vertical-align:-3px;display:inline-block"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>',
    '💻': '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="icon" style="vertical-align:-3px;display:inline-block"><rect x="2" y="3" width="20" height="14" rx="2" ry="2"/><line x1="8" y1="21" x2="16" y2="21"/><line x1="12" y1="17" x2="12" y2="21"/></svg>',
    '<span class="arrow">▶</span>': '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" class="arrow" style="vertical-align:0px;display:inline-block;margin-right:6px"><polygon points="5 3 19 12 5 21 5 3"/></svg>',
    '▶ ': '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round" class="arrow" style="vertical-align:-3px;display:inline-block;margin-right:4px"><polygon points="5 3 19 12 5 21 5 3"/></svg> ',
    '🚀': '<svg width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" style="color:#0071e3;display:inline-block;margin-bottom:12px"><path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09z"/><path d="m12 15-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z"/><path d="M9 12H4s.55-3.03 2-4c1.62-1.08 5 0 5 0"/><path d="M12 15v5s3.03-.55 4-2c1.08-1.62 0-5 0-5"/></svg>',
    '<span class="icon">💡</span>': '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="icon" style="vertical-align:-4px;display:inline-block;margin-right:8px"><path d="M15 14c.2-1 .7-1.7 1.5-2.5 1-.9 1.5-2.2 1.5-3.5A6 6 0 0 0 6 8c0 1 .2 2.2 1.5 3.5.7.9 1.2 1.5 1.5 2.5"/><path d="M9 18h6"/><path d="M10 22h4"/></svg>',
}

# Apply direct replacements for emojis inside html
for emoji, svg in svg_map.items():
    html = html.replace(emoji, svg)

# Let's fix Check Results JS fallback logic
html = html.replace("html += `<span class=\\\"check-result ${cls}\\\">${ok ? '✅' : '❌'} ${name}${ver}</span>`;", 
                    "html += `<span class=\\\"check-result ${cls}\\\">${ok ? '<svg width=\\\"14\\\" height=\\\"14\\\" viewBox=\\\"0 0 24 24\\\" fill=\\\"none\\\" stroke=\\\"currentColor\\\" stroke-width=\\\"3\\\" stroke-linecap=\\\"round\\\" stroke-linejoin=\\\"round\\\" style=\\\"color:#1a7b31;vertical-align:-2px;display:inline-block;margin-right:4px\\\"><polyline points=\\\"20 6 9 17 4 12\\\"/></svg>' : '<svg width=\\\"14\\\" height=\\\"14\\\" viewBox=\\\"0 0 24 24\\\" fill=\\\"none\\\" stroke=\\\"currentColor\\\" stroke-width=\\\"3\\\" stroke-linecap=\\\"round\\\" stroke-linejoin=\\\"round\\\" style=\\\"color:var(--red);vertical-align:-2px;display:inline-block;margin-right:4px\\\"><line x1=\\\"18\\\" y1=\\\"6\\\" x2=\\\"6\\\" y2=\\\"18\\\"/><line x1=\\\"6\\\" y1=\\\"6\\\" x2=\\\"18\\\" y2=\\\"18\\\"/></svg>'} ${name}${ver}</span>`;")

html = html.replace("banner.innerHTML = '✅ Already done", "banner.innerHTML = '<svg width=\\\"14\\\" height=\\\"14\\\" viewBox=\\\"0 0 24 24\\\" fill=\\\"none\\\" stroke=\\\"currentColor\\\" stroke-width=\\\"3\\\" stroke-linecap=\\\"round\\\" stroke-linejoin=\\\"round\\\" style=\\\"color:#1a7b31;vertical-align:-2px;display:inline-block;margin-right:6px\\\"><polyline points=\\\"20 6 9 17 4 12\\\"/></svg> Already done")
html = html.replace("banner.innerHTML = '⚡ Optional", "banner.innerHTML = '<svg width=\\\"14\\\" height=\\\"14\\\" viewBox=\\\"0 0 24 24\\\" fill=\\\"none\\\" stroke=\\\"currentColor\\\" stroke-width=\\\"3\\\" stroke-linecap=\\\"round\\\" stroke-linejoin=\\\"round\\\" style=\\\"color:#b36800;vertical-align:-2px;display:inline-block;margin-right:6px\\\"><polygon points=\\\"13 2 3 14 12 14 11 22 21 10 12 10 13 2\\\"/></svg> Optional")
html = html.replace("banner.innerHTML = '❌ Action needed", "banner.innerHTML = '<svg width=\\\"14\\\" height=\\\"14\\\" viewBox=\\\"0 0 24 24\\\" fill=\\\"none\\\" stroke=\\\"currentColor\\\" stroke-width=\\\"3\\\" stroke-linecap=\\\"round\\\" stroke-linejoin=\\\"round\\\" style=\\\"color:var(--red);vertical-align:-2px;display:inline-block;margin-right:6px\\\"><line x1=\\\"18\\\" y1=\\\"6\\\" x2=\\\"6\\\" y2=\\\"18\\\"/><line x1=\\\"6\\\" y1=\\\"6\\\" x2=\\\"18\\\" y2=\\\"18\\\"/></svg> Action needed")

# Replace header text manually
html = html.replace('<h1><svg width="24" height="24"', '<h1><svg width="28" height="28"') # Make header bolt slightly bigger

# Make sure "Supervisor AI : Setup Wizard" replaced
html = html.replace('<h1><svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="title-icon" style="color:#0071e3;vertical-align:-4px;margin-right:8px;display:inline-block"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"></polygon></svg> Supervisor AI : Setup Wizard</h1>', 
                    '<h1><svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" class="title-icon" style="color:#0071e3;vertical-align:-4px;margin-right:12px;display:inline-block"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"></polygon></svg>Supervisor AI : Setup Wizard</h1>')

with open(path, 'w', encoding='utf-8') as f:
    f.write(html)
print("Updated SETUP.html successfully.")
