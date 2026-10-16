// Start n8n with the project's .env exported into its process environment.
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawn } = require('child_process');

const ROOT = path.resolve(__dirname, '..');
const RUNTIME = path.join(os.homedir(), 'n8n-runtime');
// An install outside the repo is normal (n8n is ~1 GB), so keep both locations
// configurable while defaulting to the ones this repo has always used.
const USER_FOLDER = process.env.N8N_USER_FOLDER || path.join(RUNTIME, '.n8n-data');
const N8N_BIN = process.env.N8N_BIN || path.join(RUNTIME, 'node_modules', 'n8n', 'bin', 'n8n');
const ENV_PATH = process.env.N8N_DOTENV_PATH || path.join(ROOT, '.env');
const env = { ...process.env };

for (const line of fs.readFileSync(ENV_PATH, 'utf8').split(/\r?\n/)) {
  if (line.trim().startsWith('#')) continue;
  const m = line.match(/^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/);
  if (m) env[m[1]] = m[2].replace(/^["']|["']$/g, '');
}

Object.assign(env, {
  N8N_USER_FOLDER: USER_FOLDER,
  N8N_DIAGNOSTICS_ENABLED: 'false',
  N8N_PERSONALIZATION_ENABLED: 'false',
  N8N_BLOCK_ENV_ACCESS_IN_NODE: 'false',   // required: Code nodes read $env
  N8N_PORT: '5678',
  N8N_HOST: '127.0.0.1',
  N8N_PROTOCOL: 'http',
  WEBHOOK_URL: 'http://localhost:5678/',
  GENERIC_TIMEZONE: 'UTC',
  EXECUTIONS_TIMEOUT: '300',
  DB_TYPE: 'sqlite',
});

const mask = (v) => (v ? v.slice(0, 7) + '...(len ' + v.length + ')' : '(unset)');
const bin = N8N_BIN;
// No args => start the server. Args => run that n8n CLI subcommand with the same env.
const cmd = process.argv.slice(2);
const argv = cmd.length ? cmd : ['start'];
if (!cmd.length) {
  console.log('PRIMARY_BASE_URL   =', env.PRIMARY_BASE_URL);
  console.log('PRIMARY_MODEL      =', env.PRIMARY_MODEL);
  console.log('PRIMARY_API_KEY    =', mask(env.PRIMARY_API_KEY));
  console.log('SECONDARY_MODEL    =', env.SECONDARY_MODEL);
  console.log('TIE_BREAK_CUT      =', env.SECONDARY_DECISION_THRESHOLD,
    '(the secondary answers only where the primary defers; deferral band =',
    env.HUMAN_PROBABILITY_THRESHOLD, '-', env.AI_PROBABILITY_THRESHOLD + ')');
  console.log('THRESHOLDS ai/human=', env.AI_PROBABILITY_THRESHOLD, '/', env.HUMAN_PROBABILITY_THRESHOLD,
    ' conf=', env.AI_CONFIDENCE_THRESHOLD, '/', env.HUMAN_CONFIDENCE_THRESHOLD,
    ' penalty=', env.SINGLE_MODEL_CONFIDENCE_PENALTY);
  console.log('BLOCK_ENV_ACCESS   =', env.N8N_BLOCK_ENV_ACCESS_IN_NODE);
  console.log('---');
}
const child = spawn(process.execPath, [bin, ...argv], { env, stdio: 'inherit' });
child.on('exit', (c) => process.exit(c || 0));
