// Throwaway probe: what does NVIDIA actually serve for the deployed primary id?
// Prints model ids and match candidates only; never prints a key.
import fs from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';

const root = path.dirname(path.dirname(fileURLToPath(import.meta.url))) + '/';
const env = Object.fromEntries(
  fs.readFileSync(root + '.env', 'utf8').split(/\r?\n/)
    .filter((l) => /^[A-Z0-9_]+=/.test(l))
    .map((l) => [l.slice(0, l.indexOf('=')), l.slice(l.indexOf('=') + 1)]),
);
const base = env.PRIMARY_BASE_URL;
const key = env.PRIMARY_API_KEY;

const res = await fetch(base + '/models', { headers: { authorization: 'Bearer ' + key } });
console.log('GET ' + base + '/models ->', res.status);
if (!res.ok) {
  console.log((await res.text()).slice(0, 400));
  process.exit(0);
}
const body = await res.json();
const ids = (body.data || body.models || []).map((m) => (typeof m === 'string' ? m : m.id));
console.log('total ids:', ids.length);
const hits = ids.filter((i) => /nemotron/i.test(i));
console.log('nemotron-family ids (' + hits.length + '):');
for (const h of hits.sort()) console.log('  ' + h);
const want = (env.PRIMARY_MODEL || '').replace(/^nvidia\//, '');
console.log('\ndeployed primary id:', env.PRIMARY_MODEL);
console.log('exact-listed:', ids.includes(env.PRIMARY_MODEL), '| substring matches:',
  ids.filter((i) => i.includes(want)).length);
