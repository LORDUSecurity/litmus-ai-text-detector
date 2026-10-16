import { HttpClient, HttpHeaders } from '@angular/common/http';
import { Injectable, inject, signal } from '@angular/core';
import { Observable, map, timeout } from 'rxjs';
import type { ByokKeys, DetectionResult, LaneConfig } from './detection.types';
import type { BackendHealth } from './backend.types';

/** The workflow's own ceiling is 50 000 chars; keep the client in step so a
    too-long paste is an inline field error rather than a 400 round-trip. */
export const MAX_TEXT_LENGTH = 50000;
export const MIN_TEXT_LENGTH = 10;

@Injectable({ providedIn: 'root' })
export class DetectionService {
  private readonly http = inject(HttpClient);

  /** Everything goes through the local gateway, which both adds CORS and deletes
      the n8n execution row that would otherwise keep a BYOK key on disk. */
  private readonly endpoint = '/api/detect';

  /** Long because the 550B lane took 80s on a real benchmark text. */
  private readonly requestTimeoutMs = 150_000;

  /** A model the signed-in user chose on the Ranking tab, or null for the admin's deployed lane.
      This is the "pick them auto if a verified key is there" step: the ranking already says which
      models the user can pay for, and choosing one there has to survive the jump to Detect.
      Only the model id travels — the backend fills the endpoint and that model's own calibrated
      band from its stored row and injects the user's stored key, so no advanced parameter and no
      credential is ever handled here. */
  readonly pickedModel = signal<string | null>(null);

  usePickedModel(model: string): void {
    this.pickedModel.set(model.trim() || null);
  }

  clearPickedModel(): void {
    this.pickedModel.set(null);
  }

  buildLaneConfig(laneConfig: LaneConfig): LaneConfig | undefined {
    const out: LaneConfig = {};
    (['primary', 'secondary'] as const).forEach((lane) => {
      const entry = laneConfig[lane];
      if (!entry) return;
      const baseUrl = (entry.base_url ?? '').trim();
      const model = (entry.model ?? '').trim();
      const clean: { base_url?: string; model?: string; enabled?: boolean } = {};
      if (baseUrl) clean.base_url = baseUrl;
      if (model) clean.model = model;
      // An explicit false survives even with no other fields — that entry IS the
      // "switch the tie-break off for this run" message to the backend + workflow.
      if (lane === 'secondary' && entry.enabled === false) clean.enabled = false;
      if (Object.keys(clean).length) out[lane] = clean;
    });
    return Object.keys(out).length ? out : undefined;
  }

  detect(text: string, laneConfig: LaneConfig, keys: ByokKeys): Observable<DetectionResult> {
    const body: { text: string; lane_config?: LaneConfig } = { text };
    const lane = this.buildLaneConfig(laneConfig);
    if (lane) body.lane_config = lane;

    // A model picked on the Ranking overrides the panel's primary for this run: it is a deliberate
    // action taken one tab ago, and the chip on Detect says so out loud.
    const pick = this.pickedModel();
    if (pick) {
      body.lane_config = { ...(body.lane_config ?? {}), primary: { model: pick } };
    }

    let headers = new HttpHeaders({ 'Content-Type': 'application/json' });
    const primary = (keys.primary ?? '').trim();
    const secondary = (keys.secondary ?? '').trim();
    if (primary) headers = headers.set('x-primary-api-key', primary);
    if (secondary) headers = headers.set('x-secondary-api-key', secondary);

    return this.http
      .post<DetectionResult>(this.endpoint, body, { headers, responseType: 'json' })
      .pipe(timeout(this.requestTimeoutMs));
  }

  health(): Observable<BackendHealth> {
    return this.http.get<BackendHealth>('/api/gateway-health').pipe(timeout(5_000));
  }
}
