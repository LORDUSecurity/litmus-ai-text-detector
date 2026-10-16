/* Save knee picks into the `models` table through the real validated PATCH endpoint, then
   re-read from the store to prove what actually landed.

   Since #100 every mutating endpoint is admin-only and even GET /models requires a session, so
   this client logs in first and carries the Flask signed-cookie session. (The original version of
   this file did an unauthenticated fetch, which the guard answered with 401 AUTH_REQUIRED on every
   row -- it could not write to an auth'd backend at all.)

   usage:
     BACKEND_API=http://127.0.0.1:8787 ADMIN_USERNAME=admin ADMIN_PASSWORD=... \
       node benchmark/patch-bands-v3.js                 # write the rows the fitter changed
     node benchmark/patch-bands-v3.js --dry             # show the bodies, spend no write
     node benchmark/patch-bands-v3.js --all             # rewrite every row in the file (idempotent)
     node benchmark/patch-bands-v3.js --row 30          # just one row
     PICKS_FILE=other-picks.json node benchmark/patch-bands-v3.js   # a different fitter output

   BACKEND_API is the ORIGIN only -- no path. `backend.app` serves the API at the root, and the
   guard in backend/auth.py compares against prefix-less PUBLIC_PATHS, so a `/api` here is not a
   route: POST /api/auth/login answers 401 {"error_code":"AUTH_REQUIRED"} even with a correct
   password (measured 2026-10-16 on 127.0.0.1:8787). `/api` is a browser-side convention
   (frontend/src/app/core/backend.service.ts) that the SPA proxy rewrites away before it hits
   Flask, which is why the UI and this tool need different bases.
   The password is read from the environment only -- it is never an argument, never echoed,
   and the log prints a masked fingerprint. Rows skipped are always named: a run that writes
   nothing must say so rather than print nothing.
*/
const fs = require('fs');
const path = require('path');
const B = __dirname;
const API = (process.env.BACKEND_API || 'http://127.0.0.1:8787').replace(/\/+$/, '');
const USER = process.env.ADMIN_USERNAME || 'admin';
const PASS = process.env.ADMIN_PASSWORD || '';
const args = process.argv.slice(2);
const dry = args.includes('--dry');
const all = args.includes('--all');
const onlyRow = args.includes('--row') ? Number(args[args.indexOf('--row') + 1]) : null;

// EOL model: its stored band is a measurement record, nothing to deploy.
const SKIP_ROWS = new Set([26]);
// No validation corpus for these -> the fit is in-sample, and the note must say so.
const TRAIN_ONLY = new Set([4, 10, 21, 31]);

const NOTE = 'band v4 2026-10-16: K=5 knee refit (utility = decided - 5*wrong, floor acc/decided >= 0.95, ' +
  'ties broken by distance from the probability values the model actually emits) on captures taken AFTER the ' +
  'token cap was removed. v3 (2026-10-15) was fit under a max_tokens cap; re-running v4 over every model left ' +
  'all other saved bands unchanged, so a row moving here moved on its own evidence.';

const BAND_FIELDS = ['ai_prob_threshold', 'human_prob_threshold', 'ai_conf_threshold', 'human_conf_threshold'];

const raw = JSON.parse(fs.readFileSync(
  path.join(B, process.env.PICKS_FILE || 'knee-picks.json'), 'utf8'));
const picks = Array.isArray(raw) ? raw : [raw];

let jar = '';
const fingerprint = (s) => (s ? `sha1:${require('crypto').createHash('sha1').update(s).digest('hex').slice(0, 8)}` : 'EMPTY');

async function call(p, method, body) {
  const res = await fetch(`${API}${p}`, {
    method,
    headers: { 'content-type': 'application/json', ...(jar ? { cookie: jar } : {}) },
    ...(body ? { body: JSON.stringify(body) } : {}),
  });
  const setCookies = typeof res.headers.getSetCookie === 'function'
    ? res.headers.getSetCookie()
    : (res.headers.get('set-cookie') ? [res.headers.get('set-cookie')] : []);
  for (const c of setCookies) {
    const kv = c.split(';')[0].trim();
    if (!kv.includes('=')) continue;
    const name = kv.split('=')[0];
    jar = [jar.split('; ').filter((x) => x && !x.startsWith(name + '=')), kv].flat().filter(Boolean).join('; ');
  }
  let json = null;
  const type = res.headers.get('content-type') || 'no content-type';
  try { json = await res.json(); } catch { /* non-JSON body: the status + type carry the answer */ }
  return { status: res.status, json, type };
}

// A non-JSON body is never a credential problem, and reading it as one wastes a debugging cycle.
// Two shapes seen in practice: text/html = a static SPA server answered (wrong port, or a second
// process bound to the same port), and AUTH_REQUIRED = a /api prefix on a prefix-less guard.
function explain(status, type, json, endpoint) {
  if (json === null) {
    return `the response was not JSON (${type}, status ${status}) -- ${API} is not an API origin. ` +
      `A page server answers there: check the port is the backend's, and that nothing else is ` +
      `already bound to it (netstat -ano | grep :<port>). Tried: POST ${endpoint}`;
  }
  if (json && json.error_code === 'AUTH_REQUIRED') {
    return `${status} AUTH_REQUIRED on ${endpoint} -- BACKEND_API must be an origin with no path; ` +
      `the guard compares prefix-less paths, so a "/api" here is an unknown route, not the login.`;
  }
  return `${status} ${JSON.stringify(json).slice(0, 200)}`;
}

async function login() {
  const endpoint = `${API}/auth/login`;
  const { status, json, type } = await call('/auth/login', 'POST', { username: USER, password: PASS });
  const role = json && json.user && json.user.role;
  if (status !== 200 || !json || !json.user) {
    const why = json && json.error_code === 'BAD_CREDENTIALS'
      ? `${status} BAD_CREDENTIALS for "${USER}" -- the origin is right and the credentials are not`
      : explain(status, type, json, endpoint);
    console.error(`login FAILED at ${endpoint} (credential ${fingerprint(PASS)}): ${why}`);
    process.exit(3);
  }
  if (role !== 'admin') {
    console.error(`logged in as "${USER}" but role is "${role}" -- PATCH /models is admin-only. Exiting.`);
    process.exit(4);
  }
  console.log(`session OK: "${USER}" role=admin via ${API} (credential ${fingerprint(PASS)}, never printed)`);
}

async function listModels() {
  const { status, json, type } = await call('/models', 'GET');
  if (status !== 200 || !Array.isArray(json)) {
    console.error(`GET /models -> ${explain(status, type, json, `${API}/models`)}`);
    process.exit(5);
  }
  return json;
}

const bandOf = (m) => BAND_FIELDS.map((f) => (m && m[f] === undefined ? '?' : m[f])).join('/');

function bodyFor(p) {
  return {
    ai_prob_threshold: p.ai,
    human_prob_threshold: p.hum,
    ai_conf_threshold: p.gate,
    human_conf_threshold: p.gate,
    notes: `${NOTE} train+val: ${p.decided} decided, ${p.wrong} wrong, acc/decided ${p.acc_decided}` +
      (typeof p.margin === 'number'
        ? `, cut sits ${p.margin} away from the values this model emits (${p.lattice}).`
        : '.') +
      (TRAIN_ONLY.has(p.row_id) ? ' TRAIN-ONLY fit: in-sample, provisional until a disjoint capture lands.' : ''),
  };
}

(async () => {
  if (dry) {
    for (const p of picks) console.log(`DRY row ${p.row_id} ${p.model}: ${JSON.stringify(bodyFor(p))}`);
    console.log(`(dry run: ${picks.length} row(s) in knee-picks.json, no login, no write)`);
    return;
  }
  if (!PASS) {
    console.error('ADMIN_PASSWORD is unset -- refusing to guess. This tool writes the bands every ' +
      'user gets as defaults, so it needs an explicit admin session.');
    process.exit(2);
  }
  await login();

  const written = [];
  const skipped = [];
  for (const p of picks) {
    if (SKIP_ROWS.has(p.row_id)) { skipped.push(`row ${p.row_id} ${p.model}: EOL, never patched`); continue; }
    if (onlyRow !== null && p.row_id !== onlyRow) { skipped.push(`row ${p.row_id}: not selected by --row`); continue; }
    if (!all && p.changed === false) {
      skipped.push(`row ${p.row_id} ${p.model}: changed=false (band already ${p.ai}/${p.hum}/g${p.gate}) -- use --all to rewrite`);
      continue;
    }
    const before = (await listModels()).find((m) => m.id === p.row_id) || {};
    const res = await call(`/models/${p.row_id}`, 'PATCH', bodyFor(p));
    if (res.status !== 200) {
      console.error(`row ${p.row_id} ${p.model}: PATCH ${res.status} ${JSON.stringify(res.json).slice(0, 200)}`);
      process.exitCode = 1;
      continue;
    }
    const after = (await listModels()).find((m) => m.id === p.row_id) || {};
    const want = [p.ai, p.hum, p.gate, p.gate].map(Number).join('/');
    const got = bandOf(after);
    const ok = want === got;
    console.log(`row ${p.row_id} ${p.model}: PATCH 200, band ${bandOf(before)} -> ${got} ` +
      `(want ${want}) ${ok ? 'VERIFIED from re-read' : 'MISMATCH'}` +
      (ok ? `, lane=${after.lane} max_tokens=${after.max_tokens}` : ''));
    if (!ok) process.exitCode = 1;
    written.push(p.row_id);
  }

  console.log(`\n${written.length} row(s) written${written.length ? ': ' + written.join(', ') : ''}`);
  for (const s of skipped) console.log(`skipped: ${s}`);
  if (!written.length) {
    console.log('Nothing was written. That is a result, not a silence: every row in knee-picks.json ' +
      'is either unchanged or excluded. Refit first (benchmark/fit-bands.js --knee) or pass --all.');
  }
})().catch((e) => {
  console.error(`patch-bands-v3 aborted: ${e && e.message}`);
  process.exitCode = 1;
});
