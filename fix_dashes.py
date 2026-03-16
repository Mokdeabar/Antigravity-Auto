import re
import os

ps1_path = r'C:\Users\mokde\Desktop\Experiments\Antigravity Auto\INSTALL.ps1'
with open(ps1_path, 'r', encoding='utf-8') as f:
    ps1 = f.read()

ps1 = ps1.replace(' — ', ' : ')
ps1 = ps1.replace(' - ', ' : ')
ps1 = ps1.replace('—', '-')

with open(ps1_path, 'w', encoding='utf-8') as f:
    f.write(ps1)

md_path = r'C:\Users\mokde\Desktop\Experiments\Antigravity Auto\SETUP.md'
with open(md_path, 'r', encoding='utf-8') as f:
    md = f.read()

md = md.replace(' — ', ' : ')
md = md.replace(' - ', ' : ')
md = md.replace('—', '-')

with open(md_path, 'w', encoding='utf-8') as f:
    f.write(md)

print("Updated INSTALL.ps1 and SETUP.md successfully.")
