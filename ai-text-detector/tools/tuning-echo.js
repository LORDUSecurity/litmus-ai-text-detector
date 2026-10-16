/*
 * Loopback LLM echo used to prove that the tuned sampling parameters actually
 * reach the outbound chat-completion request.
 *
 * The workflow's host allowlist accepts loopback, so a detection request can be
 * pointed at this server instead of a real provider. It records the JSON body it
 * receives and replies with a valid OpenAI-style completion whose ai_probability
 * is fixed, which makes the resulting verdict deterministic.
 *
 *   node tools/tuning-echo.js 20128 0.62
 *   curl http://127.0.0.1:20128/last      -> the body of the last /chat/completions
 *   curl http://127.0.0.1:20128/count     -> how many calls it took
 *
 * Not part of the product: a verification instrument. It binds 127.0.0.1 only.
 */
const http = require('node:http');

const PORT = Number(process.argv[2] || 20128);
const PROBABILITY = Number(process.argv[3] || 0.62);

let last = null;
let count = 0;

const reply = () => ({
  id: 'chatcmpl-echo',
  object: 'chat.completion',
  model: 'echo/model',
  choices: [{
    index: 0,
    message: {
      role: 'assistant',
      content: JSON.stringify({
        ai_probability: PROBABILITY,
        confidence: 0.9,
        signals: [{ name: 'echo', direction: 'AI', strength: 0.5 }],
      }),
    },
    finish_reason: 'stop',
  }],
});

const server = http.createServer((req, res) => {
  const chunks = [];
  req.on('data', (c) => chunks.push(c));
  req.on('end', () => {
    const raw = Buffer.concat(chunks).toString('utf8');
    res.setHeader('Content-Type', 'application/json');
    if (req.method === 'GET' && req.url === '/last') {
      res.end(JSON.stringify(last, null, 2));
      return;
    }
    if (req.method === 'GET' && req.url === '/count') {
      res.end(String(count));
      return;
    }
    if (req.method === 'POST' && /chat\/completions$/.test(req.url)) {
      count += 1;
      try {
        last = { at: new Date().toISOString(), path: req.url, body: JSON.parse(raw) };
      } catch (e) {
        last = { at: new Date().toISOString(), path: req.url, unparsed: raw, error: String(e) };
      }
      res.end(JSON.stringify(reply()));
      return;
    }
    last = { at: new Date().toISOString(), path: req.url, method: req.method, unparsed: raw };
    res.statusCode = 404;
    res.end(JSON.stringify({ error: { message: 'echo: unmatched path ' + req.url } }));
  });
});

server.listen(PORT, '127.0.0.1', () => {
  console.log(`tuning echo listening on http://127.0.0.1:${PORT}/v1/chat/completions (pays ai_probability=${PROBABILITY})`);
});
