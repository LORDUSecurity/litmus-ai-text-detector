// THROWAWAY probe (not a deliverable): how many real HC3 rows fall in the
// short/long length bands we want for the length sweep? Reads the same
// datasets-server endpoint build-corpus.js uses; spends no model budget.
const CONFIGS = ['open_qa', 'reddit_eli5', 'finance', 'medicine', 'wiki_csai'];

async function fetchRows(config, offset, length) {
  const url = 'https://datasets-server.huggingface.co/rows'
    + '?dataset=Hello-SimpleAI%2FHC3'
    + '&config=' + encodeURIComponent(config)
    + '&split=train&offset=' + offset + '&length=' + length;
  const res = await fetch(url);
  if (!res.ok) throw new Error('HF ' + res.status + ' ' + config);
  return (await res.json()).rows || [];
}

const BANDS = [
  ['short 60-299', 60, 299],
  ['long 1601-3000', 1601, 3000],
  ['long 3001-6000', 3001, 6000],
];

(async () => {
  const tally = {};
  for (const b of BANDS) tally[b[0]] = { HUMAN: 0, AI: 0 };
  for (const config of CONFIGS) {
    for (const off of [700, 900, 1100, 1300]) {
      let rows;
      try { rows = await fetchRows(config, off, 100); }
      catch (e) { console.log('skip', config, off, e.message); continue; }
      if (!rows.length) break;
      for (const w of rows) {
        const r = w.row || {};
        const hs = Array.isArray(r.human_answers) ? r.human_answers : [];
        const cs = Array.isArray(r.chatgpt_answers) ? r.chatgpt_answers : [];
        const ht = hs.find(t => typeof t === 'string');
        const ct = cs.find(t => typeof t === 'string');
        for (const b of BANDS) {
          if (ht && ht.length >= b[1] && ht.length <= b[2]) tally[b[0]].HUMAN++;
          if (ct && ct.length >= b[1] && ct.length <= b[2]) tally[b[0]].AI++;
        }
      }
      await new Promise(r => setTimeout(r, 400));
    }
    console.log('after', config, JSON.stringify(tally));
  }
})();
