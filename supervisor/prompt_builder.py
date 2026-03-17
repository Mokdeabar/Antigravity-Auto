"""
V79: Prompt construction utilities extracted from main.py.

Provides error compression and task tag extraction for DAG worker prompts.
"""

import logging
import re

logger = logging.getLogger("supervisor")


# ── File:line pattern for error stack trace extraction ──
_FILE_LINE_PATTERN = re.compile(
    r'(?:'
    r'at\s+(?:.*?\s+\()?(/[^\s:]+):(\d+)'           # Node.js: at fn (/path:line
    r'|(/[^\s:]+):(\d+):\d+'                          # Generic: /path:line:col
    r'|([A-Za-z]:\\\\[^\s:]+):(\d+)'                   # Windows: C:\path:line
    r'|(\S+\.(?:ts|tsx|js|jsx|py|css))\((\d+),\d+\)'  # TS: file.ts(line,col)
    r')'
)


def compress_errors_for_retry(errors: list[str], max_per_error: int = 1500) -> list[str]:
    """
    Compress raw error strings to extract only actionable information.

    For each error:
      1. First line (error name + message)
      2. File:line references from stack traces
      3. Up to 5 lines of surrounding context per reference
      4. Capped at max_per_error chars total

    Returns a list of compressed error strings.
    """
    compressed = []
    for raw in errors:
        raw_str = str(raw)
        lines = raw_str.splitlines()

        if not lines:
            compressed.append("(empty error)")
            continue

        parts = []

        # Always include the first non-empty line (error name/message)
        first_line = ""
        for line in lines:
            stripped = line.strip()
            if stripped:
                first_line = stripped
                break
        parts.append(first_line)

        # Extract file:line references and their surrounding context
        seen_refs = set()
        for i, line in enumerate(lines):
            match = _FILE_LINE_PATTERN.search(line)
            if match:
                # Get the matched file and line number
                groups = match.groups()
                file_ref = next((g for g in groups[::2] if g), None)
                line_ref = next((g for g in groups[1::2] if g), None)
                ref_key = f"{file_ref}:{line_ref}" if file_ref and line_ref else line.strip()

                if ref_key in seen_refs:
                    continue
                seen_refs.add(ref_key)

                # Include this line and up to 2 lines before/after for context
                start = max(0, i - 2)
                end = min(len(lines), i + 3)
                context = "\n".join(lines[start:end]).strip()
                if context and context != first_line:
                    parts.append(context)

                # Cap at 5 file references per error
                if len(seen_refs) >= 5:
                    break

        # If no file references found, include up to first 10 non-empty lines
        if not seen_refs:
            meaningful = [l for l in lines[1:] if l.strip()][:10]
            if meaningful:
                parts.append("\n".join(meaningful))

        result = "\n---\n".join(parts)

        # Final cap
        if len(result) > max_per_error:
            result = result[:max_per_error - 20] + "\n... (truncated)"

        compressed.append(result)

    return compressed


def extract_tag(description: str) -> str:
    """Extract category tag from a task description for tX-TAG naming.

    Scans for explicit tags like [FUNC], [UI/UX], [PERF] first, then
    falls back to keyword matching. Returns: FUNC, UIUX, PERF, or FIX.
    """
    _d = description.upper()

    # ── Explicit tags (highest priority) ──
    if "[UI/UX]" in _d or "[UIUX]" in _d:
        return "UIUX"
    if "[PERF]" in _d:
        return "PERF"
    if "[FUNC]" in _d:
        return "FUNC"
    if "[FIX]" in _d or "[BUG]" in _d:
        return "FIX"

    # ── UI/UX keywords ──
    _uiux_keywords = (
        "STYLING", "CSS", "ANIMATION", "TRANSITION", "HOVER", "MICRO-INTERACTION",
        "LAYOUT", "TYPOGRAPHY", "FONT", "COLOR", "COLOUR", "GRADIENT", "SHADOW",
        "BORDER-RADIUS", "GLASSMORPHISM", "GLASS-MORPHISM", "DARK MODE", "LIGHT MODE",
        "THEME", "RESPONSIVE", "BREAKPOINT", "MOBILE", "TABLET", "GRID LAYOUT",
        "FLEXBOX", "SPACING", "PADDING", "MARGIN", "ICON", "SVG ICON", "CUSTOM SVG",
        "SKELETON", "LOADING STATE", "EMPTY STATE", "ERROR STATE", "TOAST",
        "MODAL", "DIALOG", "DROPDOWN", "TOOLTIP", "POPOVER", "CAROUSEL",
        "SCROLL-DRIVEN", "@STARTING-STYLE", "OKLCH", "CONTAINER QUER",
        "ANCHOR POSITION", "@SCOPE", "VIEW TRANSITION", "TEXT-WRAP",
        "CONTENT-VISIBILITY", "LIGHT-DARK(", "COLOR-MIX(", "SCROLL ANIMATION",
        "PARALLAX", "REVEAL", "FADE-IN", "SLIDE-IN", "STAGGER", "EASE",
        "CUBIC-BEZIER", "KEYFRAME", "@KEYFRAME", "DESIGN SYSTEM", "DESIGN TOKEN",
        "AWWWARDS", "VISUAL", "AESTHETIC", "POLISH", "UI COMPONENT", "UX",
        "NAVIGATION", "NAVBAR", "SIDEBAR", "FOOTER", "HEADER", "HERO",
        "CARD", "BUTTON STYLE", "INPUT STYLE", "FORM STYLE", "SELECT STYLE",
        "CHART", "RADAR", "HEATMAP", "BADGE", "CHIP", "TAG STYLE", "AVATAR",
        "PROGRESS BAR", "STEPPER", "TAB", "ACCORDION",
    )
    for _kw in _uiux_keywords:
        if _kw in _d:
            return "UIUX"

    # ── Performance / Lighthouse / A11y / SEO keywords ──
    _perf_keywords = (
        "LIGHTHOUSE", "PERFORMANCE", "SEO", "A11Y", "ACCESSIBILITY", "WCAG",
        "ARIA", "ALT TEXT", "CONTRAST", "KEYBOARD NAV", "FOCUS TRAP", "SCREEN READER",
        "SEMANTIC", "LANDMARK", "HEADING ORDER", "SKIP LINK", "TAB ORDER",
        "FCP", "LCP", "TBT", "CLS", "SPEED INDEX", "RENDER-BLOCKING",
        "CODE SPLIT", "TREE SHAKE", "LAZY LOAD", "PRELOAD", "PRECONNECT",
        "PREFETCH", "WEBP", "AVIF", "IMAGE OPTIM", "COMPRESS", "MINIF",
        "BUNDLE SIZE", "CACHE", "SERVICE WORKER", "HTTP/2", "HTTP/3", "BROTLI",
        "GZIP", "CDN", "FONT-DISPLAY", "CRITICAL CSS", "ABOVE-FOLD",
        "CONTENT-SECURITY-POLICY", "CSP", "HSTS", "COOP", "TRUSTED TYPES",
        "SOURCE MAP", "ROBOTS.TXT", "SITEMAP", "CANONICAL", "META DESCRIPTION",
        "STRUCTURED DATA", "JSON-LD", "OPEN GRAPH", "OG:", "TWITTER CARD",
        "RESOURCE HINT", "DEFER", "ASYNC SCRIPT", "WEB WORKER", "WILL-CHANGE",
        "PAINT", "REFLOW", "DOM SIZE", "MEMORY", "LEAK",
    )
    for _kw in _perf_keywords:
        if _kw in _d:
            return "PERF"

    # ── Bug fix keywords ──
    _fix_keywords = (
        "FIX", "BUG", "ERROR", "CRASH", "BROKEN", "UNDEFINED", "REFERENCEERROR",
        "SYNTAXERROR", "TYPEERROR", "IMPORT", "MISSING IMPORT", "DEAD CODE",
        "REGRESSION", "PATCH", "HOTFIX",
    )
    for _kw in _fix_keywords:
        if _kw in _d:
            return "FIX"

    return "FUNC"  # Default to FUNC for feature/implementation tasks
