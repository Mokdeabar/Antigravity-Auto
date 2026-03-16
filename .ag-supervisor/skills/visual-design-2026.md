---
name: Visual Design 2026
tags: [frontend, coding, setup]
priority: 10
---
# 2026 Visual Design Mandate

Every project MUST have a mind-blowing UI/UX that would win Awwwards Site of the Year 2026. Non-negotiable. The design must be the PEAK of beauty while fitting the project's domain.

## Color System
- Use a curated, harmonious palette with HSL-tuned colors. No generic red/blue/green.
- Build CSS custom properties for the full palette (--color-primary, --color-surface, --color-accent, etc.).
- Light Mode AND Dark mode MUST be included with equally beautiful colors, not just inverted.

## Typography
- Import a premium font stack from Google Fonts (Inter, Outfit, Geist, Space Grotesk, or similar).
- Set a proper type scale with at least 5 sizes. Use font-weight variation (300-700).
- Letter-spacing and line-height must be deliberately tuned, not browser defaults.

## Spacing & Layout
- Consistent spacing system (4px/8px base grid). Generous whitespace.
- Content should breathe. No cramped layouts.
- Sections must have visual rhythm -- alternating content density, backgrounds.

## Animations & Micro-Interactions
- Every interactive element needs feedback.
- Buttons: scale + glow on hover/press. Cards: lift shadow on hover.
- Page transitions: smooth fade/slide, staggered entry for lists.
- CSS transitions (0.2-0.4s ease) and @keyframes for complex sequences.
- Scroll-triggered animations (IntersectionObserver).
- Loading states with skeleton screens or shimmer effects, not spinners.

## Visual Depth
- Layered shadows (not flat box-shadow).
- Glassmorphism (backdrop-filter: blur) for overlays and navigation.
- Subtle gradients on key surfaces.
- Border-radius consistency (CSS vars for radius scale).

## Responsive
- Mobile-first. Must look stunning on 375px AND 2560px.
- Not just "working" -- genuinely beautiful at every breakpoint.
- Touch targets 44px minimum. Swipe gestures where appropriate.

## Domain Fit
Match the aesthetic to the project's purpose:
- Finance/SaaS = clean, trustworthy, data-dense.
- Creative/Portfolio = bold, expressive, editorial.
- E-commerce = warm, inviting, product-focused.
- Dev tools = precise, monospace accents, dark-by-default.
- Health/Wellness = soft, calming, organic shapes.
- Gaming = vibrant, energetic, dramatic.

## Details
- Icons: Use a consistent icon library (Lucide, Phosphor, or custom SVG). Never mix icon styles.
- Images: Generate real ones or use high-quality Unsplash/Pexels URLs. NEVER use broken placeholders.
- Empty states: Design beautiful empty/zero/error states with illustrations or icons.

## The Acid Test
If a user's first reaction is NOT "wow, this is gorgeous", the design has FAILED. Go back and make it stunning.
