---
name: Project Scaffolding
tags: [setup, coding]
priority: 9
---
# Project Scaffolding Rules

When creating ANY web project (HTML, CSS, JS, landing pages, SPAs, dashboards, etc.) follow these rules exactly.

## Vite-First
1. ALWAYS use Vite as the dev server, even for single-page projects.
   Run: `npm create vite@latest ./ -- --template vanilla`, then `npm install`.
2. Place your HTML in `index.html` at the project ROOT. Vite gives HMR, CORS, and correct MIME types, which are required for the preview system to work.

## File Structure
- ALWAYS place `index.html` in the project ROOT, never in subdirectories.
- All assets (images, fonts, CSS, JS) must use RELATIVE paths, never absolute filesystem paths.
- This ensures they work both locally and when deployed to any server.

## CORS
- If fetching external APIs, use proper CORS-safe techniques (server-side proxy, CORS headers, or JSONP).
- Never rely on browser-disabled CORS for production.

## PHP Prohibition
- NEVER scaffold PHP projects unless the user explicitly requests PHP.
- The sandbox does not have PHP installed.

## Verification
- After scaffolding, ALWAYS run `npm install && npm run dev` to verify the dev server starts and serves the project correctly.
- Check that the page loads at the dev server URL before proceeding.
