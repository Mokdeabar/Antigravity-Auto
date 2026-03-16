import re

html_path = r'c:\Users\mokde\Desktop\Experiments\Antigravity Auto\supervisor\ui\index.html'
css_path = r'c:\Users\mokde\Desktop\Experiments\Antigravity Auto\supervisor\ui\style_cupertino.css'

with open(html_path, 'r', encoding='utf-8') as f:
    html = f.read()

with open(css_path, 'r', encoding='utf-8') as f:
    new_css = f.read()

# Replace everything between <style> and </style>
html = re.sub(r'<style>.*?</style>', f'<style>\n{new_css}\n    </style>', html, flags=re.DOTALL)

with open(html_path, 'w', encoding='utf-8') as f:
    f.write(html)

print("Successfully replaced CSS in index.html with Cupertino Light Theme")
