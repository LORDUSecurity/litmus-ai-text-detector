#!/usr/bin/env node
/* Per-model decision-band fit.

   For every model with 48-scale captures, grid-search (ai_cut, human_cut, conf_gate)
   and pick the sweet spot. Train corpora decide the fit; validation corpora only confirm
   it transfers -- a candidate that wins on train but bleeds on val is rejected by the
   ranking (wrong counted over BOTH, decided over BOTH).

   Decision rule mirrors the deployed single-lane path:
     AI    if conf >= gate && p >= ai_cut
     HUMAN if conf >= gate && p <= human_cut
     REVIEW otherwise
   One-sided human corpora (books) can only produce FPs; one-sided AI corpora (freshai)
   only FNs. That is fine -- they are pure trap checks for the cuts.

   usage: node fit-bands.js [--model <db_id>] [--top 3]
   prints per model: shipped-band score vs best few bands, broken down by corpus.
*/
const fs = require('fs');
const path = require('path');
const B = __dirname;

const arg = (k, d) => { const i = process.argv.indexOf('--' + k); return i > -1 ? process.argv[i + 1] : d; };
const TOP = parseInt(arg('top', '3'), 10);

// model row in models table -> { train: [files], val: [files] }
// files are results-*.jsonl captures; each line: {id,label,ok,salvaged,truncated,ai_probability,confidence,text_length}
const MAP = {
  1:  { name: 'nvidia/nemotron-3-ultra-550b-a55b',
        // CANNOT be re-validated uncapped: the deployed primary id answers HTTP 404
        // "Function id ... : Specified function in account ... is not found" on integrate.api.nvidia.com
        // with both stored NVIDIA keys (measured 2026-10-16, 48/48 rows and a 1-row probe). The id is
        // still listed in GET /v1/models, so the catalogue is not the proof of servability. The lists
        // below are capped-era and are all that exists; the band stays as-fit and is NOT claimed
        // valid in the uncapped regime.
        train: ['ultra550', 'clean550-hc3'],
        val:   ['clean550-holdout', 'bookultra550', 'freshai-550', 'oosstress-nemotron-ultra'] },
  2:  { name: 'google/diffusiongemma-26b-a4b-it',
        // Refit 2026-10-16 (#111) on the post-cap-removal regime, same as rows 30/31. Both legs are
        // uncapped with the current phrase prompt: clean 48/48 answered (1 salvaged, so 47 fit rows)
        // and the disjoint holdout 47/48 (2 salvaged, 1 hard 429 = 45 fit rows). The capped-era lists
        // this replaced -- diffgemma26b, cleandiff-hc3, cleandiff-holdout, bookdiffgemma -- measured
        // a truncated answer length and stay on disk as the historical record.
        train: ['nocap-clean-diffgemma'],
        val:   ['nocap-holdout-diffgemma'] },
  4:  { name: 'nvidia/nemotron-3-ultra-550b-a55b:free',
        train: ['orfull-nvidia-nemotron-3-ultra-550b-a55b-free', 'clean-orfull-nemotron-ultra-free'],
        val:   [] },
  10: { name: 'inclusionai/ling-3.1-flash',
        train: ['orfull-ling-3-1-flash', 'clean-orfull-ling-3-1-flash'],
        val:   [] },
  21: { name: 'google/gemma-4-31b-it',
        train: ['nvfull-gemma-4-31b-it', 'clean-nvfull-gemma-4-31b-it'],
        val:   [] },
  22: { name: 'z-ai/glm-5.3',
        train: ['nvfull-glm-5-3'],
        val:   ['oos-nv-glm-5-3', 'book-nv-glm-5-3', 'oosstress-nv-glm-5-3'] },
  23: { name: 'z-ai/glm-5.3-flash',
        train: ['nvfull-glm-5-3-flash', 'clean-nvfull-glm-5-3-flash'],
        val:   ['ooslong-glm-5-3-flash', 'oosshort-glm-5-3-flash', 'oosstress-nv-glm-5-3-flash'] },
  24: { name: 'nvidia/nemotron-3.5-lightning-30b-a3b',
        train: ['nvfull-nemotron-3-5-lightning-30b', 'clean-nvfull-nemotron-3-5-lightning'],
        val:   [] },
  25: { name: 'deepseek-ai/deepseek-v4.1-flash',
        train: ['nvfull-deepseek-v4-1-flash'],
        val:   [] },
  26: { name: 'moonshotai/kimi-k3',
        train: ['nvfull-kimi-k3'],
        val:   ['nvholdout-kimi-k3', 'nvholdout2-kimi-k3'] },
  30: { name: 'kiro/claude-sonnet-4.5',
        // Refit 2026-10-16 on the post-cap-removal regime: `nocap-clean-*` is the 48-text fit
        // corpus and `nocap-holdout-*` the disjoint 48, both captured uncapped with the current
        // phrase prompt (48/48 usable, 0 truncated, 0 salvaged each). The lists this replaced --
        // omfull-kiro-claude-sonnet-4-5, clean-omfull-kiro-claude-sonnet-4-5, oos-omkiro-sonnet-4-5,
        // clean-oos-omkiro-sonnet-4-5, book-omkiro-sonnet-4-5, oosstress-omkiro-sonnet-4-5 -- are all
        // capped-era and stay on disk as the historical record. Keeping them here would average two
        // answer-length regimes and let a full `--knee` re-run quietly un-move the signed-off cut.
        train: ['nocap-clean-omkiro-sonnet-4-5'],
        val:   ['nocap-holdout-omkiro-sonnet-4-5'] },
  31: { name: 'kiro/claude-haiku-4.5',
        // Refit 2026-10-16, same regime change as row 30: the capped-era lists
        // (omfull-kiro-claude-haiku-4-5, clean-omfull-kiro-claude-haiku-4-5) measured a truncated
        // answer length, and #107 removed the cap. Both new captures are 48/48 usable with the
        // current phrase prompt; the holdout is the disjoint corpus-holdout-clean set.
        train: ['nocap-clean-omkiro-haiku-4-5'],
        val:   ['nocap-holdout-omkiro-haiku-4-5'] },
};

// bands as SAVED in the DB. v3 2026-10-15: K=5 knee refit with 95% acc/decided floor (v2 was
// fewest-wrong-first and over-bought reviews -- see --knee mode). Row 30 moved to v4 2026-10-16:
// same criterion plus the lattice-margin tie-break, fit on uncapped captures, user-signed-off.
// Every other row re-fit under v4 on its existing captures produced the identical band.
const CURRENT = {
  1: [0.70, 0.45, 0.65], 2: [0.90, 0.30, 0.0], 4: [0.70, 0.30, 0.75],
  10: [0.65, 0.60, 0.0], 21: [0.90, 0.35, 0.0], 22: [0.65, 0.30, 0.55],
  23: [0.80, 0.55, 0.50], 24: null, 25: null, 26: [0.70, 0.60, 0.0],
  30: [0.80, 0.30, 0.0], 31: [0.75, 0.35, 0.0],
};

function load(tag) {
  const f = path.join(B, `results-${tag}.jsonl`);
  if (!fs.existsSync(f)) return null;
  const rows = [];
  for (const line of fs.readFileSync(f, 'utf8').split('\n')) {
    if (!line.trim()) continue;
    let r; try { r = JSON.parse(line); } catch { continue; }
    if (!r || typeof r.ai_probability !== 'number' || !r.label) continue;
    if (!r.ok || r.salvaged || r.truncated) continue; // only clean, complete answers
    rows.push({ p: r.ai_probability, c: typeof r.confidence === 'number' ? r.confidence : 1, label: r.label, src: tag });
  }
  return rows.length >= 12 ? rows : null; // screen/probe sizes are not fit data
}

function score(rows, ai, hum, gate) {
  let correct = 0, wrong = 0, review = 0, fp = 0, fn = 0;
  for (const r of rows) {
    const sure = r.c >= gate;
    const v = sure && r.p >= ai ? 'AI' : sure && r.p <= hum ? 'HUMAN' : null;
    if (!v) { review++; continue; }
    if (v === r.label) correct++;
    else { wrong++; if (r.label === 'HUMAN') fp++; else fn++; }
  }
  return { correct, wrong, review, fp, fn, decided: correct + wrong };
}

const perCorpus = (rows, ai, hum, gate) => {
  const by = {};
  for (const r of rows) (by[r.src] = by[r.src] || []).push(r);
  return Object.fromEntries(Object.entries(by).map(([k, v]) => [k, score(v, ai, hum, gate)]));
};

const fmt = s => `c${s.correct} w${s.wrong}(${s.fp}FP/${s.fn}FN) r${s.review}`;

// node fit-bands.js --eval [--model <db_id>] [--val tag1,tag2]
// Scores the SAVED band (CURRENT) of every model over its mapped corpora plus any extra
// --val tags (results-<tag>.jsonl), with a per-corpus breakdown. Used for the stress test.
if (process.argv.includes('--eval')) {
  const onlyId = parseInt(arg('model', '0'), 10) || 0;
  const extra = arg('val', '').split(',').map((s) => s.trim()).filter(Boolean).map(load).filter(Boolean).flat();
  for (const [idStr, spec] of Object.entries(MAP)) {
    const id = parseInt(idStr, 10);
    if (onlyId && id !== onlyId) continue;
    const band = CURRENT[id];
    if (!band) { console.log(`${id} ${spec.name}: no saved band`); continue; }
    const rows = spec.train.map(load).filter(Boolean).concat(spec.val.map(load).filter(Boolean)).flat().concat(extra);
    if (!rows.length) { console.log(`${id} ${spec.name}: no fit-size data`); continue; }
    const s = score(rows, band[0], band[1], band[2]);
    const pc = perCorpus(rows, band[0], band[1], band[2]);
    console.log(`${id} ${spec.name} @ ${band.join('/')} : ${fmt(s)} cov=${(100 * s.decided / rows.length).toFixed(0)}%\n    ` +
      Object.entries(pc).map(([k, v]) => `${k}:${fmt(v)}`).join('  '));
  }
  process.exit(0);
}

// node fit-bands.js --knee [K] [--model <db_id>] [--train tag,tag] [--val tag,tag]
//   [--kneek 5] [--floor 0.95]
//   --train/--val REPLACE the registry's capture lists for this run (see the override note below).
// Coverage-priced selection: instead of "fewest wrong first" (which will spend 40 reviews to
// erase the last error), rank bands by utility U = decided - K*wrong over train+val together.
// K is the price of one wrong call in deferred decisions. K=0 is "decide everything", K=40 is
// the old fewest-wrong-first criterion at this corpus scale. Prints the winner at each of
// K = 0,1,2,5,40 so the sensitivity is visible; the saved pick should use one stated K.
if (process.argv.includes('--knee')) {
  const KS = [0, 1, 2, 5, 40];
  const KF = parseFloat(arg('kneek', '5'));   // shipping K
  const FLOOR = parseFloat(arg('floor', '0.95')); // min accuracy over decided
  const onlyId = parseInt(arg('model', '0'), 10) || 0;
  const picks = [];
  for (const [idStr, spec] of Object.entries(MAP)) {
    const id = parseInt(idStr, 10);
    if (onlyId && id !== onlyId) continue;
    // --train/--val replace the registry lists instead of adding to them. Bands v3 was fit
    // on captures taken under a max_tokens cap; the cap is deleted (2026-10-16), so a
    // re-validation must be able to fit on ONLY the uncapped captures -- appending them to
    // the capped lists would average two answer-length regimes and hide the drift the
    // re-validation exists to find. Overridden lists are echoed so the output always says
    // which captures produced the numbers.
    const tagList = (key, dflt) => {
      const raw = arg(key, '');
      return raw ? raw.split(',').map((s) => s.trim()).filter(Boolean) : dflt;
    };
    const trainTags = tagList('train', spec.train);
    const valTags = tagList('val', spec.val);
    const T = trainTags.map(load).filter(Boolean).flat();
    const V = valTags.map(load).filter(Boolean).flat();
    if (!T.length) { console.log(`${id} ${spec.name}: no train data`); continue; }
    if (arg('train', '') || arg('val', '')) {
      console.log(`  captures  train=[${trainTags.join(', ')}]  val=[${valTags.join(', ')}]`);
    }
    const cands = [];
    // How far a cut sits from any probability the model actually emits. On 2026-10-16 the
    // uncapped sonnet captures produced only 7 distinct values (0.15 0.25 0.72 0.75 0.85 0.88
    // 0.92) and the shipped AI cut 0.75 sat exactly ON one of them, so a borderline human text
    // hinged on the model's own rounding: the same text returned 0.72 in one run and 0.75 in
    // the next, moving it between "review" and a confident AI verdict. Two candidate bands can
    // score identically while one of them is a coin flip and the other is in the middle of an
    // empty gap -- so once decided/wrong/fp are tied, prefer the cut with the larger margin.
    const lattice = [...new Set(T.concat(V).map((r) => r.p))].sort((a, b) => a - b);
    const margin = (ai, hum) => Math.min(
      ...lattice.map((p) => Math.abs(ai - p)),
      ...lattice.map((p) => Math.abs(hum - p)));
    for (let aq = 0.50; aq <= 0.90 + 1e-9; aq += 0.05)
      for (let hq = 0.10; hq <= Math.min(0.60, aq - 0.05) + 1e-9; hq += 0.05) {
        const ai = +aq.toFixed(2), hum = +hq.toFixed(2);
        for (const gate of [0.0, 0.5, 0.55, 0.6, 0.65, 0.7, 0.75]) {
          const st = score(T, ai, hum, gate), sv = score(V, ai, hum, gate);
          cands.push({ ai, hum, gate,
            decided: st.decided + sv.decided, wrong: st.wrong + sv.wrong,
            correct: st.correct + sv.correct,
            fp: st.fp + sv.fp, margin: +margin(ai, hum).toFixed(3), st, sv });
        }
      }
    const cur = CURRENT[id];
    console.log(`\n=== row ${id}  ${spec.name}  (train ${T.length}; val ${V.length})`);
    if (cur) {
      const s = score(T, ...cur), sv = score(V, ...cur);
      console.log(`  saved   ${cur[0]}/${cur[1]}/g${cur[2]}: train ${fmt(s)} | val ${fmt(sv)}`);
    }
    const seen = new Set();
    for (const K of KS) {
      const ranked = cands.slice().sort((a, b) =>
        (b.decided - K * b.wrong) - (a.decided - K * a.wrong) || a.wrong - b.wrong || b.decided - a.decided || a.fp - b.fp
        || b.margin - a.margin);
      const b = ranked[0];
      console.log(`  K=${String(K).padEnd(2)} pick ${b.ai}/${b.hum}/g${b.gate}: decided=${b.decided} wrong=${b.wrong} margin=${b.margin} (train ${fmt(b.st)} | val ${fmt(b.sv)})`);
      seen.add(`${b.ai}/${b.hum}/${b.gate}`);
    }
    if (cur) {
      const s = score(T, ...cur), sv = score(V, ...cur);
      const dAll = s.decided + sv.decided, wAll = s.wrong + sv.wrong;
      // A lower-wrong candidate outscores the saved band once K exceeds
      // (d_saved - d_cand) / (w_saved - w_cand): the deferrals-per-error it demands.
      // min over candidates = the K at which the saved band stops being the pick.
      let flipK = null, dominated = 0;
      for (const b of cands) {
        if (b.wrong < wAll) {
          if (b.decided >= dAll) { dominated++; continue; }
          const k = (dAll - b.decided) / (wAll - b.wrong);
          if (k >= 0 && (flipK === null || k < flipK)) flipK = k;
        }
      }
      const note = dominated > 0
        ? `  | WARNING: ${dominated} candidate band(s) decide MORE and err LESS than the saved band`
        : (flipK !== null ? `  | saved band is the pick for K < ${flipK.toFixed(1)}; above that a lower-wrong band wins` : '');
      console.log(`  saved band: decided=${dAll} wrong=${wAll}${note}`);
    }
    // FINAL: maximize U = decided - KF*wrong subject to accuracy-over-decided >= FLOOR.
    const ok = cands.filter((b) => b.decided >= 12 && b.correct / b.decided >= FLOOR);
    if (ok.length) {
      const b = ok.slice().sort((x, y) =>
        (y.decided - KF * y.wrong) - (x.decided - KF * x.wrong) || x.wrong - y.wrong || y.decided - x.decided || x.fp - y.fp
        || y.margin - x.margin)[0];
      console.log(`  FINAL K=${KF} floor=${FLOOR}: ${b.ai}/${b.hum}/g${b.gate} -> decided=${b.decided} wrong=${b.wrong} acc/dec=${(100 * b.correct / b.decided).toFixed(1)}% margin=${b.margin} (train ${fmt(b.st)} | val ${fmt(b.sv)})`);
      picks.push({ row_id: id, model: spec.name, ai: b.ai, hum: b.hum, gate: b.gate,
        decided: b.decided, wrong: b.wrong, acc_decided: +(b.correct / b.decided).toFixed(3),
        margin: b.margin, lattice: lattice.join(' '),
        changed: !cur || cur[0] !== b.ai || cur[1] !== b.hum || cur[2] !== b.gate });
    } else {
      console.log(`  FINAL K=${KF} floor=${FLOOR}: NO band meets the floor — leave saved band alone`);
      picks.push({ row_id: id, model: spec.name, ai: null, hum: null, gate: null, changed: false,
        note: 'no band meets floor' });
    }
  }
  fs.writeFileSync(path.join(B, 'knee-picks.json'), JSON.stringify(picks, null, 1));
  console.log('\nwrote knee-picks.json');
  process.exit(0);
}

const out = [];
for (const [idStr, spec] of Object.entries(MAP)) {
  const id = parseInt(idStr, 10);
  const train = spec.train.map(load).filter(Boolean);
  const val = spec.val.map(load).filter(Boolean);
  if (!train.length) { console.log(`${id} ${spec.name}: no fit-size train data`); continue; }
  const T = train.flat(), V = val.flat();
  const best = [];
  // Round to the 2-decimal value that would actually be SAVED (>= comparisons are
  // exact on doubles: an accumulated 0.85000000000000003 moves rows sitting exactly on
  // the cut from verdict to review compared with the DB's 0.85).
  for (let aq = 0.50; aq <= 0.90 + 1e-9; aq += 0.05)
    for (let hq = 0.10; hq <= Math.min(0.60, aq - 0.05) + 1e-9; hq += 0.05) {
      const ai = +aq.toFixed(2), hum = +hq.toFixed(2);
      for (const gate of [0.0, 0.5, 0.55, 0.6, 0.65, 0.7, 0.75]) {
        const st = score(T, ai, hum, gate), sv = score(V, ai, hum, gate);
        // sweet spot: fewest wrong over train+val, then most decided, then fewest FPs
        best.push({ ai: +ai.toFixed(2), hum: +hum.toFixed(2), gate,
          wrong: st.wrong + sv.wrong, decided: st.decided + sv.decided,
          fp: st.fp + sv.fp, st, sv, pcT: perCorpus(T, ai, hum, gate), pcV: perCorpus(V, ai, hum, gate) });
      }
    }
  best.sort((a, b) => a.wrong - b.wrong || b.decided - a.decided || a.fp - b.fp);
  const seen = new Set(); const top = best.filter(b => {
    const k = `${b.ai}/${b.hum}/${b.gate}`; if (seen.has(k)) return false; seen.add(k); return true;
  }).slice(0, TOP);

  console.log(`\n=== row ${id}  ${spec.name}  (train ${T.length} rows: ${train.map(f => f.length).join('+')}; val ${V.length})`);
  const cur = CURRENT[id];
  if (cur) {
    const s = score(T, ...cur), sv = score(V, ...cur);
    console.log(`  shipped ${cur[0]}/${cur[1]}/g${cur[2]}: train ${fmt(s)} | val ${fmt(sv)}`);
  }
  for (const b of top) {
    console.log(`  FIT ${b.ai}/${b.hum}/g${b.gate}: wrong=${b.wrong} decided=${b.decided} (${b.st.decided + b.sv.decided ? ((b.st.correct + b.sv.correct) / (b.st.decided + b.sv.decided) * 100).toFixed(1) : '-'}%)`);
    console.log(`      train ${fmt(b.st)}  val ${fmt(b.sv)}`);
    const det = Object.entries({ ...b.pcT, ...b.pcV }).map(([k, s]) => `${k}:${fmt(s)}`).join('  ');
    console.log(`      ${det}`);
    out.push({ row_id: id, model: spec.name, ai_cut: b.ai, human_cut: b.hum, conf_gate: b.gate,
      wrong_all: b.wrong, decided_all: b.decided, fp_all: b.fp, per_corpus: { train: b.pcT, val: b.pcV } });
  }
}
fs.writeFileSync(path.join(B, 'bands-fit-candidates.json'), JSON.stringify(out, null, 1));
console.log('\nwrote bands-fit-candidates.json (all top picks; decision + save is the next step)');
