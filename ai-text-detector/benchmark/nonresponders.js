#!/usr/bin/env node
/**
 * Which measured models never answered, and why.
 *
 * The accuracy table scores what came back; this table scores what did not. It exists because
 * a run that answered 2 of 48 and a run that answered 46 of 48 can share a headline accuracy
 * number, and only the second one is a model measurement -- the first is a gateway incident.
 *
 * Row classification is the backend's, not a reimplementation: `row_never_answered` decides
 * no-answer vs bad-answer by error_code, so this file and the UI can never disagree about
 * whether the provider or the model failed.
 *
 *   node benchmark/nonresponders.js            # every capture, worst yield first
 *   node benchmark/nonresponders.js --retry    # only the captures a resume could still fix
 *   node benchmark/nonresponders.js --codes     # add the error-code histogram
 */
const fs = require('fs');
const path = require('path');

const NO_ANSWER_CODES = new Set(['exception', 'empty_response', 'no_choices', 'null_content']);
const NEVER_ANSWERED = (code) => {
  const c = String(code || '');
  if (c.startsWith('http_') || NO_ANSWER_CODES.has(c)) return true;
  return /^\d+$/.test(c) && parseInt(c, 10) >= 400;
};

const argv = process.argv.slice(2);
const onlyRetry = argv.includes('--retry');
const showCodes = argv.includes('--codes');

// A resume re-asks only the rows that carry no score, so a capture with none is finished
// business: whatever it says about the model is all this corpus will ever say about it.
const files = fs.readdirSync(__dirname).filter((f) => /^results-.*\.jsonl$/.test(f)).sort();
const rows = [];
for (const file of files) {
  const lines = fs.readFileSync(path.join(__dirname, file), 'utf8').split('\n').filter(Boolean);
  const parsed = lines.map((l) => { try { return JSON.parse(l); } catch { return null; } })
    .filter((r) => r && r.id);
  if (!parsed.length) continue;
  const scored = parsed.filter((r) => r.ai_probability != null);
  const unscored = parsed.filter((r) => r.ai_probability == null);
  const noAnswer = unscored.filter((r) => NEVER_ANSWERED(r.error_code));
  const badAnswer = unscored.filter((r) => !NEVER_ANSWERED(r.error_code));
  const codes = {};
  for (const r of unscored) codes[r.error_code || 'null'] = (codes[r.error_code || 'null'] || 0) + 1;
  const attempts = unscored.map((r) => r.attempt).filter((a) => typeof a === 'number');
  rows.push({
    tag: file.replace(/^results-/, '').replace(/\.jsonl$/, ''),
    n: parsed.length,
    scored: scored.length,
    noAnswer: noAnswer.length,
    badAnswer: badAnswer.length,
    yield: parsed.length ? scored.length / parsed.length : 0,
    maxAttempt: attempts.length ? Math.max(...attempts) : 0,
    codes,
    salved: parsed.filter((r) => r.salvaged).length,
    truncated: parsed.filter((r) => r.truncated).length,
  });
}

rows.sort((a, b) => a.yield - b.yield || b.noAnswer - a.noAnswer);
const out = onlyRetry ? rows.filter((r) => r.noAnswer + r.badAnswer > 0) : rows;

const pad = (s, n) => String(s).padEnd(n).slice(0, n);
console.log(pad('TAG', 46) + pad('N', 4) + pad('SCORED', 8) + pad('NO-ANS', 8) + pad('BAD', 5) + pad('YIELD', 7) + 'MAXTRY  SALV TRUNC');
for (const r of out) {
  console.log(pad(r.tag, 46) + pad(r.n, 4) + pad(r.scored, 8) + pad(r.noAnswer, 8) + pad(r.badAnswer, 5)
    + pad(r.yield.toFixed(3), 7) + pad(r.maxAttempt, 7) + pad(r.salved, 6) + r.truncated);
  if (showCodes && Object.keys(r.codes).length) {
    console.log('  ' + Object.entries(r.codes).map(([k, v]) => `${k}:${v}`).join('  '));
  }
}
console.log(`\n${out.length} capture(s) listed, ${rows.length} on disk.`
  + `\nA capture whose MAXTRY is 1 in a --tries >1 run is the harness giving up early, not the gateway refusing.`);
