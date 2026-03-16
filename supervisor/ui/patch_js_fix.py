import re

html_path = 'index.html'

with open(html_path, 'r', encoding='utf-8') as f:
    text = f.read()

# 1. Inject the auto-redirect logic inside updateState()
pattern_state = r'function updateState\(s\) \{\n                if \(\!s\) return;'
replacement_state = r"""function updateState(s) {
                if (!s) return;
                
                // Auto-switch to dashboard if engine is running
                if (s.engine_running && currentScreen === 'launcher') {
                    showScreen('dashboard');
                }"""

text = re.sub(pattern_state, replacement_state, text)

# 2. Add !important to dashboard-screen CSS to ensure it hides when inactive
pattern_css = r'\.dashboard-screen \{\n    position: relative;\n    z-index: 1;\n    display: none;'
replacement_css = r'''.dashboard-screen {
    position: relative;
    z-index: 1;
    display: none !important;'''
    
text = re.sub(pattern_css, replacement_css, text)

# 3. Allow it to display when active
pattern_css2 = r'\.dashboard-screen\.active \{ display: grid; \}'
replacement_css2 = r'.dashboard-screen.active { display: grid !important; }'
text = re.sub(pattern_css2, replacement_css2, text)


with open(html_path, 'w', encoding='utf-8') as f:
    f.write(text)

print("Patch applied to index.html")
