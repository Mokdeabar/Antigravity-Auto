---
name: Webapp Testing
tags: [testing, frontend]
priority: 7
---
# Autonomous Web Application Testing

Patterns for verifying web applications using MCP browser tools.

## Dev Server Readiness
Before any browser test, confirm the dev server is running:
1. Run the dev command (`npm run dev`, `npx vite`, etc.)
2. Wait for the "ready" or "Local:" output line
3. Use `browser_navigate` to open the reported URL
4. If the page doesn't load, check terminal output for errors

## Browser-Based Test Checklist

### 1. Page Load Verification
- Navigate to each major route/page
- Confirm HTTP 200 response (page renders, not blank)
- Check that the document title is set correctly

### 2. Console Error Audit
After each navigation, read the console logs:
- **Zero tolerance** for TypeError, ReferenceError, SyntaxError
- **Investigate** any 4xx/5xx network errors
- **Note** deprecation warnings for follow-up

### 3. Visual Integrity
- Take screenshots at key breakpoints: mobile (375px), tablet (768px), desktop (1440px)
- Check for layout overflow, clipped text, or overlapping elements
- Verify images and SVGs render (no broken image icons)

### 4. Interactive Element Testing
- Click all navigation links — confirm they navigate correctly
- Submit forms with valid data — confirm success state
- Submit forms with invalid data — confirm error states render
- Test modals/drawers open and close properly
- Verify hover states and transitions work

### 5. Accessibility Quick Check
- Verify all images have `alt` attributes
- Confirm proper heading hierarchy (h1 → h2 → h3)
- Check that interactive elements are keyboard-focusable (Tab navigation)
- Verify sufficient color contrast on text elements

### 6. Performance Signals
- Check for large unoptimized images (> 500KB)
- Verify no layout shift visible on page load
- Confirm fonts load without FOUT (flash of unstyled text)

## Error Classification
| Severity | Examples | Action |
|---|---|---|
| **Critical** | JS runtime errors, missing pages, broken builds | Fix immediately |
| **Warning** | Deprecations, performance hints, a11y violations | Fix in same session |
| **Info** | Console.log output, debug traces | Ignore unless excessive |

## Responsive Testing Matrix
Test at these widths to catch layout issues:
- **320px** — Small mobile (iPhone SE)
- **375px** — Standard mobile (iPhone 14)
- **768px** — Tablet portrait (iPad)
- **1024px** — Tablet landscape / small laptop
- **1440px** — Standard desktop
- **2560px** — Ultra-wide / 4K

## Test Isolation
- Each test should be independent — no shared state
- Clear localStorage/sessionStorage between test flows
- Use fresh browser context to avoid cookie contamination
