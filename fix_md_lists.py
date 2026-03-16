import re

target_file = r'C:\Users\mokde\Desktop\Experiments\Antigravity Auto\SETUP.md'

with open(target_file, 'r', encoding='utf-8') as f:
    lines = f.readlines()

new_lines = []
for line in lines:
    # Fix list items that became :
    # e.g. "   : **Dell:** F2" -> "   - **Dell:** F2"
    # Or "- item : description" which might be fine
    match = re.match(r'^(\s*):\s(.*)', line)
    if match:
        new_lines.append(f"{match.group(1)}- {match.group(2)}\n")
    else:
        new_lines.append(line)

with open(target_file, 'w', encoding='utf-8') as f:
    f.writelines(new_lines)

print("Fixed markdown lists in SETUP.md")
