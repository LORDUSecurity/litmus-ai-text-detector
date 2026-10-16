#!/usr/bin/env bash
# Bring up the stack that actually runs: n8n (AI-only workflow) -> Flask + SQLite -> Angular.
#
# Idempotent: anything already listening is left alone, anything missing is started in the
# background and its log is written under backend/data/.
#
#   ./quickstart.sh          start what is missing, then report
#   ./quickstart.sh --stop   stop the three processes this script started

set -euo pipefail
cd "$(dirname "$0")"

N8N_URL=http://127.0.0.1:5678
BACKEND_URL=http://127.0.0.1:8787
WEBHOOK="$N8N_URL/webhook/ai-detect-ai"
LOGS=backend/data
mkdir -p "$LOGS"

if [ "${1:-}" = "--stop" ]; then
  for port in 5678 8787 4200; do
    pid=$(node -e '
      const {execSync}=require("child_process");
      const port=process.argv[1];
      try{
        for(const line of execSync("netstat -ano -p tcp",{encoding:"utf8"}).split(/\r?\n/)){
          const f=line.trim().split(/\s+/);
          if(f[0]==="TCP"&&f[3]==="LISTENING"&&f[1].endsWith(":"+port)){console.log(f[4]);break;}
        }
      }catch(e){}
    ' "$port")
    if [ -n "${pid:-}" ]; then
      echo "port $port (pid $pid) -> taskkill"
      taskkill //PID "$pid" //F >/dev/null 2>&1 || true
    else
      echo "port $port: nothing listening"
    fi
  done
  exit 0
fi

[ -f .env ] || { echo "no .env — copy .env.example to .env and fill in the keys first."; exit 1; }
command -v node >/dev/null || { echo "node not on PATH"; exit 1; }
command -v python >/dev/null || { echo "python not on PATH (the backend is Flask + stdlib sqlite3)"; exit 1; }

listening() { curl -s -o /dev/null --max-time 2 "$1" 2>/dev/null; }

echo
echo "== n8n =="
# /healthz answers 200 roughly 40-55 s before the webhook registry exists, so it proves
# nothing here. Poll the webhook itself: a POST with an empty body must get the workflow's
# own 400 validation response, which only exists once the published version is registered.
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -X POST -H 'Content-Type: application/json' -d '{}' "$WEBHOOK" || echo 000)
# That probe is itself a webhook call, so it leaves one execution row behind (status
# crashed, empty body, no key) whose id sits below the backend's purge floor. Harmless,
# and it is the only way to prove the webhook is registered without trusting /healthz.
if [ "$code" = "400" ]; then
  echo "already running, webhook registered (HTTP 400 = the workflow's own input validation)"
else
  [ "$code" = "000" ] || echo "n8n answers HTTP $code on $WEBHOOK — up but this webhook is not registered."
  echo "starting n8n (log: $LOGS/n8n.log)..."
  nohup node tools/start-n8n.js start >"$LOGS/n8n.log" 2>&1 &
  echo -n "waiting for the webhook to register"
  for _ in $(seq 1 40); do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 -X POST -H 'Content-Type: application/json' -d '{}' "$WEBHOOK" || echo 000)
    [ "$code" = "400" ] && break
    echo -n .
    sleep 5
  done
  echo
  [ "$code" = "400" ] || {
    echo "webhook did not answer 400 (got $code). If n8n is up but the webhook is 404, deploy the workflow:"
    echo "  node tools/start-n8n.js import:workflow --input=n8n/ai-only-workflow.json"
    echo "  node tools/start-n8n.js publish:workflow --id=ai-detection-ai-only"
    echo "  then restart n8n — the registry is built at boot."
    tail -20 "$LOGS/n8n.log"
    exit 1
  }
fi

echo
echo "== SQLite =="
if [ ! -f "$LOGS/detector.sqlite3" ]; then
  echo "seeding providers, endpoints, keys, lane models + tuned params..."
  python -m backend.seed
else
  echo "$LOGS/detector.sqlite3 already present"
fi

echo
echo "== Flask backend =="
if listening "$BACKEND_URL/"; then
  echo "already running on $BACKEND_URL"
else
  echo "starting backend (log: $LOGS/backend.log)..."
  nohup python -m backend.app >"$LOGS/backend.log" 2>&1 &
  for _ in $(seq 1 20); do listening "$BACKEND_URL/" && break; sleep 1; done
fi
curl -s --max-time 10 "$BACKEND_URL/gateway-health" | python -m json.tool || {
  echo "backend not answering — see $LOGS/backend.log"; exit 1; }

echo
echo "== Angular =="
if ! listening http://localhost:4200/; then
  echo "starting frontend (log: $LOGS/ng-serve.log)..."
  (cd frontend && nohup npm start >"../$LOGS/ng-serve.log" 2>&1 &)
  echo -n "waiting for ng serve"
  for _ in $(seq 1 60); do listening http://localhost:4200/ && break; echo -n .; sleep 2; done
  echo
fi

echo
echo "=== up ==="
echo "UI        http://localhost:4200   (ng serve binds IPv6 — use localhost, not 127.0.0.1)"
echo "API       $BACKEND_URL"
echo "Workflow  $WEBHOOK"
echo "Stop it   ./quickstart.sh --stop"
