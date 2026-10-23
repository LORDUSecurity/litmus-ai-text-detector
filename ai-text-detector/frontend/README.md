# Detection UI

Angular page for the AI-only detection webhook: pick the provider, host, port and model per
lane, paste text, read the full verdict.

## Running it

The page talks to a local gateway, not to n8n directly. Start both, in this order:

```
node ../tools/detector-gateway.js     # :8787 — CORS proxy + execution-row purger
npm install
npm start                             # ng serve on :4200, proxies /api -> :8787
```

n8n must be running on :5678 with the workflow published, and its environment must have
`ALLOW_BYOK=true` for the key fields to do anything — that Node gateway forwards `/detect` to the
workflow, which is the hosted engine. `python -m backend.serve_local` is the other answer: it serves
this same built page and scores the text inside the Python process (`backend/engine.py`), so with it
no workflow server has to exist. The two engines are proved equivalent by `tools/engine_parity.py`.

The gateway is not optional. n8n writes the inbound webhook request — headers included — into
its execution database, so a key POSTed straight to n8n would sit on disk permanently. The
gateway deletes this workflow's execution rows a few seconds after each call. The config panel
shows whether the purge is live; if it says the gateway is unreachable, do not paste a key.

## Tests

```
npm test      # 28 unit tests, HTTP layer verified with HttpTestingController
npm run build
```

## Layout

- `src/app/core/` — types, the provider/model catalogue, the form state (signals), and
  `DetectionService`, the only place `HttpClient` appears.
- `src/app/ui/` — `config-panel`, `text-panel`, `result-card`, `raw-json`.
- `proxy.conf.json` — `/api` to `http://127.0.0.1:8787`.

BYOK keys are held only in component signals. They are never written to `localStorage`, never
placed in the request body, and never echoed back by the workflow.
