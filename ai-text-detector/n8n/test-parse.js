/*
 * Offline test for the shipped `Parse Primary` code node, read out of ai-only-workflow.json so it
 * tests the artefact that gets imported into n8n rather than a copy that can drift.
 *
 * The cases here are about ONE thing: what a max_tokens cut does to the score. A cut answer is
 * recoverable only when the number itself survived -- the salvage regex matches the digits that
 * are present, so a cap that lands inside `0.78` yields `0.7`, and one that lands right after the
 * decimal yields `0`. `0` is not a missing answer, it is a confident "human" verdict on machine
 * text, and it is the worst failure this parser can make. So: salvage whole numbers, refuse ones
 * that ran to the end of a truncated string.
 *
 *   node n8n/test-parse.js
 */
const { readFileSync } = require('node:fs');
const { join } = require('node:path');

const WF = JSON.parse(readFileSync(join(__dirname, 'ai-only-workflow.json'), 'utf8'));
const NODE = WF.nodes.find((n) => n.name === 'Parse Primary');
if (!NODE) throw new Error('Parse Primary node not found in ai-only-workflow.json');

const run = new Function('$input', '$', NODE.parameters.jsCode);

const CFG = {
  primary_model: 'test/model',
  ai_prob_threshold: 0.69,
  ai_conf_threshold: 0.6,
  human_prob_threshold: 0.4,
  human_conf_threshold: 0.6,
};

// The shipped gate reads `cfg.secondary_enabled !== false`, so a cfg with no key at
// all (every fixture above) means ENABLED. A missing flag must never silently turn
// the tie-break off -- that is the whole defensive-default rule for this feature.
const CFG_OFF = Object.assign({}, CFG, { secondary_enabled: false });

function parse(content, finish, cfg) {
  const payload = { choices: [{ message: { content }, finish_reason: finish || 'stop' }] };
  const out = run(
    { first: () => ({ json: payload }) },
    () => ({ first: () => ({ json: { config: cfg || CFG } }) }),
  );
  return out[0].json;
}

const cases = [];
const check = (name, cond, detail) => cases.push({ name, pass: !!cond, detail });

// 1. A complete answer parses and, being decisive, does not buy a second opinion.
const whole = parse('{"ai_probability": 0.9, "confidence": 0.9, "signals": []}', 'stop');
check('complete JSON parses', whole.ok === true, JSON.stringify(whole).slice(0, 160));
check('complete decisive answer needs no secondary', whole.need_secondary === false,
  'need_secondary=' + whole.need_secondary);

// 2. A cut AFTER a whole number still yields that number, flagged as salvaged, and the salvaged
// flag alone buys the second opinion even when the number looks decisive.
const afterNumber = parse('{"ai_probability": 0.9, "confidence": 0.9, "signals": [{"name": "rhy', 'length');
check('cut after a complete number salvages it', afterNumber.ok === true
  && afterNumber.result.ai_probability === 0.9, JSON.stringify(afterNumber).slice(0, 160));
check('salvage is flagged', afterNumber.salvaged === true, 'salvaged=' + afterNumber.salvaged);
check('salvaged decisive-looking answer still escalates', afterNumber.need_secondary === true,
  'need_secondary=' + afterNumber.need_secondary);

// 3. The hazard: a cut INSIDE the number must not be read as the digits that happen to be there.
const midDecimal = parse('{"ai_probability": 0.', 'length');
check('cut at "0." is not read as 0', midDecimal.ok === false,
  JSON.stringify(midDecimal).slice(0, 200));
const midDigits = parse('{"ai_probability": 0.8', 'length');
check('cut at "0.8" is not read as 0.8', midDigits.ok === false,
  JSON.stringify(midDigits).slice(0, 200));
const cutBefore = parse('{"id": 7, "ai_probability": 0.7', 'length');
check('cut mid-number fails rather than deferring to a salvage', cutBefore.ok === false
  && cutBefore.error_code === 'json_parse_failed', JSON.stringify(cutBefore).slice(0, 200));

// 4. A failed parse must always buy the second opinion -- never silently decide alone.
check('a failed parse escalates', midDecimal.need_secondary === true,
  'need_secondary=' + midDecimal.need_secondary);

// 5. Only the probability survives (confidence cut): the number is whole, so it is still salvage,
// and confidence falls back to the neutral 0.5 rather than to a missing field.
const noConf = parse('{"ai_probability": 0.85, "confidence": ', 'length');
check('probability-only salvage', noConf.ok === true && noConf.result.ai_probability === 0.85
  && noConf.result.confidence === 0.5, JSON.stringify(noConf).slice(0, 200));

// 6. A prose answer with no JSON at all keeps its own error code (regression guard).
const prose = parse('I think this text is human written.', 'stop');
check('no-json answer reports no_json_object', prose.ok === false
  && prose.error_code === 'no_json_object', JSON.stringify(prose).slice(0, 160));

// 7. The optional-lane gates. Gray zone with the lane ON still escalates; with the lane
// OFF the parse must not flag a second opinion -- that deferral becomes REVIEW_REQUIRED
// downstream, and buying the call anyway is the bug this pins out. A failed parse under
// OFF likewise stops escalating: there is no second opinion to buy.
const grayOff = parse('{"ai_probability": 0.55, "confidence": 0.9, "signals": []}', 'stop', CFG_OFF);
check('gray zone still escalates when the flag is absent (defensive on)',
  whole.need_secondary === false && parse('{"ai_probability": 0.55, "confidence": 0.9, "signals": []}', 'stop').need_secondary === true,
  'need_secondary=' + parse('{"ai_probability": 0.55, "confidence": 0.9, "signals": []}', 'stop').need_secondary);
check('gray zone does NOT escalate when the secondary lane is off',
  grayOff.ok === true && grayOff.need_secondary === false, JSON.stringify(grayOff).slice(0, 160));
const cutOff = parse('{"ai_probability": 0.', 'length', CFG_OFF);
check('failed parse does NOT escalate when the secondary lane is off',
  cutOff.ok === false && cutOff.need_secondary === false, JSON.stringify(cutOff).slice(0, 160));

// 8. Phrase evidence: sanitized passthrough, backward compatible with answers that predate it.
const withPhrases = parse('{"ai_probability": 0.9, "confidence": 0.9, "signals": [], "phrases": ['
  + '{"quote": "  In conclusion, it is worth noting that  ", "direction": "ai", "strength": 0.85},'
  + '{"quote": "I remember the summer of 1912", "direction": "HUMAN", "strength": 1.7},'
  + '{"quote": "", "direction": "AI", "strength": 0.9},'
  + '{"quote": 42, "direction": "AI", "strength": 0.9},'
  + '{"quote": "no strength here", "direction": "banana"}'
  + ']}', 'stop');
check('valid phrases pass through sanitized', withPhrases.ok === true
  && withPhrases.result.phrases.length === 3, JSON.stringify(withPhrases.result.phrases).slice(0, 200));
check('quote is trimmed', withPhrases.ok
  && withPhrases.result.phrases[0].quote === 'In conclusion, it is worth noting that',
  JSON.stringify(withPhrases.result.phrases[0]).slice(0, 160));
check('direction normalizes case, junk defaults to AI', withPhrases.ok
  && withPhrases.result.phrases[0].direction === 'AI'
  && withPhrases.result.phrases[1].direction === 'HUMAN'
  && withPhrases.result.phrases[2].direction === 'AI',
  JSON.stringify(withPhrases.result.phrases.map((p) => p.direction)));
check('strength clamps to [0,1], missing defaults to 0.5', withPhrases.ok
  && withPhrases.result.phrases[0].strength === 0.85
  && withPhrases.result.phrases[1].strength === 1
  && withPhrases.result.phrases[2].strength === 0.5,
  JSON.stringify(withPhrases.result.phrases.map((p) => p.strength)));
const longQuote = parse('{"ai_probability": 0.9, "confidence": 0.9, "phrases": [{"quote": "'
  + 'x'.repeat(400) + '", "direction": "AI", "strength": 0.9}]}', 'stop');
check('oversized quote is sliced to 200 chars', longQuote.ok
  && longQuote.result.phrases[0].quote.length === 200, 'len=' + (longQuote.result.phrases[0] || {}).length);
const noPhrases = parse('{"ai_probability": 0.9, "confidence": 0.9, "signals": []}', 'stop');
check('answer without phrases stays backward compatible', noPhrases.ok === true
  && Array.isArray(noPhrases.result.phrases) && noPhrases.result.phrases.length === 0,
  JSON.stringify(noPhrases.result.phrases));
check('salvaged cut carries no invented phrases', afterNumber.ok === true
  && Array.isArray(afterNumber.result.phrases) && afterNumber.result.phrases.length === 0,
  JSON.stringify(afterNumber.result.phrases));

let failed = 0;
for (const c of cases) {
  if (!c.pass) failed++;
  console.log(`${c.pass ? 'ok  ' : 'FAIL'} ${c.name}${c.pass ? '' : '   <- ' + c.detail}`);
}
console.log(`\n${cases.length - failed}/${cases.length} parse cases pass`);
if (failed) process.exit(1);
