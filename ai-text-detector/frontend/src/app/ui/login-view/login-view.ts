import { Component, inject, signal } from '@angular/core';
import { FormsModule } from '@angular/forms';
import { HttpErrorResponse } from '@angular/common/http';
import { AuthService } from '../../core/auth.service';

/** The gate in front of the hosted app: sign in with an e-mail, or create an account with a
    name, an e-mail and a password typed twice. The backend keeps the session in a signed
    HttpOnly cookie, so there is no token to store on the client. Register auto-logs-in (the
    backend returns the new session), so one submit is enough.

    A username is gone as a thing to invent. The sign-in field still accepts one, because every
    account created before this change has only a handle and no way to add an address from the
    page -- `identifier` is sent, and the backend resolves either.

    The four fields are signals, not plain properties, and are bound `[ngModel]` +
    `(ngModelChange)` rather than `[(ngModel)]`. This app runs zoneless, so a mutation of a plain
    field is invisible to change detection: the wipe below (`this.password.set('')`) would change
    the model and never reach the input, and the form would quietly keep the typed secret on
    screen after a successful signup. */
@Component({
  selector: 'app-login-view',
  standalone: true,
  imports: [FormsModule],
  template: `
    <div class="auth-wrap">
      <div class="auth-card">
        <div class="auth-brand">
          <!-- The same three prose bars the header paints and tools/make_icon.py stamps into both
               favicons: the product's mark is a thumbnail of a marked-up paragraph, and a login
               card carrying a different glyph is a second logo. -->
          <span class="brand-mark" aria-hidden="true">
            <svg viewBox="0 0 24 24">
              <path d="M4.5 7.5h11" />
              <path d="M4.5 12h14" />
              <path d="M4.5 16.5h8" />
            </svg>
          </span>
          <h1>Litmus</h1>
        </div>
        <p class="tagline">
          Sign in with your e-mail to score text with the calibrated lane and keep your history.
          New here? Create an account — a name, an address and a password.
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
          @if (mode() === 'register') {
            <label class="field">
              <span>Name</span>
              <input name="name" type="text" [ngModel]="name()" (ngModelChange)="name.set($event)"
                     autocomplete="name" placeholder="your name" [disabled]="busy()" />
            </label>
          }

          @if (mode() === 'login') {
            <label class="field">
              <span>Email</span>
              <input name="identifier" type="email" [ngModel]="identifier()"
                     (ngModelChange)="identifier.set($event)"
                     autocomplete="username" placeholder="you&#64;example.com" [disabled]="busy()" />
            </label>
          } @else {
            <label class="field">
              <span>Email</span>
              <input name="email" type="email" [ngModel]="email()" (ngModelChange)="email.set($event)"
                     autocomplete="email" placeholder="you&#64;example.com" [disabled]="busy()" />
            </label>
          }

          <label class="field">
            <span>Password</span>
            <input type="password" name="password" [ngModel]="password()"
                   (ngModelChange)="password.set($event)"
                   [attr.autocomplete]="mode() === 'login' ? 'current-password' : 'new-password'"
                   placeholder="at least 8 characters" [disabled]="busy()" />
          </label>

          @if (mode() === 'register') {
            <label class="field">
              <span>Confirm password</span>
              <input type="password" name="confirm" [ngModel]="confirm()"
                     (ngModelChange)="confirm.set($event)" autocomplete="new-password"
                     placeholder="type it again" [disabled]="busy()" />
            </label>
          }

          <button class="primary" type="submit" [disabled]="busy()">
            {{ busy() ? 'Working…' : (mode() === 'login' ? 'Sign in' : 'Create account') }}
          </button>
        </form>

        <p class="auth-foot">
          @if (mode() === 'login') {
            Signing in on this site? Use the address you signed up with. An account created before
            e-mails worked with a plain handle, and that still gets you in.
          } @else {
            Your password is hashed on the server and the session lives in a secure cookie. Nothing
            is ever mailed to you — the address is only how you sign in. Admins set the models and
            thresholds for everyone; you never touch the advanced parameters.
          }
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

  readonly name = signal('');
  readonly identifier = signal('');
  readonly email = signal('');
  readonly password = signal('');
  readonly confirm = signal('');

  /** The shape the backend accepts. It deliberately does not enforce RFC 5322 — this app never
      sends mail, so the only useful rule is "does this look like an address at all": something,
      then @, then a domain with a dot. Everything else is a typo the user should be told about
      before they lose the form. */
  private static readonly EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/;
  private static readonly MIN_PASSWORD = 8;

  submit(): void {
    const bad = this.validate();
    if (bad) {
      this.error.set(bad);
      return;
    }
    this.error.set(null);
    this.busy.set(true);
    const pw = this.password();
    const op =
      this.mode() === 'login'
        ? this.auth.login(this.identifier().trim(), pw)
        : this.auth.register({
            name: this.name().trim(),
            email: this.email().trim().toLowerCase(),
            password: pw,
            confirm: this.confirm(),
          });
    op.subscribe({
      next: () => {
        this.busy.set(false);
        // The secrets leave the DOM as soon as the account exists. The name and address stay:
        // a rejected sign-up is usually one of them being wrong, and typing an e-mail twice is
        // how people end up with two accounts.
        this.password.set('');
        this.confirm.set('');
      },
      error: (err: unknown) => {
        this.busy.set(false);
        this.error.set(this.describe(err));
      },
    });
  }

  /** Checked here as well as on the server, so a half-filled form costs a keystroke to notice
      rather than a round trip -- and so nothing leaves the page that the backend would refuse. */
  private validate(): string | null {
    if (this.mode() === 'login') {
      if (!this.identifier().trim() || !this.password()) {
        return 'Enter your email and your password.';
      }
      return null;
    }
    if (!this.name().trim()) return 'Enter your name.';
    if (!LoginView.EMAIL_RE.test(this.email().trim())) return 'Enter a valid email address.';
    if (this.password().length < LoginView.MIN_PASSWORD) {
      return `Use at least ${LoginView.MIN_PASSWORD} characters for the password.`;
    }
    if (this.password() !== this.confirm()) return 'The two passwords do not match.';
    return null;
  }

  private describe(err: unknown): string {
    if (err instanceof HttpErrorResponse) {
      const body = err.error as { error?: string; message?: string } | null;
      const text = body && typeof body === 'object' ? (body.error ?? body.message) : '';
      if (err.status === 0) {
        return 'Cannot reach the backend on /api. Is the Flask server running (port 8787)?';
      }
      return text || `HTTP ${err.status} — check the email and password.`;
    }
    return String(err);
  }
}
