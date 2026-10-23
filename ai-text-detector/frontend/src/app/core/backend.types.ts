/** Shapes returned by the Flask backend (port 8787). Field names mirror the SQLite
    columns on purpose, so a DB change shows up here as a type error. */
import type { DetectionResult } from './detection.types';

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

/** One stored run, as `/detections/<id>` answers: the list row plus the body that was
    recorded with it and — from schema v7 — the text that was analyzed.

    `payload` is partial because a failed run stores the *error* body there, not a verdict; a
    reader has to check `classification` before treating it as one. `text` is null on every row
    saved before the column existed, which the History sheet says out loud rather than leaving an
    empty panel where the analyzed text should be. */
export type DetectionDetail = DetectionRow & {
  payload: Partial<DetectionResult>;
  text: string | null;
};

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
  /** Absent when the answer says the workflow is not on the request path: a local install has no
      server to probe, so there is no reachability to report. Typed optional because reading an
      absent field as `false` is how a card ends up calling a working app unreachable. */
  reachable?: boolean;
  healthz?: number;
  healthz_error?: string;
  /** False on a packaged install, which scores text in its own process and never contacts the
      workflow. It is how a lane note can tell the admin whether a leg that does not answer will be
      replaced by the next model, or simply left unanswered. */
  on_request_path?: boolean;
  note?: string;
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
  /** The legacy handle. Sign-up is addressed by e-mail now, but this column still exists and
      still signs in, because every account created before then has only it. Never shown when the
      person gave us a name. */
  username: string;
  /** Null on an account that predates e-mail sign-up and has not been given one yet. */
  email: string | null;
  /** What the person typed at sign-up; falls back to the handle on a legacy account. */
  display_name: string;
  role: 'admin' | 'user';
  theme: 'dark' | 'light';
  created_at: string;
  last_login_at: string | null;
}

export interface AuthResponse {
  user: SessionUser | null;
  /** Which deployment this backend is. `local` means a single-owner packaged install with no
      sign-in page at all, so one built front-end can serve both; `accounts` is the hosted build.
      It rides on /auth/me because that is the only route read before anyone is signed in.
      Optional: a backend built before this change simply omits it, and the page then behaves as
      it always did -- show the gate. */
  auth_mode?: 'local' | 'accounts';
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
 * `n8n_url` and `webhook_path` name the workflow webhook a *hosted* deploy forwards `/detect` to;
 * a packaged single-owner install answers `/detect` in its own process and reads neither, which is
 * why `/n8n/status` says whether the workflow is on the request path before the UI offers to edit
 * it. Blank means "use the built-in default" (http://localhost:5678 + /webhook/ai-detect-ai).
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
  /** True for a gateway the caller runs himself (OmniRoute), which is the only case a key may
      carry its own endpoint: the seeded address belongs to whichever box has it. `POST /my/keys`
      refuses an override for anything else, so the form must not offer one. */
  self_hosted: boolean;
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

/** What the last discovery probe concluded about one model, in the engine's own words.
    `dead` is a refusal (410 delisted, 404 function id, `no_choices`); `slow` and `busy` are the
    pool being saturated, which does not retire the model -- it stays pickable and is asked again;
    `pending` answered only after a retry and is waiting for the user's accept-or-refuse;
    `declined` is the user's own `no`. `unknown` means the id was never probed. */
export type ProbeStatus = 'usable' | 'pending' | 'declined' | 'slow' | 'busy' | 'dead' | 'unknown';

export interface PoolOption {
  provider: string;
  provider_id: number;
  provider_name: string;
  base_url: string;
  free_tier: boolean;
  host: string;
  model: string;
  measured: boolean;
  timeout_ms: number | null;
  notes: string;
  probe_status: ProbeStatus;
  probe_note: string;
  answer_ms: number | null;
  verified_key: boolean;
  admin_key: boolean;
  needs_key: boolean;
  in_pool: boolean;
  your_rank: number | null;
  health: PoolEntry['health'];
  offered: boolean;
  offer_seconds_left: number | null;
}

export interface PoolOptionsResponse {
  options: PoolOption[];
  window_s: number;
  fast_ms: number;
  note: string;
}

/** The body `/my/catalog/decision` answers with: the row as it now stands, plus the pool the
    decision may have appended to. */
export interface CatalogDecision {
  provider: string;
  model: string;
  decision: 'accept' | 'refuse';
  probe_status: ProbeStatus;
  pool: PoolEntry[];
}

/** What `POST /my/pool/auto-rank` answers with. `ranked` is how many of his models a capture could
    actually place; the rest are `unranked` and stay at the back in the order he gave them, because
    a missing measurement is not a verdict about a model. */
export interface AutoRankResponse {
  pool: PoolEntry[];
  ranked: number;
  unranked: number;
  note?: string;
}

/** What `POST /my/probe` answers with: one 8-token round trip on the caller's own credential.
    It is the user's version of the admin's `ProbeResult` and carries less — no base_url, no reply
    excerpt — and it writes nothing: no health row, no catalogue status, no discovery job. */
export interface MyProbeResult {
  ok: boolean;
  status?: number | null;
  latency_ms?: number;
  provider?: string;
  model?: string;
  error_code?: string;
  error?: string;
  detail?: string;
}

/** Which lane actually answers, reported by `/ranking` from the same functions `/detect` walks
    (`pool_primary_lane`, `default_models`, `pool.next_candidates`) — never re-derived here.
    The ranking says which model judges best; this says which one the user's runs are scored with
    and what a failover would switch to. `next.source` distinguishes "your pool's first entry"
    from "the admin's deployed default", and `failover` omits any model marked down, because the
    engine refuses to switch to one that has already failed three times in a row. */
export interface LaneRef {
  model: string;
  provider: string;
}

export interface LaneAuthority {
  deployed_default: LaneRef | null;
  deployed_tie_break: LaneRef | null;
  next: (LaneRef & { source: 'user-pool' | 'deployed-default' }) | null;
  failover: LaneRef[];
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
  /** Absent on an older backend: the page then says nothing about who answers rather than
      guessing it client-side from the pool it happens to have loaded. */
  lane_authority?: LaneAuthority;
  note: string;
}

