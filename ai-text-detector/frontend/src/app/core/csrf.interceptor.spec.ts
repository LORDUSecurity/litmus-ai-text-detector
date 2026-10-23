/** The page half of the CSRF contract.
    Run from ai-text-detector/frontend/:
        npx ng test --watch=false --include src/app/core/csrf.interceptor.spec.ts

    The backend refuses a signed-in write that cannot name the session (`backend/security.py`), and
    the only thing that makes the app work anyway is this interceptor copying the readable
    `detector_csrf` cookie onto the `X-CSRF-Token` header. Two ways this passes while the product is
    broken: the interceptor exists but is not in `appConfig` (so nothing but the unit tests run it),
    and it is in `appConfig` but reads a token cached at boot, which 403s every write after the owner
    signs out and signs back in. Both are asserted here, through the shipped provider list.
 */
import { HttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { appConfig } from '../app.config';
import { CSRF_COOKIE, CSRF_HEADER, csrfInterceptor, csrfToken } from './csrf.interceptor';

const TOKEN = 'aBc-123_xYz';

function setCookie(value: string): void {
  document.cookie = `${CSRF_COOKIE}=${value}; path=/`;
}

function clearCookie(): void {
  document.cookie = `${CSRF_COOKIE}=; path=/; expires=Thu, 01 Jan 1970 00:00:00 GMT`;
}

describe('CSRF interceptor', () => {
  let http: HttpTestingController;

  beforeEach(() => {
    // The app's own provider list, so "the interceptor is wired up" is proved rather than assumed:
    // a request fired here goes through exactly what a request fired by Detect goes through.
    TestBed.configureTestingModule({
      providers: [...appConfig.providers, provideHttpClientTesting()],
    });
    http = TestBed.inject(HttpTestingController);
    clearCookie();
  });

  afterEach(() => {
    http.verify();
    clearCookie();
  });

  it('puts the session token on a write', () => {
    setCookie(TOKEN);
    TestBed.inject(HttpClient).post('/my/pool', { provider: 'nvidia' }).subscribe();
    const req = http.expectOne('/my/pool');
    expect(req.request.headers.get(CSRF_HEADER)).toBe(TOKEN);
    req.flush({ ok: true });
  });

  it('leaves a read alone -- a read is how the token arrives', () => {
    setCookie(TOKEN);
    TestBed.inject(HttpClient).get('/auth/me').subscribe();
    const req = http.expectOne('/auth/me');
    expect(req.request.headers.has(CSRF_HEADER)).toBe(false);
    req.flush({ ok: true });
  });

  it('sends nothing when no session has been issued a token', () => {
    // The sign-in and sign-up calls themselves, plus every anonymous probe. An empty header would be
    // a wrong answer; the guard only asks a request that already carries a signed-in session.
    TestBed.inject(HttpClient).post('/auth/login', { identifier: 'a@b.co' }).subscribe();
    const req = http.expectOne('/auth/login');
    expect(req.request.headers.has(CSRF_HEADER)).toBe(false);
    req.flush({ ok: true });
  });

  it('re-reads the cookie per request, because a re-login replaces the token', () => {
    setCookie('first-session-token');
    const client = TestBed.inject(HttpClient);
    client.post('/auth/theme', { theme: 'light' }).subscribe();
    const first = http.expectOne('/auth/theme');
    expect(first.request.headers.get(CSRF_HEADER)).toBe('first-session-token');
    first.flush({ ok: true });
    // Sign out deletes it, sign in mints a different one; a token cached at boot would keep sending
    // the first value and every later write would 403 with "sign in again".
    clearCookie();
    setCookie('second-session-token');
    client.post('/auth/theme', { theme: 'dark' }).subscribe();
    const second = http.expectOne('/auth/theme');
    expect(second.request.headers.get(CSRF_HEADER)).toBe('second-session-token');
    second.flush({ ok: true });
  });

  it('survives a cookie whose value is percent-encoded', () => {
    // `secrets.token_urlsafe` only emits - and _ beside alphanumerics, but the cookie reader is
    // shared with anything else the deploy ever sets, and a decode failure would throw inside every
    // write rather than the one request that happens to look odd.
    setCookie('tok%2Den');
    TestBed.inject(HttpClient).post('/detect', { text: 'x'.repeat(20) }).subscribe();
    const req = http.expectOne('/detect');
    expect(req.request.headers.get(CSRF_HEADER)).toBe('tok-en');
    req.flush({ ok: true });
  });
});

describe('csrfToken reader', () => {
  const fakeDoc = (cookie: string) => ({ cookie } as Document);

  it('returns the token, the empty string, and never another cookie', () => {
    expect(csrfToken(fakeDoc(`${CSRF_COOKIE}=${TOKEN}`))).toBe(TOKEN);
    expect(csrfToken(fakeDoc('other=1; __Secure-session=abc'))).toBe('');
    expect(csrfToken(fakeDoc(`theme=dark; ${CSRF_COOKIE}=${TOKEN}; path=/`))).toBe(TOKEN);
    // A cookie literally named `detector_csrf_x` is not this cookie.
    expect(csrfToken(fakeDoc(`${CSRF_COOKIE}x=nope`))).toBe('');
  });

  it('is the interceptor the backend expects: same header name, safe methods skipped', () => {
    // These two names are a protocol between two files in two languages. Pinning them here means a
    // rename on either side fails a test instead of shipping a UI that cannot save.
    expect(CSRF_COOKIE).toBe('detector_csrf');
    expect(CSRF_HEADER).toBe('X-CSRF-Token');
    expect(typeof csrfInterceptor).toBe('function');
  });
});
