/**
 * Supervisor AI — Console Error Collector
 * Tiny HTTP server (port 9999) that receives POSTed errors from the hook
 * and stores them in /tmp/console_errors.json for the supervisor to read.
 *
 * GET  /errors  → Returns all collected errors as JSON array
 * POST /errors  → Appends error batch
 * GET  /clear   → Clears collected errors
 */
const http = require('http');
const fs = require('fs');

const PORT = 9999;
const FILE = '/tmp/console_errors.json';

// Initialize empty
if (!fs.existsSync(FILE)) fs.writeFileSync(FILE, '[]');

const server = http.createServer((req, res) => {
    // CORS headers for cross-origin fetch from preview
    res.setHeader('Access-Control-Allow-Origin', '*');
    res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS');
    res.setHeader('Access-Control-Allow-Headers', 'Content-Type');

    if (req.method === 'OPTIONS') {
        res.writeHead(204);
        res.end();
        return;
    }

    if (req.method === 'POST') {
        let body = '';
        req.on('data', chunk => body += chunk);
        req.on('end', () => {
            try {
                const newErrors = JSON.parse(body);
                let existing = [];
                try { existing = JSON.parse(fs.readFileSync(FILE, 'utf8')); } catch (e) { }
                const merged = existing.concat(newErrors).slice(-200); // Keep last 200
                fs.writeFileSync(FILE, JSON.stringify(merged, null, 2));
                res.writeHead(200, { 'Content-Type': 'application/json' });
                res.end(JSON.stringify({ received: newErrors.length, total: merged.length }));
            } catch (e) {
                res.writeHead(400);
                res.end(JSON.stringify({ error: e.message }));
            }
        });
        return;
    }

    if (req.url === '/clear') {
        fs.writeFileSync(FILE, '[]');
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end('[]');
        return;
    }

    // GET /errors (default)
    try {
        const data = fs.readFileSync(FILE, 'utf8');
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end(data);
    } catch (e) {
        res.writeHead(200, { 'Content-Type': 'application/json' });
        res.end('[]');
    }
});

server.listen(PORT, '0.0.0.0', () => {
    console.log(`[Error Collector] Listening on port ${PORT}`);
});
