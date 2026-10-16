/*
 * Local gateway for the detection UI: CORS proxy + execution-row purger.
 *
 * SUPERSEDED 2026-10-06 by the Flask backend (python -m backend.app), which does
 * the same proxy + purge and additionally owns the SQLite config store. Both bind
 * 127.0.0.1:8787, so only one of them can run. Kept for reference and as a
 * Node-only fallback if Python is unavailable.
 *
 * Why this exists: n8n writes the webhook's inbound request record — including
 * any x-primary-api-key header — into its SQLite database when an execution
 * starts. Disabling run-data saving shrinks the row (1-2 KB instead of 12 KB)
 * but does not remove it, so a BYOK key is still at rest. Measured 2026-10-06.
 *
 * The same process that fronts the API also deletes those rows a second later,
 * so there is no way to run the UI without also running the purge.
 *
 *   node tools/detector-gateway.js
 *   PORT=8787 N8N_URL=http://localhost:5678 MAX_AGE_SECONDS=5 node tools/detector-gateway.js
 */
const http = require('node:http');
const { DatabaseSync } = require('node:sqlite');

const PORT = parseInt(process.env.PORT || '8787', 10);
const HOST = process.env.HOST || '127.0.0.1';
const N8N_URL = process.env.N8N_URL || 'http://localhost:5678';
const WEBHOOK_PATH = process.env.WEBHOOK_PATH || '/webhook/ai-detect-ai';
const N8N_DB = process.env.N8N_DB || 'C:/Users/adam/n8n-runtime/.n8n-data/.n8n/database.sqlite';
const WORKFLOW_ID = process.env.DETECTOR_WORKFLOW_ID || 'ai-detection-ai-only';
const MAX_AGE_SECONDS = Math.max(1, parseInt(process.env.MAX_AGE_SECONDS || '5', 10));
const SWEEP_INTERVAL_MS = Math.max(500, parseInt(process.env.SWEEP_INTERVAL_MS || '2000', 10));

// n8n writes startedAt in the machine's local frame while Date.toISOString() is
// UTC, so any time-based floor silently matches nothing on a clock west of UTC.
// Row ids are monotonic, so "rows this session created" is expressed exactly as
// "id greater than whatever existed when we started" — no clock involved.
let startupMaxId = 0;
let lastMaxId = 0;
let quietSweeps = 0;
// The newest row may belong to an execution still in flight, so it is only
// dropped once nothing new has appeared for this many sweeps.
const QUIET_SWEEPS_BEFORE_LAST = Math.max(2, Math.ceil(MAX_AGE_SECONDS * 1000 / SWEEP_INTERVAL_MS));
let db = null;
try {
  db = new DatabaseSync(N8N_DB);
  db.exec('PRAGMA busy_timeout = 4000');
  // Required: execution_data references execution_entity ON DELETE CASCADE, and
  // SQLite leaves cascades off by default per connection. Without this the
  // parent row disappears while the child keeps the key.
  db.exec('PRAGMA foreign_keys = ON');
  startupMaxId = db.prepare(
    'select coalesce(max(id), 0) as id from execution_entity where workflowId = ?'
  ).get(WORKFLOW_ID).id;
  lastMaxId = startupMaxId;
} catch (e) {
  console.error('cannot open n8n database at', N8N_DB, '-', e.message);
  console.error('the proxy will work, but keys will NOT be purged.');
}

const maxIdStmt = db && db.prepare('select coalesce(max(id), 0) as id from execution_entity where workflowId = ?');
const delNotNewest = db && db.prepare(`
  delete from execution_entity
  where workflowId = ? and id > ? and id < ?
`);
const delAll = db && db.prepare(`
  delete from execution_entity
  where workflowId = ? and id > ?
`);

let purged = 0;
let errors = 0;

function sweep() {
  if (!maxIdStmt) return;
  try {
    const current = maxIdStmt.get(WORKFLOW_ID).id;
    quietSweeps = current === lastMaxId ? quietSweeps + 1 : 0;
    lastMaxId = current;

    // Everything this session created, except (while things are busy) the newest.
    let r = delNotNewest.run(WORKFLOW_ID, startupMaxId, current);
    if (quietSweeps >= QUIET_SWEEPS_BEFORE_LAST) {
      const tail = delAll.run(WORKFLOW_ID, startupMaxId);
      r = { changes: r.changes + tail.changes };
    }
    if (r.changes) {
      purged += r.changes;
      console.log(`purged ${r.changes} execution row(s); total ${purged}; max id now ${current}`);
    }
  } catch (e) {
    errors++;
    console.error('purge failed:', e.message);
  }
}

const timer = setInterval(sweep, SWEEP_INTERVAL_MS);

const UPSTREAM_HEADERS = ['content-type', 'x-primary-api-key', 'x-secondary-api-key'];

// Echo the caller's origin, but only a loopback one. Hardcoding a single value
// would break ng serve on any other port, and reflecting everything would turn a
// loopback-only tool into a CORS-free endpoint any web page can drive.
const LOOPBACK_ORIGIN = /^http:\/\/(localhost|127\.0\.0\.1|\[::1\])(:\d+)?$/;
function cors(res, req) {
  const origin = req.headers.origin;
  if (origin && LOOPBACK_ORIGIN.test(origin)) {
    res.setHeader('Access-Control-Allow-Origin', origin);
  } else {
    res.setHeader('Access-Control-Allow-Origin', `http://${HOST}:${PORT}`);
  }
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type, X-Primary-Api-Key, X-Secondary-Api-Key');
  res.setHeader('Access-Control-Max-Age', '600');
  res.setHeader('Access-Control-Allow-Methods', 'POST, OPTIONS');
  res.setHeader('Vary', 'Origin, Access-Control-Request-Headers');
}

const server = http.createServer((req, res) => {
  // If a handler throws, the process would die and the purge would stop with it.
  // A dead gateway is worse than a slow one, so nothing here is allowed to escape.
  try {
    handle(req, res);
  } catch (e) {
    console.error('handler threw:', e && e.stack ? e.stack : e);
    try {
      res.writeHead(500, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: 'gateway internal error', detail: String(e && e.message) }));
    } catch (_) {
      /* the socket is already gone */
    }
  }
});

function handle(req, res) {
  cors(res, req);
  if (req.method === 'OPTIONS') {
    res.writeHead(204);
    res.end();
    return;
  }
  if (req.url === '/gateway-health') {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({
      ok: true,
      purge_enabled: !!maxIdStmt,
      rows_purged: purged,
      purge_errors: errors,
      max_age_seconds: MAX_AGE_SECONDS,
      startup_max_id: startupMaxId,
      newest_id: lastMaxId,
      quiet_sweeps: quietSweeps,
      quiet_sweeps_before_last: QUIET_SWEEPS_BEFORE_LAST,
    }));
    return;
  }
  if (req.url !== '/detect') {
    res.writeHead(404, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ error: 'unknown path', expected: '/detect' }));
    return;
  }

  const chunks = [];
  req.on('data', (c) => chunks.push(c));
  req.on('end', async () => {
    const headers = {};
    for (const h of UPSTREAM_HEADERS) {
      if (req.headers[h]) headers[h] = req.headers[h];
    }
    const upstreamUrl = new URL(WEBHOOK_PATH.replace('/webhook/', '/webhook/'), N8N_URL);
    const t0 = Date.now();
    try {
      const up = await fetch(upstreamUrl, { method: 'POST', headers, body: Buffer.concat(chunks) });
      const body = Buffer.from(await up.arrayBuffer());
      console.log(`detect -> ${up.status} in ${Date.now() - t0}ms (${body.length} bytes)`);
      res.writeHead(up.status, { 'Content-Type': up.headers.get('content-type') || 'application/json' });
      res.end(body);
    } catch (e) {
      console.error('detect failed:', e.message);
      res.writeHead(502, { 'Content-Type': 'application/json' });
      res.end(JSON.stringify({ error: 'n8n unreachable at ' + N8N_URL, detail: e.message }));
    }
  });
}

console.log('detector gateway');
console.log('  listen        =', `http://${HOST}:${PORT}`);
console.log('  detect POST   =', `-> ${N8N_URL}${WEBHOOK_PATH}`);
console.log('  purge         =', maxIdStmt ? `enabled (${WORKFLOW_ID}, swept every ${SWEEP_INTERVAL_MS}ms)` : 'DISABLED - keys will NOT be purged');
console.log('  startup max id =', startupMaxId, '(rows created before this process are left untouched)');
console.log('  newest row is dropped after', QUIET_SWEEPS_BEFORE_LAST, 'quiet sweeps (~' + (QUIET_SWEEPS_BEFORE_LAST * SWEEP_INTERVAL_MS / 1000) + 's)');

server.listen(PORT, HOST, () => console.log('ready'));

function shutdown() {
  clearInterval(timer);
  server.close(() => process.exit(0));
  if (db) db.close();
  setTimeout(() => process.exit(0), 1500).unref();
}
process.on('SIGINT', shutdown);
process.on('SIGTERM', shutdown);
