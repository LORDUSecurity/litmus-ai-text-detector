// Analyze benchmark results: separation metrics, AUC, optimal thresholds, confusion matrices.
// Usage: node analyze.js results-baseline.jsonl [results-other.jsonl ...]

const fs = require('fs');
const path = require('path');

// The policy that shipped before the 2026-09-29 calibration. Its confusion rows are kept
// as the failure evidence, not as the current settings. For the policy that ships now,
// run: node ../n8n/test-ensemble.js replay <primary.jsonl> <secondary.jsonl>
const AI_PROB_T = 0.85, HUMAN_PROB_T = 0.20, CONF_T = 0.70, SOLO_PENALTY = 0.85;

function load(f) {
  return fs.readFileSync(path.join(__dirname, f), 'utf8')
    .split('\n').filter(Boolean).map((l) => JSON.parse(l));
}

function stats(xs) {
  if (!xs.length) return { n: 0 };
  const s = [...xs].sort((a, b) => a - b);
  const mean = xs.reduce((a, b) => a + b, 0) / xs.length;
  const sd = Math.sqrt(xs.reduce((a, b) => a + (b - mean) ** 2, 0) / xs.length);
  const q = (p) => s[Math.min(s.length - 1, Math.floor(p * s.length))];
  // q(0.5) is the nearest-rank element, which for an even list is the UPPER of the middle
  // pair -- it disagreed with the backend panel on the same file (0.850 vs 0.815 for
  // ultra550). median() is the pair mean, the definition every other tool here now uses.
  const med = () => (s.length % 2 ? s[s.length >> 1] : (s[s.length / 2 - 1] + s[s.length / 2]) / 2);
  return { n: xs.length, mean: +mean.toFixed(3), sd: +sd.toFixed(3), min: s[0], p25: q(0.25), median: +med().toFixed(3), p75: q(0.75), max: s[s.length - 1] };
}

// Rank-based AUC with tie correction.
function auc(rows) {
  const scored = rows.filter((r) => r.ok && r.ai_probability !== null);
  const pos = scored.filter((r) => r.label === 'AI');
  const neg = scored.filter((r) => r.label === 'HUMAN');
  if (!pos.length || !neg.length) return null;
  let sum = 0;
  for (const p of pos) {
    for (const n of neg) {
      if (p.ai_probability > n.ai_probability) sum += 1;
      else if (p.ai_probability === n.ai_probability) sum += 0.5;
    }
  }
  return +(sum / (pos.length * neg.length)).toFixed(4);
}

// Cohen's d
function cohend(rows) {
  const a = rows.filter((r) => r.ok && r.label === 'AI').map((r) => r.ai_probability);
  const h = rows.filter((r) => r.ok && r.label === 'HUMAN').map((r) => r.ai_probability);
  if (a.length < 2 || h.length < 2) return null;
  const ma = a.reduce((x, y) => x + y, 0) / a.length;
  const mh = h.reduce((x, y) => x + y, 0) / h.length;
  const va = a.reduce((x, y) => x + (y - ma) ** 2, 0) / (a.length - 1);
  const vh = h.reduce((x, y) => x + (y - mh) ** 2, 0) / (h.length - 1);
  const sp = Math.sqrt(((a.length - 1) * va + (h.length - 1) * vh) / (a.length + h.length - 2));
  return sp === 0 ? null : +((ma - mh) / sp).toFixed(3);
}

// Sweep a single probability threshold; binary decision AI if p >= t.
function sweep(rows) {
  const scored = rows.filter((r) => r.ok && r.ai_probability !== null);
  const out = [];
  for (let t = 0; t <= 1.0001; t += 0.01) {
    const th = +t.toFixed(2);
    let tp = 0, fp = 0, tn = 0, fn = 0;
    for (const r of scored) {
      const predAI = r.ai_probability >= th;
      const isAI = r.label === 'AI';
      if (predAI && isAI) tp++; else if (predAI && !isAI) fp++;
      else if (!predAI && !isAI) tn++; else fn++;
    }
    const acc = (tp + tn) / scored.length;
    const tpr = tp + fn ? tp / (tp + fn) : 0;
    const fpr = fp + tn ? fp / (fp + tn) : 0;
    out.push({ t: th, tp, fp, tn, fn, acc: +acc.toFixed(4), tpr: +tpr.toFixed(4), fpr: +fpr.toFixed(4), j: +(tpr - fpr).toFixed(4) });
  }
  return out;
}

// Three-way classification exactly as the workflow does it on the single-model path.
function classify3(p, conf, aiT, huT, confT, penalty) {
  const c = conf * penalty;
  if (p >= aiT && c >= confT) return 'AI_GENERATED';
  if (p <= huT && c >= confT) return 'HUMAN_LIKELY';
  return 'REVIEW_REQUIRED';
}

function confusion3(rows, aiT, huT, confT, penalty) {
  const scored = rows.filter((r) => r.ok && r.ai_probability !== null);
  const m = {};
  let correct = 0, review = 0;
  for (const r of scored) {
    const pred = classify3(r.ai_probability, r.confidence, aiT, huT, confT, penalty);
    const key = r.label + ' -> ' + pred;
    m[key] = (m[key] || 0) + 1;
    if (pred === 'REVIEW_REQUIRED') review++;
    else if ((r.label === 'AI' && pred === 'AI_GENERATED') || (r.label === 'HUMAN' && pred === 'HUMAN_LIKELY')) correct++;
  }
  return {
    matrix: m,
    n: scored.length,
    accuracy: +(correct / scored.length).toFixed(4),
    correct_of_decided: +(correct / Math.max(1, scored.length - review)).toFixed(4),
    review_rate: +(review / scored.length).toFixed(4)
  };
}

const files = process.argv.slice(2);
if (!files.length) { console.error('pass result files'); process.exit(1); }

for (const f of files) {
  const rows = load(f);
  const ok = rows.filter((r) => r.ok);
  const ai = ok.filter((r) => r.label === 'AI').map((r) => r.ai_probability);
  const hu = ok.filter((r) => r.label === 'HUMAN').map((r) => r.ai_probability);
  const aiC = ok.filter((r) => r.label === 'AI').map((r) => r.confidence);
  const huC = ok.filter((r) => r.label === 'HUMAN').map((r) => r.confidence);

  console.log('\n' + '='.repeat(72));
  console.log('FILE: ' + f + '   usable ' + ok.length + '/' + rows.length);
  console.log('='.repeat(72));
  console.log('ai_probability  AI   :', JSON.stringify(stats(ai)));
  console.log('ai_probability  HUMAN:', JSON.stringify(stats(hu)));
  console.log('confidence      AI   :', JSON.stringify(stats(aiC)));
  console.log('confidence      HUMAN:', JSON.stringify(stats(huC)));
  console.log('AUC=' + auc(rows) + "  Cohen's d=" + cohend(rows) +
    '  (AUC 0.5 = coin flip, 1.0 = perfect)');

  const sw = sweep(rows);
  const bestAcc = sw.reduce((a, b) => (b.acc > a.acc ? b : a));
  const bestJ = sw.reduce((a, b) => (b.j > a.j ? b : a));
  console.log('\nbest-threshold sweep (binary AI if p >= t):');
  console.log('  max accuracy : t=' + bestAcc.t + ' acc=' + bestAcc.acc +
    ' (tp=' + bestAcc.tp + ' fp=' + bestAcc.fp + ' tn=' + bestAcc.tn + ' fn=' + bestAcc.fn + ')');
  console.log("  max Youden's J: t=" + bestJ.t + ' J=' + bestJ.j + ' tpr=' + bestJ.tpr + ' fpr=' + bestJ.fpr);

  console.log('\nper-domain accuracy at best threshold t=' + bestAcc.t + ':');
  const doms = [...new Set(ok.map((r) => r.config))];
  for (const d of doms) {
    const sub = ok.filter((r) => r.config === d);
    const c = sub.filter((r) => (r.ai_probability >= bestAcc.t) === (r.label === 'AI')).length;
    console.log('  ' + d.padEnd(12) + ' ' + c + '/' + sub.length + ' = ' + (c / sub.length).toFixed(2));
  }

  console.log('\nthree-way confusion (workflow single-model logic):');
  const cur = confusion3(rows, AI_PROB_T, HUMAN_PROB_T, CONF_T, SOLO_PENALTY);
  console.log('  PRE-CALIBRATION policy (ai>=0.85, human<=0.20, conf>=0.70, penalty 0.85):');
  console.log('    ' + JSON.stringify(cur.matrix));
  console.log('    accuracy=' + cur.accuracy + ' correct_of_decided=' + cur.correct_of_decided + ' review_rate=' + cur.review_rate);

  const tuned = confusion3(rows, bestJ.t, +(1 - bestJ.t).toFixed(2) > 0.5 ? 0.35 : 0.35, 0.0, 1.0);
  console.log('  TUNED-CANDIDATE (ai>=' + bestJ.t + ', human<=0.35, conf gate off, penalty 1.0):');
  console.log('    ' + JSON.stringify(tuned.matrix));
  console.log('    accuracy=' + tuned.accuracy + ' correct_of_decided=' + tuned.correct_of_decided + ' review_rate=' + tuned.review_rate);
}
