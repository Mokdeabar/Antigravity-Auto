import re
import os

files = [
    r'C:\Users\mokde\Desktop\Experiments\Antigravity Auto\SETUP.html',
    r'C:\Users\mokde\Desktop\Experiments\Antigravity Auto\INSTALL.ps1',
    r'C:\Users\mokde\Desktop\Experiments\Antigravity Auto\SETUP.md'
]

for path in files:
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()

    # En dash and Em dash
    content = content.replace('–', '-')
    content = content.replace('—', '-')

    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)

print("Double checked dashes in all files.")
