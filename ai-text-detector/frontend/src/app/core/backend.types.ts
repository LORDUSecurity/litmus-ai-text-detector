/** Shapes returned by the Flask backend (port 8787). Field names mirror the SQLite
    columns on purpose, so a DB change shows up here as a type error. */

export interface ApiKeyView {
  id: number;
  provider_id: number;
  label: string;
  /** Masked at the source: first 6 chars plus a length. The plaintext never leaves the DB. */
  key_hint: string;
  is_active: boolean;
  /** When this key last served a request, or null if it never has. Rotation across a
      provider's active keys is only visible if the panel can say which one was used. */
  last_used_at: string | null;
  created_at: string;
  updated_at: string;
  provider_name?: string;
}

export interface ModelView {
  id: number;
  provider_id: number;
  model_id: string;
  display_name: string;
  role: 'primary' | 'secondary' | 'either';
  lane: 'primary' | 'secondary' | null;
  temperature: number;
  top_p: number;
  max_tokens: number;
  timeout_ms: number;
  ai_prob_threshold: number | null;
  ai_conf_threshold: number | null;
  human_prob_threshold: number | null;
  human_conf_threshold: number | null;
  secondary_decision_threshold: number | null;
  notes: string;
  enabled: boolean;
  measured: boolean;
  provider_name?: string;
  provider_base_url?: string;
}

export interface ProviderView {
  id: number;
  name: string;
  label: string;
  base_url: string;
  auth_header: string;
  auth_prefix: string;
  free_tier: boolean;
  enabled: boolean;
  notes: string;
  key_count: number;
  model_count: number;
  keys: ApiKeyView[];
  models: ModelView[];
}

export interface BenchmarkModelRow {
  run_id: number;
  tag: string;
  model: string;
  provider: string;
  corpus: string;
  prompt: string;
  params: Record<string, unknown>;
  ai_cut: number;
  human_cut: number;
  n_rows: number;
  n_scored: number;
  // The two ways a text ends up unscored; they sum to n_rows - n_scored.
  n_no_answer: number;      // gateway/upstream failure -- the model never replied
  n_parse_failed: number;   // it replied, with nothing scorable in the text
  correct: number;
  wrong: number;
  deferred: number;
  accuracy_over_scored: number | null;
  // correct calls over every text in the run -- the denominator that makes a model which
  // answers nothing usable on a third of the corpus visible as such.
  accuracy_over_corpus: number | null;
  median_probability: number | null;
  median_ai_text: number | null;
  median_human_text: number | null;
  min_ai_text: number | null;
  max_human_text: number | null;
  separation: number | null;
}

export interface CatalogEntry {
  model: string;
  latency_ms: number | null;
  probed_at: string;
}

export type CatalogByProvider = Record<string, Record<string, CatalogEntry[]>>;

export interface DetectionRow {
  id: number;
  created_at: string;
  document_id: string | null;
  text_length: number | null;
  classification: string | null;
  ai_probability: number | null;
  confidence: number | null;
  method: string | null;
  review_required: number | null;
  deciding_lane: string | null;
  primary_model: string | null;
  secondary_model: string | null;
  latency_ms: number | null;
  http_status: number | null;
  error_code: string | null;
}

export interface N8nWorkflowRow {
  id: string;
  name: string;
  active: number;
  activeVersionId: string | null;
  updatedAt: string;
}

export interface N8nStatus {
  n8n_url: string;
  workflow_id: string;
  reachable: boolean;
  healthz?: number;
  healthz_error?: string;
  db_readable: boolean;
  db_error?: string;
  workflow?: N8nWorkflowRow | null;
  published?: boolean;
  webhook_registered?: boolean;
  execution_rows_present?: number;
}

export interface N8nExecutions {
  workflow_id: string;
  count: number;
  executions: Array<Record<string, unknown>>;
  note?: string;
}

export interface ProbeResult {
  ok: boolean;
  status: number | null;
  latency_ms: number;
  provider: string;
  base_url: string;
  model: string;
  reply_excerpt: string | null;
  error: string | null;
  error_code?: string;
  detail?: string;
  hint?: string;
}

export interface BackendHealth {
  ok: boolean;
  backend: string;
  purge_enabled: boolean;
  rows_purged: number;
  purge_errors?: number;
  startup_max_id: number;
  newest_id: number;
  quiet_sweeps: number;
  workflow_id: string;
  purge_error?: string | null;
  db_path: string;
  db_counts: Record<string, number>;
  n8n: N8nStatus;
  defaults: {
    primary_model: string | null;
    secondary_model: string | null;
    primary_provider: string | null;
    secondary_provider: string | null;
  };
  thresholds: {
    ai_probability: number | string | null;
    human_probability: number | string | null;
    secondary_decision: number | string | null;
  };
}

/** POST /extract: a document read into plain text, ready for the textarea. */
export interface ExtractResult {
  ok: boolean;
  text: string;
  chars: number;
  truncated: boolean;
  kind: string;
  filename: string;
}

// ---- accounts, BYOK, ranking, public config (the shared/hostable build) ----

export interface SessionUser {
  id: number;
  username: string;
  role: 'admin' | 'user';
  theme: 'dark' | 'light';
  created_at: string;
  last_login_at: string | null;
}

export interface AuthResponse {
  user: SessionUser | null;
}

/** A user's OWN key for an endpoint the admin allows. The plaintext is never returned —
    only a hint and whether it has been proven to work. */
export interface UserKeyView {
  id: number;
  provider_id: number;
  label: string;
  key_hint: string;
  verified: boolean;
  last_verified_at: string | null;
  created_at: string;
  provider_name?: string;
  provider_base_url?: string;
  /** Present on POST /my/keys when saving the key also queued a discovery run — the backend
      only enqueues once the key has actually verified, so this is never a promise it broke. */
  discovery?: DiscoveryEnqueue;
}

export interface KeyGuide {
  provider: string;
  title?: string;
  url: string;
  steps?: string;
}

/**
 * Admin-writable settings behind GET/POST `/settings` (a plain user gets 403 on both).
 * `n8n_url` and `webhook_path` are how the backend reaches the detection engine — the workflow
 * webhook — and are what a packaged local app has to change when n8n is not on its own machine.
 * Blank means "use the built-in default" (http://localhost:5678 + /webhook/ai-detect-ai).
 */
export interface AdminSettings {
  paper_url: string;
  paper_title: string;
  api_key_guides: KeyGuide[];
  allowed_llm_hosts: string;
  n8n_url: string;
  webhook_path: string;
  ai_prob_threshold: string;
  human_prob_threshold: string;
  secondary_decision_threshold: string;
}

export interface SettingsWriteResult {
  ok: boolean;
  written: Partial<AdminSettings>;
  /** Present only on a refused write: which field, and why. */
  field?: string;
  detail?: string;
}

export interface ConfigProviderModel {
  model_id: string;
  display_name: string | null;
  lane: 'primary' | 'secondary' | null;
}

export interface ConfigProvider {
  id: number;
  name: string;
  label: string;
  base_url: string;
  free_tier: boolean;
  host: string;
  has_admin_key: boolean;
  models: ConfigProviderModel[];
}

export interface PublicConfig {
  paper_url: string;
  paper_title: string;
  api_key_guides: KeyGuide[];
  providers: ConfigProvider[];
  user: SessionUser | null;
}

export interface RankedModel {
  rank: number;
  model: string;
  tag: string;
  corpus: string;
  provider: string;
  provider_id: number | null;
  provider_name?: string;
  base_url?: string;
  host?: string;
  free_tier?: boolean;
  n_rows: number;
  n_clean: number;
  correct: number;
  wrong: number;
  review: number;
  decided: number;
  accuracy: number | null;
  coverage: number | null;
  band: { ai: number; human: number; gate: number };
  lane: 'primary' | 'secondary' | null;
  /** Fewer texts than the ranking treats as a measurement — a sample, not a result. */
  provisional?: boolean;
  verified_key: boolean;
  admin_key: boolean;
  /** Whether this model is in the caller's using-pool, and at which of their ranks. */
  in_pool?: boolean;
  pool_position?: number | null;
  your_rank?: number | null;
  /** Availability, not accuracy: measured from real detections, 'unknown' until one runs. */
  health?: 'ok' | 'degraded' | 'down' | 'unknown';
  consecutive_failures?: number;
}

/** One row of the caller's using-pool, as `pool.pool_view` returns it. `your_rank` is where
    the user put it; `position` is where it is actually served from — the two differ exactly
    when a model stopped answering and was pushed to the back (`demoted`). */
export interface PoolEntry {
  provider: string;
  model: string;
  source: 'user' | 'auto';
  your_rank: number;
  position: number;
  health: 'ok' | 'degraded' | 'down' | 'unknown';
  consecutive_failures: number;
  last_error: string;
  last_ok_at: string | null;
  demoted: boolean;
}

export interface PoolResponse {
  pool: PoolEntry[];
  note?: string;
}

/** One auto-discovery run: list a provider's models, screen the untested ones on 2 texts. */
export interface DiscoveryJob {
  id: number;
  provider: string;
  status: 'queued' | 'running' | 'done' | 'failed';
  stage: string;
  n_listed: number;
  n_screened: number;
  n_usable: number;
  error: string;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
}

export interface DiscoveryStatus {
  jobs: DiscoveryJob[];
  screen_texts: string;
  note: string;
}

/** `queued: false` with `status: 'disabled'` means no worker will pick the job up — the
    backend refuses to write a row it cannot honour, so the page must not say "queued". */
export interface DiscoveryEnqueue {
  ok?: boolean;
  job_id: number | null;
  queued: boolean;
  status: string;
  reason?: string;
  error?: string;
  error_code?: string;
}

export interface RankingResponse {
  ranked: RankedModel[];
  pool?: PoolEntry[];
  paper_url: string;
  note: string;
}

