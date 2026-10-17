export type Lane = 'primary' | 'secondary';

export type Classification =
  | 'AI_GENERATED'
  | 'HUMAN_LIKELY'
  | 'REVIEW_REQUIRED'
  | 'DETECTION_UNAVAILABLE';

export interface LaneConfigEntry {
  base_url?: string;
  model?: string;
  /** Only meaningful on the secondary lane: false switches the tie-break off for
      this request, so a primary deferral becomes REVIEW instead of a second call. */
  enabled?: boolean;
}

export interface LaneConfig {
  primary?: LaneConfigEntry;
  secondary?: LaneConfigEntry;
}

export interface ByokKeys {
  primary?: string | null;
  secondary?: string | null;
}

export interface ModelSignal {
  name: string;
  direction: 'AI' | 'HUMAN';
  strength: number;
}

/** A verbatim passage the model quoted as evidence, with its own read on it.
    `quote` is copied from the analyzed text (the parser caps it at 200 chars and
    drops empties); `direction` is 'AI' or 'HUMAN'; `strength` is 0..1. Salvaged
    rows carry an empty list — a truncated answer never invents phrases. */
export interface ModelPhrase {
  quote: string;
  direction: 'AI' | 'HUMAN';
  strength: number;
}

export interface LaneVerdict {
  ai_probability: number;
  confidence: number;
  signals: ModelSignal[];
  /** Absent on older result shapes; the UI treats a missing list as "no phrases". */
  phrases?: ModelPhrase[];
}

export interface LaneAttempt {
  lane: string;
  model: string | null;
  ok: boolean;
  skipped: boolean;
  error_code: string | null;
  error_detail: string | null;
  salvaged: boolean;
  truncated: boolean;
  result: LaneVerdict | null;
}

export interface LaneConfigUsed {
  base_url: string;
  model: string;
  base_url_source: 'request' | 'env';
  model_source: 'request' | 'env';
  key_source: 'header' | 'env' | 'none';
  /** Echoed by the workflow; absent means enabled (older result shapes). */
  enabled?: boolean;
}

export interface SkippedEvidence {
  checked: boolean;
  reason: string;
}

export interface ComponentScores {
  primary_probability: number | null;
  primary_confidence: number | null;
  secondary_probability: number | null;
  secondary_confidence: number | null;
  lane_gap: number | null;
  secondary_called: boolean;
  decision_mode: string;
  deciding_lane: 'primary' | 'secondary' | null;
  secondary_decision_threshold: number | null;
}

export interface EffectiveThresholds {
  ai_prob: number;
  ai_conf: number;
  human_prob: number;
  human_conf: number;
  secondary_decision: number;
  single_model_penalty: number;
}

export interface EffectiveSampling {
  temperature: number;
  top_p: number;
  /** 'auto' means the request carried no max_tokens field at all: the gateway's own
      default applies and the answer can run as long as it needs to close its JSON. */
  max_tokens: number | 'auto';
}

/** One candidate the backend tried after the first model produced no verdict. `skipped`
    means it was never called — no time left in the request budget, or an endpoint outside
    `allowed_llm_hosts` — which is a different fact from "called and failed". */
export interface FailoverAttempt {
  from: string | null;
  to: string;
  provider: string;
  ok?: boolean;
  error_code?: string | null;
  skipped?: string;
}

export interface DetectionResult {
  document_id?: string;
  classification: Classification;
  ai_probability: number | null;
  confidence: number | null;
  method: string;
  review_required: boolean;
  config_used?: {
    byok_enabled: boolean;
    primary: LaneConfigUsed;
    secondary: LaneConfigUsed;
    /** The values in force after the backend's per-request tuning was merged. */
    thresholds?: EffectiveThresholds;
    sampling?: EffectiveSampling;
    /** Exactly which of those came from the SQLite model row rather than the
        defaults baked into the workflow. Empty means the baked ones won. */
    tuning_applied?: Record<string, number>;
  };
  evidence?: {
    provenance: SkippedEvidence;
    watermark: SkippedEvidence;
    statistics: SkippedEvidence;
    models: LaneAttempt[];
  };
  component_scores?: ComponentScores;
  /** Which authority chose the model that answered. The workflow's own `model_source` says
      'request' for all four cases, because from its side they are identical; only the backend
      can tell a deployed default from the caller's pool or from a failover they did not ask for. */
  lane_source?: 'failover' | 'request' | 'user-pool' | 'deployed-default';
  /** Present only when the first model returned no usable verdict and a later one did. The
      verdict on screen then belongs to `to`, not to the model the page would otherwise name. */
  auto_switch?: {
    from: string | null;
    to: string;
    reason: string;
    attempts: FailoverAttempt[];
  };
  /** Present when a failover was attempted and none of the candidates produced a verdict. */
  failover?: { attempts: FailoverAttempt[]; switched_to: null };
  warning?: string;
  warnings?: string[];
  versions?: {
    detector: string;
    classifier: string;
    thresholds: string;
    mode: string;
  };
  _internal?: {
    text_fingerprint: string;
    text_length: number;
    received_at: string;
    completed_at: string;
  };
  error?: string;
  error_code?: string;
  hint?: string;
}

/** A rejection from the workflow: HTTP 400 with the INPUT_REJECTED shape. */
export interface RejectionBody {
  classification?: Classification;
  error?: string;
  error_code?: string;
  hint?: string;
  method?: string;
  review_required?: boolean;
}
