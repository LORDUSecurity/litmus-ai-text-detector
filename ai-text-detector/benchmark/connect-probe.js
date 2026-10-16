/*
 * Connectivity triage: is the provider reachable from this host right now, and if not,
 * which stage of the path breaks (DNS, TCP, TLS, HTTP)? Used before spending 48+ requests
 * on a benchmark run. Scratch tool: run it, read it, delete it.
 */
const fs = require('fs');
const net = require('net');
const tls = require('tls');
const dns = require('dns');
const path = require('path');

const env = {};
for (const l of fs.readFileSync(path.join(__dirname, '..', '.env'), 'utf8').split(/\r?\n/)) {
  const m = l.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/);
  if (m) env[m[1]] = m[2];
}

const HOSTS = ['integrate.api.nvidia.com', 'www.gutenberg.org', 'openrouter.ai'];
const PROMPT = [
  'Text to classify:',
  '',
  'It was a bright cold day in April, and the clocks were striking thirteen.',
  '',
  'Reply with JSON only: {"ai_probability": 0.00}',
].join('\n');

function tcp(host, ms) {
  return new Promise((res) => {
    const t0 = Date.now();
    const s = net.connect({ host, port: 443, timeout: ms });
    s.on('connect', () => { s.destroy(); res(`tcp ok ${Date.now() - t0}ms`); });
    s.on('timeout', () => { s.destroy(); res(`tcp TIMEOUT ${Date.now() - t0}ms`); });
    s.on('error', (e) => res(`tcp ERR ${e.code} ${Date.now() - t0}ms`));
  });
}

function tlsCheck(host, ms) {
  return new Promise((res) => {
    const t0 = Date.now();
    const s = tls.connect({ host, port: 443, servername: host, timeout: ms });
    s.on('secureConnect', () => { s.destroy(); res(`tls ok ${Date.now() - t0}ms`); });
    s.on('timeout', () => { s.destroy(); res(`tls TIMEOUT ${Date.now() - t0}ms`); });
    s.on('error', (e) => res(`tls ERR ${e.code || e.message} ${Date.now() - t0}ms`));
  });
}

function lookup(host) {
  return new Promise((res) => dns.lookup(host, { family: 4, all: true }, (e, a) => {
    res(e ? `lookup ERR ${e.code}` : 'lookup ' + a.map((x) => x.address).join(','));
  }));
}

async function call(model, maxTokens) {
  const t0 = Date.now();
  try {
    const r = await fetch(env.PRIMARY_BASE_URL + '/chat/completions', {
      method: 'POST',
      headers: { Authorization: 'Bearer ' + env.PRIMARY_API_KEY, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model, temperature: 0.1, max_tokens: maxTokens,
        messages: [{ role: 'user', content: PROMPT }],
      }),
    });
    const txt = await r.text();
    let score = '';
    try { score = JSON.stringify(JSON.parse(txt).choices[0].message.content).slice(0, 90); } catch (e) { score = txt.slice(0, 90); }
    return `${model} -> HTTP ${r.status} ${Date.now() - t0}ms  ${score.replace(/\s+/g, ' ')}`;
  } catch (e) {
    return `${model} -> ERR ${e.cause ? e.cause.code || e.cause.message : e.message} ${Date.now() - t0}ms`;
  }
}

(async () => {
  for (const h of HOSTS) {
    console.log(`\n${h}`);
    console.log('  ' + await lookup(h));
    console.log('  ' + await tcp(h, 15000));
    console.log('  ' + await tlsCheck(h, 15000));
  }
  console.log('\nreal lane calls:');
  console.log('  ' + await call('google/diffusiongemma-26b-a4b-it', 60));
  console.log('  ' + await call('nvidia/nemotron-3-ultra-550b-a55b', 400));
})();
