---
name: Lighthouse Perfection
tags: [testing, analysis, coding]
priority: 7
---
# Lighthouse 100/100 Methodology

Achieve perfect scores across Performance, Accessibility, Best Practices, and SEO without compromising visual design or functionality.

## Performance (Target: 100)
- **LCP < 2.5s**: Preload hero images, use `loading="eager"` on above-fold images, defer non-critical JS.
- **CLS = 0**: Set explicit `width`/`height` on all images and embeds. Use `aspect-ratio` CSS. Reserve space for dynamic content.
- **FID/INP < 200ms**: Break long tasks with `requestIdleCallback`. Defer heavy event handlers. Use `will-change` sparingly.
- **Bundle size**: Tree-shake imports. Code-split routes. Compress with Brotli. Inline critical CSS.
- **Fonts**: Use `font-display: swap`. Preload the primary font. Subset to Latin if possible.

## Accessibility (Target: 100)
- Single `<h1>` per page with proper heading hierarchy (no skipped levels).
- All images: descriptive `alt` text (decorative images get `alt=""`).
- All form inputs: associated `<label>` elements (not just placeholder text).
- Color contrast: minimum 4.5:1 for text, 3:1 for large text (WCAG AA).
- Focus indicators: visible focus rings on all interactive elements. Never `outline: none` without replacement.
- ARIA: Use semantic HTML first. Only add ARIA when native semantics are insufficient. `aria-label` for icon-only buttons.
- Skip navigation: Add a "Skip to main content" link as the first focusable element.
- Reduced motion: Respect `prefers-reduced-motion` — disable animations, autoplay, parallax.

## Best Practices (Target: 100)
- HTTPS: Serve all resources over HTTPS. No mixed content.
- CSP: Add `Content-Security-Policy` headers where possible.
- No `document.write()`. No `eval()`. No unload listeners.
- Console: Zero errors in the browser console. Warnings should be addressed.
- Source maps: Generate them for dev, strip for production.
- Image formats: Use WebP/AVIF with `<picture>` fallbacks. Never serve uncompressed PNGs for photos.

## SEO (Target: 100)
- Proper `<title>` tag (unique, descriptive, 50-60 chars).
- `<meta name="description">` (unique, compelling, 150-160 chars).
- `<meta name="viewport" content="width=device-width, initial-scale=1">`.
- Canonical URL: `<link rel="canonical">` on every page.
- Semantic HTML5: `<header>`, `<nav>`, `<main>`, `<article>`, `<footer>`.
- Structured data: JSON-LD where applicable (breadcrumbs, articles, products).
- `robots.txt` and `sitemap.xml` for multi-page sites.

## Audit Workflow
1. Run `npx lighthouse <url> --output=json --output-path=./lighthouse.json`
2. Parse the JSON for failing audits
3. Fix the highest-impact items first (sorted by weight × score delta)
4. Re-run after each batch of fixes to confirm improvement
5. Repeat until all four categories hit 100
