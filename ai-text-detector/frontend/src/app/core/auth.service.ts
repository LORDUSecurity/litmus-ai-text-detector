import { DOCUMENT } from '@angular/common';
import { Injectable, computed, inject, signal } from '@angular/core';
import { Observable, tap } from 'rxjs';
import { BackendService } from './backend.service';
import type { AuthResponse, SessionUser } from './backend.types';

/** Owns the signed-in session for the whole app.

    The backend keeps the session in a signed HttpOnly cookie, so there is nothing sensitive
    to store on the client: this service just mirrors WHO is signed in (and their theme) into
    a signal the nav shell, the admin gate and the detect page read. `boot()` is called once at
    startup to pick up an existing cookie; a 401 there simply means "not signed in yet".

    It also mirrors WHICH DEPLOYMENT it is talking to. The container and the packaged exe serve
    the same built bundle, so the difference cannot be a build flag -- it arrives on `/auth/me`
    as `auth_mode`, and `local` means the shell must not render the sign-in gate at all. */
@Injectable({ providedIn: 'root' })
export class AuthService {
  private readonly backend = inject(BackendService);
  private readonly doc = inject(DOCUMENT);

  readonly user = signal<SessionUser | null>(null);
  readonly ready = signal(false);
  readonly mode = signal<'local' | 'accounts'>('accounts');

  readonly isAdmin = computed(() => this.user()?.role === 'admin');
  /** The single-owner local install: no sign-in page, and nothing to sign out of. */
  readonly local = computed(() => this.mode() === 'local');
  /** What to call the person in the header. The legacy handle is the fallback, never the
      label, for anyone who gave us a name. */
  readonly displayName = computed(() => this.user()?.display_name || this.user()?.username || '');

  /** Blue is the tune colour; the theme attribute drives the CSS custom properties in
      styles.css so dark/light is one class flip, not a rebuild. */
  readonly theme = signal<'dark' | 'light'>('dark');

  constructor() {
    this.applyTheme(this.theme());
  }

  private applyTheme(theme: 'dark' | 'light'): void {
    this.doc.documentElement.setAttribute('data-theme', theme);
    this.doc.documentElement.classList.toggle('light', theme === 'light');
  }

  private adopt(res: AuthResponse): AuthResponse {
    this.user.set(res.user);
    if (res.auth_mode) this.mode.set(res.auth_mode);
    if (res.user?.theme) {
      this.theme.set(res.user.theme);
      this.applyTheme(res.user.theme);
    }
    return res;
  }

  /** Pick up an existing session cookie on load. Never throws to the caller: an expired or
      absent cookie is just "signed out". */
  boot(): Observable<AuthResponse> {
    return this.backend.me().pipe(
      tap((res) => this.adopt(res)),
      tap({ error: () => this.user.set(null) }),
      // `ready` flips even on error so the shell stops showing its splash.
      tap({ error: () => this.ready.set(true), next: () => this.ready.set(true) }),
    );
  }

  login(identifier: string, password: string): Observable<AuthResponse> {
    return this.backend.login(identifier, password).pipe(tap((res) => this.adopt(res)));
  }

  register(signup: { name: string; email: string; password: string; confirm: string }): Observable<AuthResponse> {
    return this.backend.register(signup).pipe(tap((res) => this.adopt(res)));
  }

  logout(): Observable<{ ok: boolean }> {
    return this.backend.logout().pipe(tap(() => this.user.set(null)));
  }

  /** Flip dark/light and persist it to the account so it follows the user across devices. */
  toggleTheme(): void {
    const next = this.theme() === 'dark' ? 'light' : 'dark';
    this.theme.set(next);
    this.applyTheme(next);
    if (this.user()) {
      // Fire-and-forget: the local flip already happened; a failed persist is not fatal.
      this.backend.setTheme(next).subscribe({
        next: (res) => this.user.set(res.user),
        error: () => undefined,
      });
    }
  }
}
