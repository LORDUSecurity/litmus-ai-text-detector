/** How long a request that spends its time inside a model may be waited on.
    Run from ai-text-detector/:
        npx ng test --watch=false --include src/app/core/backend.service.spec.ts

    Two numbers get proved here, and they are the two halves of the same decision. A key probe and
    a save-with-verify are real calls to a real provider, so the client has to be as patient as the
    gateway underneath it (./http-budget.ts); on 2026-10-17 the packaged verifier measured one such
    probe answering successfully in 81 812 ms, which the old 60 s ceiling aborted and reported as a
    broken key. A control-plane read is the opposite kind of request -- it never waits on a model --
    so it keeps its short ceiling, and that is checked too, because a fix that turns every timeout
    into six minutes is its own bug.

    vi's fake timers, not Angular's `fakeAsync`: this app is zoneless and `fakeAsync` needs the
    zone-testing bundle the build does not carry (it fails the whole file with "zone-testing.js is
    needed for the fakeAsync() test helper").
 */
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { BackendService } from './backend.service';
import { HTTP_PATIENCE_MS } from './http-budget';

/** tools/verify_pool_failover.py, 2026-10-17: `nvidia/z-ai/glm-5.3-flash latency=81812ms`, ok. */
const MEASURED_SLOW_PROBE_MS = 81_812;

describe('BackendService request patience', () => {
  let service: BackendService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    service = TestBed.inject(BackendService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it('waits out a probe that is slow because the model is slow, not because it is broken', () => {
    vi.useFakeTimers();
    try {
      let failed = false;
      service.verifyMyKey(7, 'z-ai/glm-5.3-flash')
        .subscribe({ error: () => { failed = true; } });
      http.expectOne('/api/my/keys/7/verify');

      vi.advanceTimersByTime(MEASURED_SLOW_PROBE_MS);
      expect(failed).toBe(false);

      // Up to the ceiling the answer is still allowed to arrive.
      vi.advanceTimersByTime(HTTP_PATIENCE_MS - MEASURED_SLOW_PROBE_MS - 1);
      expect(failed).toBe(false);

      // And the ceiling is a real number, not "forever".
      vi.advanceTimersByTime(2);
      expect(failed).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  it('keeps the short ceiling on a control-plane read that never waits on a model', () => {
    vi.useFakeTimers();
    try {
      let failed = false;
      service.myKeys().subscribe({ error: () => { failed = true; } });
      http.expectOne('/api/my/keys');

      vi.advanceTimersByTime(7_000);
      expect(failed).toBe(false);
      vi.advanceTimersByTime(1_001);
      expect(failed).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });
});
