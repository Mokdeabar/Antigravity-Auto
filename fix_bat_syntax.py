import re

target_file = r'C:\Users\mokde\Desktop\Experiments\Antigravity Auto\check_setup.bat'

with open(target_file, 'r', encoding='utf-8') as f:
    content = f.read()

# Fix the Python `not "not found"` check, just do it safely:
content = content.replace('if not "!PYTHON_VER!"=="not found" set "PYTHON_OK=true"', 'if not "!PYTHON_VER!" == "not found" set "PYTHON_OK=true"')
content = content.replace('if not "!PIP_VER!"=="not found" set "PIP_OK=true"', 'if not "!PIP_VER!" == "not found" set "PIP_OK=true"')

with open(target_file, 'w', encoding='utf-8') as f:
    f.write(content)

print("Batch syntax spacing fixed.")
