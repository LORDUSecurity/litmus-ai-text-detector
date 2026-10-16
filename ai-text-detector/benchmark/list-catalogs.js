// Cheap catalogue listing: GET /v1/models for each configured gateway and print the model ids.
// This is NOT a usability probe (a listed id is only what the account MIGHT be entitled to; a real
// request is needed to prove it answers -- that is catalog-probe.js). It exists to answer "what is
// available now, and what is NEW since the last captured matrix", so the clean-text sweep can target
// the full current set ("all and what we will have coming") instead of a stale list.
//
//   node list-catalogs.js [--base_env NAME --key_env NAME ...]  (defaults to the three providers)
//   node list-catalogs.js --grep nemotron       (only show ids matching a regex)
//
// Never prints an API key. Reads the last captured matrix files to compute a NEW-set diff.

const fs = require('fs');
const path = require('path');

const env = {};
for (const l of fs.readFileSync(path.join(__dirname, '..', '.env'), 'utf8').split(/\r?\n/)) {
  if (l.trim().startsWith('#')) continue;
  const m = l.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/);
  if (m) env[m[1]] = m[2].replace(/^["']|["']$/g, '');
}

const argv = process.argv.slice(2);
const arg = (n, d) => { const i = argv.indexOf('--' + n); return i >= 0 && argv[i + 1] !== undefined && !argv[i + 1].startsWith('--') ? argv[i + 1] : d; };
const GREP = arg('grep', null);

const PROVIDERS = [
  { name: 'NVIDIA (paid/primary)', base: env.PRIMARY_BASE_URL, key: env.PRIMARY_API_KEY },
  { name: 'OpenRouter', base: env.OPENROUTER_BASE_URL, key: env.OPENROUTER_API_KEY },
  { name: 'OmniRoute (local)', base: env.OMNIROUTE_BASE_URL, key: env.OMNIROUTE_API_KEY },
];

// Known matrix from the manifest: every (base_url, model) pair already benchmarked at least once.
function knownPairs() {
  const set = new Set();
  const mf = path.join(__dirname, 'runs-manifest.log');
  if (!fs.existsSync(mf)) return set;
  for (const line of fs.readFileSync(mf, 'utf8').split(/\r?\n/)) {
    const m = line.match(/^params:\s*(\{.*\})\s*$/);
    if (!m) continue;
    try { const j = JSON.parse(m[1]); if (j.model && j.base_url) set.add(j.base_url + '::' + j.model); } catch (e) {}
  }
  return set;
}

(async () => {
  const known = knownPairs();
  const knownModels = new Set([...known].map((k) => k.split('::')[1]));
  console.log('known model ids already benchmarked: ' + knownModels.size);

  for (const p of PROVIDERS) {
    if (!p.base || !p.key) { console.log('\n### ' + p.name + ': NOT CONFIGURED, skipped'); continue; }
    let ids;
    try {
      const res = await fetch(p.base.replace(/\/$/, '') + '/models', { headers: { Authorization: 'Bearer ' + p.key, Accept: 'application/json' } });
      if (!res.ok) { console.log('\n### ' + p.name + ': HTTP ' + res.status + ' ' + (await res.text()).slice(0, 120)); continue; }
      const j = await res.json();
      ids = (j.data || j.models || []).map((x) => x.id || x.name || x).filter((x) => typeof x === 'string');
    } catch (e) { console.log('\n### ' + p.name + ': fetch error ' + e.message); continue; }

    ids.sort();
    let shown = ids;
    if (GREP) shown = ids.filter((x) => new RegExp(GREP).test(x));
    const fresh = shown.filter((x) => !knownModels.has(x));
    console.log('\n### ' + p.name + ': ' + ids.length + ' listed' + (GREP ? ', ' + shown.length + ' match /' + GREP + '/' : '') + ', ' + fresh.length + ' never-benchmarked');
    for (const id of shown) {
      const isNew = !knownModels.has(id);
      console.log('   ' + (isNew ? '* ' : '  ') + id + (isNew ? '   [NEW]' : ''));
    }
  }
  console.log('\n(* / [NEW] = not present in runs-manifest.log, i.e. a new arrival to consider probing.)');
})();
