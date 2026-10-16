import { HttpClient } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { Observable, timeout } from 'rxjs';
import type {
  AdminSettings,
  ApiKeyView,
  AuthResponse,
  BackendHealth,
  BenchmarkModelRow,
  CatalogByProvider,
  DetectionRow,
  ExtractResult,
  ModelView,
  N8nExecutions,
  N8nStatus,
  ProbeResult,
  ProviderView,
  PublicConfig,
  RankingResponse,
  SettingsWriteResult,
  UserKeyView,
} from './backend.types';
import type { DetectionResult } from './detection.types';

/** The dev proxy maps /api → the Flask backend on 127.0.0.1:8787 and strips the
    prefix, so every path here is written without /api. */
const API = '/api';

@Injectable({ providedIn: 'root' })
export class BackendService {
  private readonly http = inject(HttpClient);

  health(): Observable<BackendHealth> {
    return this.http.get<BackendHealth>(`${API}/gateway-health`).pipe(timeout(8_000));
  }

  // ---- providers + endpoints -------------------------------------------

  providers(): Observable<ProviderView[]> {
    return this.http.get<ProviderView[]>(`${API}/providers`).pipe(timeout(8_000));
  }

  createProvider(body: {
    name: string;
    base_url: string;
    label?: string;
    free_tier?: boolean;
    notes?: string;
  }): Observable<ProviderView> {
    return this.http.post<ProviderView>(`${API}/providers`, body);
  }

  updateProvider(id: number, body: Partial<ProviderView>): Observable<ProviderView> {
    return this.http.patch<ProviderView>(`${API}/providers/${id}`, body);
  }

  deleteProvider(id: number): Observable<{ deleted: number }> {
    return this.http.delete<{ deleted: number }>(`${API}/providers/${id}`);
  }

  /** One real request to the provider. This is the only endpoint that touches the
      network on the user's behalf, so it gets the long timeout. */
  probe(providerId: number, modelId: string): Observable<ProbeResult> {
    return this.http
      .post<ProbeResult>(`${API}/providers/${providerId}/probe`, { model_id: modelId })
      .pipe(timeout(60_000));
  }

  /** Read a .txt/.md/.docx/.pdf into plain text. Parsing happens on this machine
      only — the file never leaves localhost and nothing is stored server-side. */
  extractFile(file: File): Observable<ExtractResult> {
    const form = new FormData();
    form.append('file', file, file.name);
    return this.http.post<ExtractResult>(`${API}/extract`, form).pipe(timeout(30_000));
  }

  // ---- api keys (write-only: the backend returns hints, never values) ---

  keys(): Observable<ApiKeyView[]> {
    return this.http.get<ApiKeyView[]>(`${API}/keys`).pipe(timeout(8_000));
  }

  addKey(providerId: number, apiKey: string, label: string): Observable<ApiKeyView> {
    return this.http.post<ApiKeyView>(`${API}/providers/${providerId}/keys`, {
      api_key: apiKey,
      label,
    });
  }

  setKeyActive(keyId: number, isActive: boolean): Observable<ApiKeyView> {
    return this.http.patch<ApiKeyView>(`${API}/keys/${keyId}`, { is_active: isActive });
  }

  deleteKey(keyId: number): Observable<{ deleted: number }> {
    return this.http.delete<{ deleted: number }>(`${API}/keys/${keyId}`);
  }

  // ---- models + tuned parameters ---------------------------------------

  models(): Observable<ModelView[]> {
    return this.http.get<ModelView[]>(`${API}/models`).pipe(timeout(8_000));
  }

  createModel(body: Partial<ModelView> & { provider_id: number; model_id: string }): Observable<ModelView> {
    return this.http.post<ModelView>(`${API}/models`, body);
  }

  updateModel(id: number, body: Partial<ModelView>): Observable<ModelView> {
    return this.http.patch<ModelView>(`${API}/models/${id}`, body);
  }

  deleteModel(id: number): Observable<{ deleted: number }> {
    return this.http.delete<{ deleted: number }>(`${API}/models/${id}`);
  }

  assignLane(id: number, lane: 'primary' | 'secondary'): Observable<{ primary: string | null; secondary: string | null }> {
    return this.http.post<{ primary: string | null; secondary: string | null }>(
      `${API}/models/${id}/assign-lane`,
      { lane },
    );
  }

  // ---- history + measured benchmark data --------------------------------

  detections(limit = 25): Observable<DetectionRow[]> {
    return this.http.get<DetectionRow[]>(`${API}/detections`, { params: { limit } }).pipe(timeout(8_000));
  }

  /** One stored run: the row plus `payload`, the full DetectionResult as it came back
      from the workflow. Scoped to the caller on the server, so someone else's id 404s. */
  detection(id: number): Observable<DetectionRow & { payload: DetectionResult }> {
    return this.http
      .get<DetectionRow & { payload: DetectionResult }>(`${API}/detections/${id}`)
      .pipe(timeout(8_000));
  }

  benchmarkModels(): Observable<BenchmarkModelRow[]> {
    return this.http.get<BenchmarkModelRow[]>(`${API}/benchmark/models`).pipe(timeout(15_000));
  }

  benchmarkCatalog(): Observable<CatalogByProvider> {
    return this.http.get<CatalogByProvider>(`${API}/benchmark/catalog`).pipe(timeout(15_000));
  }

  importBenchmarks(): Observable<{ benchmark_runs: string[]; catalog: string[] }> {
    return this.http
      .post<{ benchmark_runs: string[]; catalog: string[] }>(`${API}/benchmark/import`, {})
      .pipe(timeout(60_000));
  }

  // ---- admin settings: engine target, paper link, key guides --------------
  // Admin-only on the server (GET and POST both 403 for a plain user). These are the
  // "advanced parameters" the shared page must not expose to users, and the only way a
  // packaged local app can be pointed at a detection engine that is not on its own machine.

  settings(): Observable<AdminSettings> {
    return this.http.get<AdminSettings>(`${API}/settings`).pipe(timeout(8_000));
  }

  saveSettings(body: Partial<AdminSettings>): Observable<SettingsWriteResult> {
    return this.http.post<SettingsWriteResult>(`${API}/settings`, body);
  }

  n8nStatus(): Observable<N8nStatus> {
    return this.http.get<N8nStatus>(`${API}/n8n/status`).pipe(timeout(10_000));
  }

  n8nExecutions(limit = 10): Observable<N8nExecutions> {
    return this.http
      .get<N8nExecutions>(`${API}/n8n/executions`, { params: { limit } })
      .pipe(timeout(10_000));
  }

  // ---- accounts + session ------------------------------------------------

  me(): Observable<AuthResponse> {
    return this.http.get<AuthResponse>(`${API}/auth/me`).pipe(timeout(8_000));
  }

  login(username: string, password: string): Observable<AuthResponse> {
    return this.http.post<AuthResponse>(`${API}/auth/login`, { username, password });
  }

  register(username: string, password: string): Observable<AuthResponse> {
    return this.http.post<AuthResponse>(`${API}/auth/register`, { username, password });
  }

  logout(): Observable<{ ok: boolean }> {
    return this.http.post<{ ok: boolean }>(`${API}/auth/logout`, {});
  }

  changePassword(current: string, next: string): Observable<{ ok: boolean }> {
    return this.http.post<{ ok: boolean }>(`${API}/auth/password`, { current, next });
  }

  setTheme(theme: 'dark' | 'light'): Observable<AuthResponse> {
    return this.http.post<AuthResponse>(`${API}/auth/theme`, { theme });
  }

  // ---- bring-your-own-key (the caller's own credentials) -----------------

  myKeys(): Observable<UserKeyView[]> {
    return this.http.get<UserKeyView[]>(`${API}/my/keys`).pipe(timeout(8_000));
  }

  addMyKey(body: {
    provider_id?: number;
    base_url?: string;
    api_key: string;
    label?: string;
    model_id?: string;
  }): Observable<UserKeyView> {
    // Verifies on save when a model is supplied, so "auto-pick a working key" is honest.
    return this.http.post<UserKeyView>(`${API}/my/keys`, body).pipe(timeout(60_000));
  }

  deleteMyKey(id: number): Observable<{ deleted: number }> {
    return this.http.delete<{ deleted: number }>(`${API}/my/keys/${id}`);
  }

  verifyMyKey(id: number, modelId: string): Observable<{ ok: boolean; status?: number }> {
    return this.http
      .post<{ ok: boolean; status?: number }>(`${API}/my/keys/${id}/verify`, { model_id: modelId })
      .pipe(timeout(60_000));
  }

  // ---- public config + ranking (the page that replaced /research) --------

  config(): Observable<PublicConfig> {
    return this.http.get<PublicConfig>(`${API}/config`).pipe(timeout(8_000));
  }

  ranking(): Observable<RankingResponse> {
    return this.http.get<RankingResponse>(`${API}/ranking`).pipe(timeout(15_000));
  }
}
