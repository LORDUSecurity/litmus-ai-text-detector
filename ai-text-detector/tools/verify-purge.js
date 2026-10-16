/*
 * Prove the purge works, and prove what it does NOT do.
 *
 *   1. send a request carrying a distinctive canary key through the gateway
 *   2. read the database immediately  -> the key is at rest (leak window)
 *   3. read it again after the sweep   -> the key is gone
 *   4. count rows that predate this gateway run -> unchanged, on purpose
 *
 * Expects tools/detector-gateway.js to be running.
 */
const { DatabaseSync } = require('node:sqlite');

const GATEWAY = process.env.GATEWAY || 'http://127.0.0.1:8787/detect';
const N8N_DB = process.env.N8N_DB || 'C:/Users/adam/n8n-runtime/.n8n-data/.n8n/database.sqlite';
const WORKFLOW_ID = 'ai-detection-ai-only';
const CANARY = 'CANARY2-transient-' + Date.now();
const WAIT_MS = parseInt(process.env.WAIT_MS || '9000', 10);
const TEXT = 'The rapid advancement of large language models has fundamentally transformed how organisations approach workflow automation and productivity enhancement.';

const db = new DatabaseSync(N8N_DB, { readOnly: true });
const keyRows = () => db.prepare(
  "select count(*) as c from execution_data where data like '%CANARY2%'"
).get().c;
// The rows that existed before this gateway started must survive: the operator
// asked for them to be left alone, and a purge that silently rewrites history is
// not the purge that was agreed.
const legacyRows = () => db.prepare(
  "select count(*) as c from execution_data where data like '%nvapi-%'"
).get().c;
const allRows = () => db.prepare(
  'select count(*) as c from execution_entity where workflowId = ?'
).get(WORKFLOW_ID).c;
const oldest = () => db.prepare(
  'select min(startedAt) as t from execution_entity where workflowId = ?'
).get(WORKFLOW_ID).t;

async function call() {
  const res = await fetch(GATEWAY, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'x-primary-api-key': CANARY, 'x-secondary-api-key': CANARY },
    body: JSON.stringify({ text: TEXT }),
  });
  return { status: res.status, text: await res.text() };
}

(async () => {
  const beforeRows = allRows();
  const beforeLegacy = legacyRows();
  console.log('canary            =', CANARY);
  console.log('rows before call  =', beforeRows, '(oldest startedAt', oldest() + ')');
  console.log('legacy key rows   =', beforeLegacy, '(pre-existing rows, expected to survive)');

  const r = await call();
  const cls = (r.text.match(/"classification":"([^"]+)"/) || [])[1];
  console.log('gateway response  = HTTP', r.status, cls);
  if (r.text.includes(CANARY)) {
    console.log('FAIL: the gateway echoed the key back to the client');
    process.exit(1);
  }

  const immediate = keyRows();
  console.log('\n[1] immediately after the call');
  console.log('    rows containing the canary =', immediate,
    immediate > 0 ? '(this is the leak window the sweep has to close)' : '(none - nothing was persisted at all)');

  console.log(`\n[2] waiting ${WAIT_MS}ms for the sweep ...`);
  await new Promise((res) => setTimeout(res, WAIT_MS));

  const after = keyRows();
  const afterLegacy = legacyRows();
  const rowsAfter = allRows();
  console.log('    rows containing the canary =', after);
  console.log('    legacy key rows            =', afterLegacy, '(was', beforeLegacy + ')');
  console.log('    total rows for workflow    =', rowsAfter, '(was', beforeRows + ')');

  let ok = true;
  if (after !== 0) { console.log('FAIL: the canary key is still at rest'); ok = false; }
  if (immediate === 0) console.log('note: the leak window was not observable in this run (row deleted before the read)');
  if (rowsAfter > beforeRows) { console.log('FAIL: rows accumulated instead of being purged'); ok = false; }
  if (afterLegacy !== beforeLegacy) { console.log('FAIL: the purge touched rows that predate this session'); ok = false; }
  console.log('\n' + (ok ? 'PURGE VERIFIED: BYOK key was at rest, then removed within the sweep window; history untouched.' : 'PURGE NOT VERIFIED'));
  process.exit(ok ? 0 : 1);
})();
