/* Assemble corpus-stress-ai.json from the existing results-stressgen.jsonl WITHOUT
   making any gateway calls (gen-stress-ai.js's append-without-push bug meant the last
   full run wrote 0 rows despite 10 usable articles in the jsonl). Same row shape as
   the generator's assembly step; params reflect the run that produced these rows.
   usage: node assemble-stress-ai.js */
const fs = require('fs');
const path = require('path');
const B = __dirname;

const src = fs.readFileSync(path.join(B, 'gen-stress-ai.js'), 'utf8');
const mm = src.match(/const ALL_PROMPTS = \[([\s\S]*?)\n\];/) || src.match(/PROMPTS = \[([\s\S]*?)\n\];/);
if (!mm) throw new Error('could not extract the prompt list from gen-stress-ai.js');
const PROMPTS = (0, eval)('[' + mm[1] + ']');

const rows = [];
for (const line of fs.readFileSync(path.join(B, 'results-stressgen.jsonl'), 'utf8').split(/\r?\n/)) {
  if (!line.trim()) continue;
  try { rows.push(JSON.parse(line)); } catch { /* skip */ }
}
// The stress band contract is 3000-6000 chars (merge-stress-corpus.js enforces it);
// drop out-of-band rows here so ids stay contiguous and the merge never sees them.
const good = rows.filter((r) => r.text && !r.truncated && !r.short && !r.error &&
  r.chars >= 3000 && r.chars <= 6000);
const corpus = good
  .sort((a, b) => a.prompt_index - b.prompt_index || a.source_model.localeCompare(b.source_model))
  .map((r, i) => ({
    id: 'stress-ai-' + String(i).padStart(3, '0'),
    text: r.text,
    label: 'AI',
    config: { model: r.source_model, base_url: 'https://integrate.api.nvidia.com/v1', temperature: 0.7, top_p: 0.9, max_tokens: 3000 },
    question: PROMPTS[r.prompt_index] || null,
    dataset: 'self-generated',
    source: 'nvidia ' + r.source_model,
    fetched_at: new Date().toISOString(),
    chars: r.chars,
    latency_ms: r.latency_ms,
  }));
fs.writeFileSync(path.join(B, 'corpus-stress-ai.json'), JSON.stringify(corpus, null, 2));
console.log(`assembled ${corpus.length} rows from ${rows.length} jsonl rows (${rows.length - good.length} dropped: truncated/short/errored/out-of-band)`);
const lens = corpus.map((c) => c.chars);
console.log('chars min/median/max:', Math.min(...lens), lens.sort((a, b) => a - b)[Math.floor(lens.length / 2)], Math.max(...lens));
