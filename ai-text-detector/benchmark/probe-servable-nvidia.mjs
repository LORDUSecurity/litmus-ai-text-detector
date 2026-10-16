// Throwaway probe (2026-10-16): which stored NVIDIA model ids actually serve a chat completion?
// GET /v1/models listing is NOT the evidence -- nemotron-3-ultra-550b is listed and still 404s.
// Prints id, HTTP status, and a short note. Never prints a key.
import fs from 'node:fs';

const root = 'C:/Users/adam/Desktop/humanizer ultra/ai-text-detector/';
const env = Object.fromEntries(fs.readFileSync(root + '.env', 'utf8').split(/\r?\n/)
  .filter((l) => /^[A-Z0-9_]+=/.test(l)).map((l) => [l.slice(0, l.indexOf('=')), l.slice(l.indexOf('=') + 1)]));
const base = env.PRIMARY_BASE_URL, key = env.PRIMARY_API_KEY;

const IDS = [
  'nvidia/nemotron-3-ultra-550b-a55b',      // row 1 - deployed primary
  'google/diffusiongemma-26b-a4b-it',       // row 2 - deployed tie-break
  'google/gemma-4-31b-it',                  // row 21
  'z-ai/glm-5.3',                           // row 22
  'z-ai/glm-5.3-flash',                     // row 23
  'nvidia/nemotron-3.5-lightning-30b-a3b',  // row 24
  'deepseek-ai/deepseek-v4.1-flash',        // row 25
  'moonshotai/kimi-k3',                     // row 26 - 410 EOL on 2026-10-15
];

async function probe(id) {
  const t0 = Date.now();
  try {
    const r = await fetch(base + '/chat/completions', {
      method: 'POST',
      headers: { authorization: 'Bearer ' + key, 'content-type': 'application/json' },
      body: JSON.stringify({ model: id, messages: [{ role: 'user', content: 'Reply with exactly: OK' }], temperature: 0.1, max_tokens: 16 }),
    });
    const txt = await r.text();
    let note;
    try {
      const j = JSON.parse(txt);
      note = j.choices ? 'content=' + JSON.stringify((j.choices[0].message.content || j.choices[0].message.reasoning || '')).slice(0, 24)
        : JSON.stringify(j).slice(0, 160);
    } catch { note = txt.slice(0, 160); }
    console.log(String(r.status).padEnd(4), (Date.now() - t0 + 'ms').padEnd(9), id.padEnd(38), note.replace(/\s+/g, ' '));
  } catch (e) { console.log('ERR ', id, String(e.message).slice(0, 80)); }
}

for (let i = 0; i < IDS.length; i += 2) {
  await Promise.all([probe(IDS[i]), probe(IDS[i + 1])].filter(Boolean));
  await new Promise((r) => setTimeout(r, 1200));
}
