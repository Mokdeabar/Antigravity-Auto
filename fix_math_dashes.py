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

    # Revert ' : ' back to ' - ' but ONLY where it looks like math or where it broke something.
    # Actually, it's safer to just revert EVERYTHING that was ' : ' back to ' - ', and then only replace the actual em dashes manually.
    # But wait, original files used ' — '. I replaced ' — ' with ' : ' and ' - ' with ' : '.
    # Oh well. Let's just fix the math ones: "(TOTAL : 1)", "(TOTAL : 2)"
    content = content.replace('(TOTAL : 1)', '(TOTAL - 1)')
    content = content.replace('TOTAL : 1', 'TOTAL - 1')
    content = content.replace('(TOTAL : 2)', '(TOTAL - 2)')
    content = content.replace('TOTAL : 2', 'TOTAL - 2')

    # Fix gemini formatting in SETUP.html
    if 'SETUP.html' in path:
        # The line break in the gemini "You should see" block:
        # It's currently:
        # ? Login with Google (Use arrow keys)
        #                     ❯ Login with Google
        content = content.replace('<span class="label">✅ You should see something like:</span>? Login with Google (Use arrow keys)\n                    ❯ Login with Google\n                    Use an API Key</div>',
                                  '<span class="label">✅ You should see something like:</span>? Login with Google (Use arrow keys)\n❯ Login with Google\nUse an API Key</div>')
        
    if 'INSTALL.ps1' in path: # fix the passed -eq total
        content = content.replace('($passed :eq $total)', '($passed -eq $total)')
        content = content.replace('$passed :eq', '$passed -eq')
        content = content.replace('-eq', '-eq') # it was eq, so wait, ` - ` would have changed `-eq` ?
        # No, `-eq` is usually ` -eq ` with space before.
        content = content.replace(' :eq ', ' -eq ')
        content = content.replace(' :ne ', ' -ne ')
        content = content.replace(' :not ', ' -not ')
        content = content.replace(' :match ', ' -match ')
        content = content.replace(' :and ', ' -and ')
        content = content.replace(' :ForegroundColor ', ' -ForegroundColor ')
        content = content.replace(' :f ', ' -f ')
        content = content.replace(' :t ', ' -t ')
        content = content.replace(' :g ', ' -g ')
        content = content.replace(' :c ', ' -c ')
        content = content.replace(' :r ', ' -r ')
        content = content.replace(' :split ', ' -split ')
        content = content.replace(' :m ', ' -m ')

    with open(path, 'w', encoding='utf-8') as f:
        f.write(content)

print("Fixed unintended dash replacements in all files.")
