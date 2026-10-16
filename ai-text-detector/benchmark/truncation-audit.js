// Does a max_tokens cap explain a run's failures, or is the model just not answering in JSON?
// The two look identical in a score table and need opposite fixes -- headroom vs a new prompt.
// A cap only bites rows that were cut off; if every cut-off row still parsed (because the JSON
// salvage repaired it) and every failure was a row that finished naturally, more tokens buys
// nothing. Usage: node truncation-audit.js results-<tag>.jsonl [more.jsonl ...]
const fs = require('node:fs');
const path = require('node:path');

const files = process.argv.slice(2);
if (!files.length) {
  console.error('usage: node truncation-audit.js results-<tag>.jsonl [more.jsonl ...]');
  process.exit(1);
}

// The results rows carry no request params, so the config comes from runs-manifest.log: quoting
// the cap that was actually sent is the only way this audit can say the cap was not the problem.
const manifest = {};
if (fs.existsSync(path.join(__dirname, 'runs-manifest.log'))) {
  for (const line of fs.readFileSync(path.join(__dirname, 'runs-manifest.log'), 'utf8').split(/\r?\n/)) {
    if (!line.startsWith('params: ')) continue;
    try {
      const p = JSON.parse(line.slice('params: '.length));
      if (p.tag) manifest[p.tag] = p;
    } catch { /* an unparseable line is not this tool's subject; skip it */ }
  }
}

const read = (f) => fs.readFileSync(path.join(__dirname, f), 'utf8')
  .split(/\r?\n/).filter(Boolean).map((l) => JSON.parse(l));

for (const f of files) {
  const rows = read(f);
  const count = (p) => rows.filter(p).length;
  const tag = f.replace(/^results-/, '').replace(/\.jsonl$/, '');
  const cfg = manifest[tag];
  console.log(`\n=== ${tag}`);
  console.log(cfg
    ? `    model=${cfg.model}  max_tokens=${cfg.max_tokens}  prompt=${cfg.prompt}  corpus=${cfg.corpus}`
    : '    no manifest row for this tag -- cannot state what parameters produced it');
  console.log(`    rows=${rows.length}  ok=${count((r) => r.ok)}  failed=${count((r) => !r.ok)}`);
  console.log(`    truncated=${count((r) => r.truncated)}`
    + `  (ok ${count((r) => r.ok && r.truncated)} / failed ${count((r) => !r.ok && r.truncated)})`);
  console.log(`    salvaged=${count((r) => r.salvaged)}`
    + `  (truncated ${count((r) => r.salvaged && r.truncated)}`
    + ` / full ${count((r) => r.salvaged && !r.truncated)})`);

  // The two numbers that decide the question, spelled out so the reader does not have to
  // re-derive the logic from the crosstab.
  const capKilled = count((r) => !r.ok && r.truncated);
  const capRepaired = count((r) => r.ok && r.truncated && r.salvaged);
  const formatKilled = count((r) => !r.ok && !r.truncated);
  console.log(`    -> failures the cap caused: ${capKilled}`);
  console.log(`    -> scores that only exist because the salvager patched a cut-off JSON: ${capRepaired}`);
  console.log(`    -> failures on answers that finished naturally: ${formatKilled}`);
  const codes = {};
  for (const r of rows) if (!r.ok) codes[r.error_code] = (codes[r.error_code] || 0) + 1;
  if (Object.keys(codes).length) console.log(`    -> error codes: ${JSON.stringify(codes)}`);
}
console.log('');
