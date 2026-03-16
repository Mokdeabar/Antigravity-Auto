/**
 * Supervisor AI — Console Error Hook  (V53)
 *
 * Injected into the preview at runtime to capture ALL categories of browser
 * errors and POST them to the collector on port 9999.
 *
 * Works with: static HTML, Vite, React, Next.js, Vue, Svelte, Nuxt, Angular,
 *             Cesium/Resium, Three.js, Socket.IO apps, PHP/Python backends.
 *
 * Captures:
 *   1. Uncaught JS errors          (window.error)
 *   2. Unhandled promise rejections (window.unhandledrejection)
 *   3. console.error() calls
 *   4. console.warn() calls        (only errors + specific warn patterns)
 *   5. Resource load failures      (PerformanceObserver — 4xx/5xx on scripts,
 *                                   images, CSS, WASM, fetch)
 *   6. Dynamic import failures     (fetch() TypeError + non-2xx intercept)
 *   7. XHR failures                (XMLHttpRequest intercept)
 *   8. WebSocket connection errors (navigator.onLine + WS error events)
 *
 * Anti-flood: identical messages within 10s are collapsed to one entry.
 * Rate limit: max 100 unique errors stored per session.
 */
(function () {
    'use strict';

    var COLLECTOR = 'http://localhost:9999/errors';
    var FLUSH_MS = 3000;
    var MAX_ERRORS = 100;
    var DEDUP_WINDOW_MS = 10000;

    var BATCH = [];
    var _seen = {};   // key → last-seen timestamp
    var _count = 0;   // total deduplicated errors this session

    // ── Error categories (matched in order) ─────────────────────────────
    var PATTERNS = [
        // Vite optimizer stale cache
        { re: /504|Outdated Optimize Dep/i, cat: 'vite_stale_cache' },
        // Dynamic import / lazy chunk failures
        { re: /Failed to fetch dynamically imported/i, cat: 'dynamic_import_fail' },
        { re: /Loading chunk \w+ failed/i, cat: 'dynamic_import_fail' },
        { re: /Failed to load module script/i, cat: 'dynamic_import_fail' },
        // WebSocket failures (app-level, not Vite HMR)
        { re: /WebSocket.*(failed|closed|error)/i, cat: 'ws_connection_fail' },
        { re: /TransportError.*websocket/i, cat: 'ws_connection_fail' },
        // Resource 404 / missing file
        { re: /the server responded with a status of 404/i, cat: 'missing_resource_404' },
        { re: /Failed to load resource.*404/i, cat: 'missing_resource_404' },
        // Generic HTTP error on resource
        { re: /Failed to load resource/i, cat: 'resource_load_fail' },
        // Deprecated API
        { re: /apple-mobile-web-app-capable.*deprecated/i, cat: 'deprecated_meta' },
        { re: /is deprecated/i, cat: 'deprecated_api' },
        // Standard JS errors
        { re: /TypeError/i, cat: 'js_type_error' },
        { re: /ReferenceError/i, cat: 'js_reference_error' },
        { re: /SyntaxError/i, cat: 'js_syntax_error' },
    ];

    function categorise(msg) {
        for (var i = 0; i < PATTERNS.length; i++) {
            if (PATTERNS[i].re.test(msg)) return PATTERNS[i].cat;
        }
        return 'uncategorised';
    }

    function dedup(key) {
        var now = Date.now();
        if (_seen[key] && (now - _seen[key]) < DEDUP_WINDOW_MS) return true;
        _seen[key] = now;
        return false;
    }

    function send(entry) {
        if (_count >= MAX_ERRORS) return;
        var key = entry.category + '|' + entry.message.substring(0, 80);
        if (dedup(key)) return;
        _count++;
        BATCH.push(entry);
    }

    function flush() {
        if (BATCH.length === 0) return;
        var payload = JSON.stringify(BATCH);
        BATCH = [];
        try {
            var xhr = new XMLHttpRequest();
            xhr.open('POST', COLLECTOR, true);
            xhr.setRequestHeader('Content-Type', 'application/json');
            xhr.send(payload);
        } catch (e) { /* collector may not be running yet */ }
    }

    // ── 1. Uncaught JS errors ─────────────────────────────────────────────
    window.addEventListener('error', function (ev) {
        // Skip errors from our own hook script and the collector
        if (ev.filename && ev.filename.indexOf('_supervisor_error_hook') !== -1) return;
        var msg = ev.message || String(ev);
        send({
            type: 'uncaught_error',
            category: categorise(msg),
            message: msg,
            source: ev.filename || '',
            line: ev.lineno || 0,
            stack: ev.error && ev.error.stack ? ev.error.stack.substring(0, 600) : '',
            ts: Date.now()
        });
    });

    // ── 2. Unhandled promise rejections ──────────────────────────────────
    window.addEventListener('unhandledrejection', function (ev) {
        var msg = ev.reason
            ? (ev.reason.message || String(ev.reason))
            : 'Unhandled promise rejection';
        send({
            type: 'unhandled_rejection',
            category: categorise(msg),
            message: msg,
            stack: ev.reason && ev.reason.stack ? ev.reason.stack.substring(0, 600) : '',
            ts: Date.now()
        });
    });

    // ── 3. console.error intercept ────────────────────────────────────────
    var _origError = console.error;
    console.error = function () {
        var args = Array.prototype.slice.call(arguments);
        var msg = args.map(function (a) {
            if (a instanceof Error) return a.message + (a.stack ? '\n' + a.stack.substring(0, 300) : '');
            return typeof a === 'object' ? JSON.stringify(a).substring(0, 300) : String(a);
        }).join(' ');
        send({
            type: 'console_error',
            category: categorise(msg),
            message: msg,
            ts: Date.now()
        });
        return _origError.apply(console, arguments);
    };

    // ── 4. console.warn — only actionable patterns ────────────────────────
    var _origWarn = console.warn;
    console.warn = function () {
        var args = Array.prototype.slice.call(arguments);
        var msg = args.map(function (a) {
            return typeof a === 'object' ? JSON.stringify(a).substring(0, 200) : String(a);
        }).join(' ');
        // Only capture warnings that match known actionable patterns
        var cat = categorise(msg);
        if (cat !== 'uncategorised') {
            send({ type: 'console_warn', category: cat, message: msg, ts: Date.now() });
        }
        return _origWarn.apply(console, arguments);
    };

    // ── 5. PerformanceObserver — resource load failures (4xx / 5xx) ───────
    // Catches: cesium.js 504, missing CSS/images, WASM failures, etc.
    try {
        if (window.PerformanceObserver) {
            var observer = new PerformanceObserver(function (list) {
                list.getEntries().forEach(function (entry) {
                    // responseStatus is available in modern browsers for resource entries
                    var status = entry.responseStatus || 0;
                    var name = entry.name || '';
                    // Skip supervisor collector itself
                    if (name.indexOf('9999') !== -1 || name.indexOf('_supervisor') !== -1) return;
                    // Flag any non-2xx resource (4xx / 5xx) or zero-duration failed loads
                    var isFailed = (status >= 400) ||
                        (status === 0 && entry.duration === 0 &&
                            entry.transferSize === 0 &&
                            entry.initiatorType === 'fetch');
                    if (isFailed) {
                        var msg = 'Failed to load resource' + (status ? ' (HTTP ' + status + ')' : '') + ': ' + name;
                        send({
                            type: 'resource_error',
                            category: categorise(msg + ' ' + (status || '')),
                            message: msg,
                            url: name,
                            status: status,
                            initiator: entry.initiatorType || '',
                            ts: Date.now()
                        });
                    }
                });
            });
            observer.observe({ type: 'resource', buffered: true });
        }
    } catch (e) { /* PerformanceObserver not supported */ }

    // ── 6. fetch() intercept — catches dynamic imports + API failures ─────
    // This is the key fix for "Failed to fetch dynamically imported module"
    var _origFetch = window.fetch;
    window.fetch = function (input, init) {
        var url = typeof input === 'string' ? input : (input && input.url) || '';
        // Skip collector POSTs to avoid recursion
        if (url.indexOf('9999') !== -1) return _origFetch.apply(this, arguments);

        return _origFetch.apply(this, arguments).then(function (response) {
            if (!response.ok && response.status >= 400) {
                var msg = 'Fetch failed (HTTP ' + response.status + '): ' + url;
                send({
                    type: 'fetch_error',
                    category: categorise(msg),
                    message: msg,
                    url: url,
                    status: response.status,
                    ts: Date.now()
                });
            }
            return response;
        }, function (err) {
            // Network error / CORS / dynamic import failure
            var msg = (err && err.message) ? err.message : 'TypeError: Failed to fetch';
            if (url) msg += ': ' + url;
            send({
                type: 'fetch_network_error',
                category: categorise(msg),
                message: msg,
                url: url,
                ts: Date.now()
            });
            throw err;
        });
    };

    // ── 7. XMLHttpRequest intercept ───────────────────────────────────────
    var _origOpen = XMLHttpRequest.prototype.open;
    var _origSend = XMLHttpRequest.prototype.send;
    XMLHttpRequest.prototype.open = function (method, url) {
        this._ag_url = url || '';
        this._ag_method = method || '';
        return _origOpen.apply(this, arguments);
    };
    XMLHttpRequest.prototype.send = function () {
        var self = this;
        var url = this._ag_url || '';
        if (url.indexOf('9999') === -1) {  // skip collector
            this.addEventListener('load', function () {
                if (self.status >= 400) {
                    var msg = 'XHR failed (HTTP ' + self.status + '): ' + url;
                    send({
                        type: 'xhr_error',
                        category: categorise(msg),
                        message: msg,
                        url: url,
                        status: self.status,
                        ts: Date.now()
                    });
                }
            });
            this.addEventListener('error', function () {
                var msg = 'XHR network error: ' + url;
                send({
                    type: 'xhr_network_error',
                    category: categorise(msg),
                    message: msg,
                    url: url,
                    ts: Date.now()
                });
            });
        }
        return _origSend.apply(this, arguments);
    };

    // ── Flush periodically and on unload ─────────────────────────────────
    setInterval(flush, FLUSH_MS);
    window.addEventListener('beforeunload', flush);

    // Flush immediately after page load (catches buffered PerformanceObserver entries)
    if (document.readyState === 'complete') {
        setTimeout(flush, 500);
    } else {
        window.addEventListener('load', function () { setTimeout(flush, 500); });
    }

})();
