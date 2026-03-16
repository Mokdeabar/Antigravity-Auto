---
name: Browser Verification
tags: [testing, frontend, coding]
priority: 9
---
# Browser Verification via MCP Tools

You have access to browser automation tools through the Model Context Protocol.
Use these tools to verify your changes work correctly in a real browser.

## When to Verify in Browser
- After modifying HTML, CSS, or JavaScript files
- After changing component logic, routing, or page layout
- After fixing console errors or broken imports
- When the task mentions "verify", "test", "check", or "preview"

## ReAct Verification Loop

Follow this pattern after making frontend changes:

### Step 1 — Start Dev Server (if not running)
```
Run: npm run dev (or appropriate dev command)
Wait for the server to report a local URL (usually http://localhost:3000 or :5173)
```

### Step 2 — Navigate to the Page
Use `browser_navigate` to open the dev server URL.
If you know the specific page affected, navigate directly to it.

### Step 3 — Check Console for Errors
Use `browser_console_logs` to read the browser console.
Look for:
- **JavaScript errors**: TypeError, ReferenceError, SyntaxError
- **Network errors**: 404 Not Found, CORS blocked, ERR_CONNECTION_REFUSED
- **React/Vue errors**: "Cannot read properties of undefined", hydration mismatches
- **Asset errors**: Missing images, broken font imports, failed CSS loads

### Step 4 — Verify Key Elements Exist
Use `browser_click` or page inspection to confirm:
- Page title renders correctly
- Navigation links are functional
- Key content sections are visible
- Interactive elements respond (buttons, forms, modals)

### Step 5 — Take a Screenshot
Use `browser_screenshot` to capture the visual state.
This provides proof the page renders correctly.

### Step 6 — Fix and Re-verify
If console errors or visual issues are found:
1. Identify the root cause from the error messages
2. Fix the code
3. Return to Step 2 and re-verify

## Common Error Diagnosis Patterns

### CORS Errors
```
Access to fetch at 'X' from origin 'Y' has been blocked by CORS policy
```
Fix: Add proper CORS headers on the backend, or use a proxy in vite.config.

### Missing Module Errors
```
Uncaught SyntaxError: Cannot use import statement outside a module
```
Fix: Check `type: "module"` in package.json, or fix import/export syntax.

### React Hydration Mismatch
```
Warning: Text content did not match. Server: "X" Client: "Y"
```
Fix: Ensure server and client render the same initial content.

### 404 Asset Errors
```
GET http://localhost:3000/assets/logo.png 404 (Not Found)
```
Fix: Verify the asset path is correct relative to the public directory.

## Lighthouse Quick Audit
When verifying performance or accessibility:
1. Navigate to the page
2. Check console for performance warnings
3. Verify images have `alt` attributes
4. Verify proper heading hierarchy (single h1, sequential h2-h6)
5. Check that interactive elements are keyboard-accessible

## Rules
- ALWAYS check the console after navigation — zero errors is the target
- Fix errors immediately rather than noting them for later
- Take a screenshot as proof of successful verification
- If the dev server won't start, diagnose the error before proceeding
