// Are the stored OpenRouter keys really separate accounts, and does the API show a free-tier
// request cap? Run: node tools/probe-or-accounts.js
//
// Prints no key material. The /api/v1/key response's `label` field is the account's own name for
// the key and OpenRouter hands back a truncated form of the value, so it is hashed down to 8 chars
// and only compared against the others.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

const env = {};
for (const line of fs.readFileSync(path.join(__dirname, '..', '.env'), 'utf8').split(/\r?\n/)) {
  const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/);
  if (m) env[m[1]] = m[2].replace(/^["']|["']$/g, '');
}

const names = Object.keys(env).filter((k) => /^OPENROUTER_API_KEY(_\d+)?$/.test(k)).sort();

(async () => {
  if (!names.length) { console.log('no OPENROUTER_API_KEY* in .env'); process.exitCode = 2; return; }
  const prints = [];
  for (const name of names) {
    const key = env[name];
    if (!key) { console.log(`${name}: not set`); continue; }
    try {
      const r = await fetch('https://openrouter.ai/api/v1/key', {
        headers: { Authorization: 'Bearer ' + key },
      });
      const body = await r.json();
      const d = body?.data ?? body ?? {};
      const fp = d.label ? crypto.createHash('sha256').update(String(d.label)).digest('hex').slice(0, 8) : '-';
      console.log(`${name} (len=${key.length}) -> ${r.status} | account ${fp} | free_tier=${d.is_free_tier}`
        + ` | spend_usage=${d.usage} | spend_limit=${d.limit} | requests_remaining=${d.requests_remaining ?? '-'}`);
      prints.push({ fp, requests_remaining: d.requests_remaining });
    } catch (e) {
      console.log(`${name}: request failed (${e.name})`);
    }
  }
  const accounts = new Set(prints.map((p) => p.fp));
  console.log(`\n${prints.length} key(s) read, ${accounts.size} distinct account label(s).`);
  // `usage` is dollars spent, and it stayed 0 while every chat request returned
  // free-models-per-day -- so headroom has to be asked of a model, not read here.
  if (prints.every((p) => p.requests_remaining === undefined)) {
    console.log('No per-key request counter is exposed: the daily free cap is invisible here.');
    console.log('Use node tools/probe-or-headroom.js to ask a model instead.');
  }
})();
