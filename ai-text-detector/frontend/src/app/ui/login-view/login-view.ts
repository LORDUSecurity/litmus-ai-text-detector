import { Component, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { HttpErrorResponse } from '@angular/common/http';
import { AuthService } from '../../core/auth.service';

/** The gate in front of the whole app. Username + password only; the backend keeps the
    session in a signed HttpOnly cookie, so there is no token to store on the client.
    Register auto-logs-in (the backend returns the new session), so one submit is enough. */
@Component({
  selector: 'app-login-view',
  standalone: true,
  imports: [FormsModule],
  template: `
    <div class="auth-wrap">
      <div class="auth-card">
        <div class="auth-brand">
          <span class="brand-mark" aria-hidden="true">
            <svg viewBox="0 0 24 24">
              <path d="M12 2.6l7.5 4.2v5.4c0 4.7-3.2 8.2-7.5 9.4-4.3-1.2-7.5-4.7-7.5-9.4V6.8L12 2.6z" />
              <path d="M8.2 12.4l2.4 2.4 5-5.4" />
            </svg>
          </span>
          <h1>AI text detection</h1>
        </div>
        <p class="tagline">
          Sign in to score text with the calibrated lane and keep your history. New here? Create an
          account — it takes a username and a password.
        </p>

        <div class="auth-modes" role="tablist">
          <button type="button" role="tab" [attr.aria-selected]="mode() === 'login'"
                  (click)="mode.set('login')">Sign in</button>
          <button type="button" role="tab" [attr.aria-selected]="mode() === 'register'"
                  (click)="mode.set('register')">Create account</button>
        </div>

        @if (error(); as e) {
          <p class="auth-error" role="alert">{{ e }}</p>
        }

        <form (ngSubmit)="submit()">
          <label class="field">
            <span>Username</span>
            <input name="username" [(ngModel)]="username" autocomplete="username"
                   placeholder="your name" [disabled]="busy()" />
          </label>
          <label class="field">
            <span>Password</span>
            <input type="password" name="password" [(ngModel)]="password"
                   [attr.autocomplete]="mode() === 'login' ? 'current-password' : 'new-password'"
                   placeholder="••••••••" [disabled]="busy()" />
          </label>
          <button class="primary" type="submit" [disabled]="busy()">
            {{ busy() ? 'Working…' : (mode() === 'login' ? 'Sign in' : 'Create account') }}
          </button>
        </form>

        <p class="auth-foot">
          Your password is hashed on the server; the session lives in a secure cookie. Admins set
          the models and thresholds for everyone — you never touch the advanced parameters.
        </p>
      </div>
    </div>
  `,
})
export class LoginView {
  private readonly auth = inject(AuthService);

  readonly mode = signal<'login' | 'register'>('login');
  readonly busy = signal(false);
  readonly error = signal<string | null>(null);

  username = '';
  password = '';

  submit(): void {
    const u = this.username.trim();
    if (!u || !this.password) {
      this.error.set('Enter both a username and a password.');
      return;
    }
    this.error.set(null);
    this.busy.set(true);
    const op =
      this.mode() === 'login'
        ? this.auth.login(u, this.password)
        : this.auth.register(u, this.password);
    op.subscribe({
      next: () => {
        this.busy.set(false);
        this.password = '';
      },
      error: (err: unknown) => {
        this.busy.set(false);
        this.error.set(this.describe(err));
      },
    });
  }

  private describe(err: unknown): string {
    if (err instanceof HttpErrorResponse) {
      const body = err.error as { error?: string; message?: string } | null;
      const text = body && typeof body === 'object' ? (body.error ?? body.message) : '';
      if (err.status === 0) {
        return 'Cannot reach the backend on /api. Is the Flask server running (port 8787)?';
      }
      return text || `HTTP ${err.status} — check the username and password.`;
    }
    return String(err);
  }
}
