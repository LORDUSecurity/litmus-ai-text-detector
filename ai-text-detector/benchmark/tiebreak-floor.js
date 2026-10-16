// What does the tie-break's missing floor actually cost?
//
// The shipped rule gives the secondary lane one cut: at or above `secondary_decision_threshold`
// the tie-break says AI, below it the tie-break says HUMAN. There is no floor, so a secondary
// that scores an AI-leaning probability under its own cut still produces a *confident human
// verdict* — measured live on 2026-10-06: primary 0.62 (defers), secondary 0.85, output
// HUMAN_LIKELY at reported confidence 0.92. That 0.85 is above the primary's own AI cut (0.69).
//
// A floor would convert those confident human verdicts into reviews. That is not free: reviews are
// not answers. This replays the shipped ensemble node through test-ensemble.js `replay` (the real
// Code node, not a reimplementation of its rule) and prints, per floor candidate, how many
// verdicts disappear and how many of those were wrong (a gain) versus right (a cost).
//
//   node benchmark/tiebreak-floor.js <primary.jsonl> <secondary.jsonl>
//   e.g. node benchmark/tiebreak-floor.js benchmark/results-ultra550.jsonl benchmark/results-diffgemma26b.jsonl

const { replay, config } = require('../n8n/test-ensemble.js');

const [primFile, secFile] = process.argv.slice(2);
if (!primFile || !secFile) {
  console.error('usage: node tiebreak-floor.js <primary.jsonl> <secondary.jsonl>');
  process.exit(2);
}

const cfg = config();
const { rows, n } = replay(primFile, secFile, null, true);
const tieHuman = rows.filter((r) => r.method === 'TIE_BREAK_HUMAN');
const contradictory = tieHuman.filter((r) => r.p >= cfg.ai_prob_threshold);

console.log(`replay primary=${primFile.split(/[\\/]/).pop()} secondary=${secFile.split(/[\\/]/).pop()}  n=${n}`);
console.log(`shipped: primary AI >= ${cfg.ai_prob_threshold} / HUMAN <= ${cfg.human_prob_threshold} (conf >= ${cfg.ai_conf_threshold}), `
  + `tie-break AI at secondary >= ${cfg.secondary_decision_threshold}, tie-break HUMAN below it — no floor`);
console.log(`tie-break HUMAN verdicts: ${tieHuman.length}; of them the deciding lane scored >= the primary's AI cut: `
  + `${contradictory.length}`);
for (const r of contradictory) {
  console.log(`  ${r.id}  secondary p=${r.p}  truth=${r.label}  reported ${r.outcome}`);
}

const base = rows.filter((r) => r.outcome !== 'review');
// A floor F means the tie-break may answer HUMAN only when the secondary scored <= F, so the
// shipped rule is F == the AI cut (anything below the cut reads as human) and *lowering* F is what
// moves verdicts into review. Sweeping it upward would be the wrong direction and would report the
// shipped rule as the most restrictive one — which is how an inverted floor looks if nobody checks.
console.log('\nfloor F | secondary in (F, cut) -> review | wrong dropped | right dropped | accuracy over remaining verdicts | answers out of ' + n);
for (const floor of [cfg.secondary_decision_threshold, 0.85, 0.75, cfg.ai_prob_threshold, 0.6, 0.5, 0.4]) {
  const moved = tieHuman.filter((r) => r.p > floor);
  const kept = base.filter((r) => !moved.includes(r));
  const correct = kept.filter((r) => r.outcome === 'correct').length;
  const acc = kept.length ? (correct / kept.length).toFixed(3) : '—';
  console.log(`${floor.toFixed(2)}     | ${String(moved.length).padStart(3)}                            `
    + `| ${String(moved.filter((r) => r.outcome === 'wrong').length).padStart(3)}           `
    + `| ${String(moved.filter((r) => r.outcome === 'correct').length).padStart(3)}             `
    + `| ${acc}                            | ${kept.length}/${n}`);
}
