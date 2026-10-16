// Summarise the OpenRouter free-tier screen: one row per results-or-*.jsonl.
//
//   node analyze-free-screen.js
//
// Reported per model:
//   ok/n      parseable answers out of the 12 screened texts
//   AI mean   mean ai_probability on real AI text
//   HU mean   mean ai_probability on real human text
//   sep       AI mean - HU mean (the raw discriminative distance)
//   AUC       P(a random AI text scores above a random human text), ties = 0.5
//   band      how the 12 land under the SHIPPED cuts (AI >= 0.69, HUMAN <= 0.40,
//             confidence >= 0.60): correct / wrong / deferred to the tie-break
//
// AUC is the honest ranking statistic here because the free models sit on their own
// probability scales; a raw accuracy at the shipped cuts penalises a well-ordered
// model that simply reports lower.

const fs = require('fs');
const path = require('path');

const AI_PROB_T = 0.69, HUMAN_PROB_T = 0.40, CONF_T = 0.60;

const dir = __dirname;
const files = fs.readdirSync(dir).filter((f) => /^results-or-.*\.jsonl$/.test(f)).sort();
if (!files.length) {
  console.log('no results-or-*.jsonl yet -- run: bash free-openrouter-screen.sh');
  process.exit(0);
}

const rows = [];
for (const f of files) {
  const lines = fs.readFileSync(path.join(dir, f), 'utf8').split('\n').filter(Boolean);
  const recs = lines.map((l) => JSON.parse(l));
  const ok = recs.filter((r) => r.ok && typeof r.ai_probability === 'number');
  const ai = ok.filter((r) => r.label === 'AI');
  const hu = ok.filter((r) => r.label === 'HUMAN');
  const mean = (a) => (a.length ? a.reduce((s, r) => s + r.ai_probability, 0) / a.length : null);
  const aiMean = mean(ai), huMean = mean(hu);

  let wins = 0, ties = 0, pairs = 0;
  for (const a of ai) for (const h of hu) {
    pairs++;
    if (a.ai_probability > h.ai_probability) wins++;
    else if (a.ai_probability === h.ai_probability) ties++;
  }
  const auc = pairs ? (wins + 0.5 * ties) / pairs : null;

  let correct = 0, wrong = 0, deferred = 0;
  for (const r of ok) {
    const strong = r.confidence >= CONF_T;
    const call = r.ai_probability >= AI_PROB_T && strong ? 'AI'
      : r.ai_probability <= HUMAN_PROB_T && strong ? 'HUMAN' : null;
    if (!call) deferred++;
    else if (call === r.label) correct++;
    else wrong++;
  }

  const model = (recs[0] && recs[0].model) || f.replace(/^results-or-/, '').replace(/\.jsonl$/, '');
  rows.push({
    model, ok: ok.length, n: recs.length,
    aiMean, huMean,
    sep: aiMean !== null && huMean !== null ? aiMean - huMean : null,
    auc, correct, wrong, deferred,
    errors: recs.length - ok.length,
  });
}

rows.sort((a, b) => (b.auc ?? -1) - (a.auc ?? -1) || (b.sep ?? -1) - (a.sep ?? -1));

const pad = (s, n) => String(s).padEnd(n);
const num = (v) => (v === null || v === undefined ? '   —  ' : v.toFixed(3));
console.log(pad('model', 48) + ' ok  AI mean HU mean   sep   AUC   correct/wrong/deferred');
for (const r of rows) {
  console.log(pad(r.model, 48) + pad(`${r.ok}/${r.n}`, 5)
    + ' ' + num(r.aiMean) + '  ' + num(r.huMean) + '  ' + num(r.sep)
    + '  ' + num(r.auc) + '   ' + `${r.correct}/${r.wrong}/${r.deferred}`);
}
console.log('\ncuts: AI >= ' + AI_PROB_T + ', HUMAN <= ' + HUMAN_PROB_T + ', confidence >= ' + CONF_T);
console.log('deferred rows are the gray zone -- in the shipped pipeline the tie-break lane answers those.');
