import { Component, OnInit, computed, inject, signal } from '@angular/core';
import { BackendService } from '../../core/backend.service';
import type {
  BenchmarkModelRow,
  CatalogByProvider,
  DetectionRow,
  N8nExecutions,
  N8nStatus,
} from '../../core/backend.types';

interface CatalogFlat {
  provider: string;
  bucket: string;
  model: string;
  latency_ms: number | null;
  probed_at: string;
}

@Component({
  selector: 'app-benchmark-panel',
  templateUrl: './benchmark-panel.html',
})
export class BenchmarkPanel implements OnInit {
  private readonly backend = inject(BackendService);

  readonly rows = signal<BenchmarkModelRow[]>([]);
  readonly catalog = signal<CatalogByProvider>({});
  readonly detections = signal<DetectionRow[]>([]);
  readonly n8n = signal<N8nStatus | null>(null);
  readonly executions = signal<N8nExecutions | null>(null);
  readonly error = signal<string | null>(null);
  readonly note = signal<string | null>(null);
  readonly busy = signal(false);

  /** Sorted so the best measured separation over scored texts is on top — that is
      the number the free-model choice was actually made on. */
  readonly ranked = computed(() =>
    [...this.rows()].sort(
      (a, b) => (b.separation ?? -1) - (a.separation ?? -1) || (b.accuracy_over_scored ?? -1) - (a.accuracy_over_scored ?? -1),
    ),
  );

  readonly catalogFlat = computed<CatalogFlat[]>(() => {
    const out: CatalogFlat[] = [];
    const byProvider = this.catalog();
    Object.keys(byProvider).forEach((provider) => {
      const buckets = byProvider[provider];
      Object.keys(buckets).forEach((bucket) => {
        buckets[bucket].forEach((e) =>
          out.push({ provider, bucket, model: e.model, latency_ms: e.latency_ms, probed_at: e.probed_at }),
        );
      });
    });
    return out.sort((a, b) => a.provider.localeCompare(b.provider) || a.model.localeCompare(b.model));
  });

  readonly catalogCounts = computed(() => {
    const n = this.catalogFlat();
    return {
      total: n.length,
      // The bucket is the probe's own verdict. Latency is not: a 404 or a 429 has
      // a latency too, so counting non-null latencies would call a refusal an answer.
      reachable: n.filter((e) => e.bucket === 'answered').length,
      providers: new Set(n.map((e) => e.provider)).size,
    };
  });

  ngOnInit(): void {
    this.reload();
  }

  reload(): void {
    this.busy.set(true);
    this.error.set(null);
    let pending = 4;
    const done = () => {
      pending -= 1;
      if (pending <= 0) this.busy.set(false);
    };
    const fail = (what: string) => (err: unknown) => {
      done();
      this.error.set(`${what}: ${String(err)}`);
    };

    this.backend.benchmarkModels().subscribe({
      next: (r) => {
        this.rows.set(r);
        done();
      },
      error: fail('benchmark runs'),
    });
    this.backend.benchmarkCatalog().subscribe({
      next: (c) => {
        this.catalog.set(c);
        done();
      },
      error: fail('catalog probes'),
    });
    this.backend.detections(25).subscribe({
      next: (d) => {
        this.detections.set(d);
        done();
      },
      error: fail('detection history'),
    });
    this.backend.n8nStatus().subscribe({
      next: (s) => {
        this.n8n.set(s);
        this.backend.n8nExecutions(10).subscribe({
          next: (e) => {
            this.executions.set(e);
            done();
          },
          error: fail('n8n executions'),
        });
      },
      error: fail('n8n status'),
    });
  }

  /** Re-reads the on-disk benchmark logs into SQLite. Idempotent: runs are keyed by
      tag, so importing twice does not duplicate a measurement. */
  reimport(): void {
    this.busy.set(true);
    this.note.set(null);
    this.backend.importBenchmarks().subscribe({
      next: (r) => {
        this.error.set(null);
        this.note.set(
          `Imported ${r.benchmark_runs.length} run(s) and ${r.catalog.length} catalog group(s) from disk.`,
        );
        this.reload();
      },
      error: (err) => {
        this.busy.set(false);
        this.error.set(`Import failed: ${String(err)}`);
      },
    });
  }

  pct(v: number | null): string {
    return v === null ? '—' : `${(v * 100).toFixed(1)}%`;
  }

  num(v: number | null, digits = 3): string {
    return v === null ? '—' : v.toFixed(digits);
  }

  stamp(iso: string | null): string {
    if (!iso) return '—';
    const d = new Date(iso.includes('T') ? iso : `${iso.replace(' ', 'T')}Z`);
    return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
  }

  params(p: Record<string, unknown>): string {
    // A run recorded before the harness wrote its params line has keys with null values.
    // Printing `base_url=null` reads as "this ran against no URL" rather than "unknown",
    // so unrecorded values are dropped and the provider/corpus columns carry the rest.
    const parts = Object.keys(p ?? {})
      .filter((k) => p[k] !== null && p[k] !== undefined)
      .map((k) => `${k}=${String(p[k])}`);
    return parts.length ? parts.join(' · ') : '—';
  }
}
