import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { provideHttpClient } from '@angular/common/http';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { LoginView } from './login-view';
import type { AuthResponse, SessionUser } from '../../core/backend.types';

/**
 * The gate in front of the hosted app is the first form a visitor meets, so its fields are the
 * product's first impression: every other signup asks for a name, an e-mail and a password typed
 * twice, and this one used to ask for a username. A username is also the worst possible
 * identifier for a detector that never sends mail and never shows anyone's handle to anyone else
 * -- it is one more thing to invent, and one more way to be locked out after a typo.
 *
 * These tests drive the DOM, because the failures that matter here are DOM failures: a field the
 * user cannot see, a confirmation the server never receives, a submit that sends a stale field
 * name. The component's own properties are not the contract.
 */
const ALICE: SessionUser = {
  id: 4,
  username: 'alice',
  email: 'alice@example.org',
  display_name: 'Alice Aronsson',
  role: 'user',
  theme: 'dark',
  created_at: '2026-10-19T08:00:00Z',
  last_login_at: null,
};

describe('LoginView', () => {
  let fixture: ComponentFixture<LoginView>;
  let http: HttpTestingController;

  beforeEach(async () => {
    await TestBed.configureTestingModule({
      imports: [LoginView],
      providers: [provideHttpClient(), provideHttpClientTesting()],
    }).compileComponents();
    fixture = TestBed.createComponent(LoginView);
    http = TestBed.inject(HttpTestingController);
    fixture.detectChanges();
    await fixture.whenStable();
  });

  afterEach(() => http.verify());

  const input = (name: string): HTMLInputElement =>
    fixture.nativeElement.querySelector(`input[name="${name}"]`) as HTMLInputElement;
  const hasInput = (name: string): boolean => !!input(name);
  const labels = (): string[] =>
    Array.prototype.slice
      .call(fixture.nativeElement.querySelectorAll('.field > span'))
      .map((el: Element) => (el.textContent ?? '').trim());
  const type = (name: string, value: string): void => {
    const el = input(name);
    el.value = value;
    el.dispatchEvent(new Event('input'));
    fixture.detectChanges();
  };
  const tab = (label: string): HTMLButtonElement =>
    Array.prototype.slice
      .call(fixture.nativeElement.querySelectorAll('.auth-modes button'))
      .find((b: HTMLButtonElement) => (b.textContent ?? '').trim() === label)!;
  const submitBtn = (): HTMLButtonElement =>
    fixture.nativeElement.querySelector('form button[type="submit"]') as HTMLButtonElement;
  const errorText = (): string =>
    (fixture.nativeElement.querySelector('.auth-error')?.textContent ?? '').trim();

  /** Switch to Create account and let the new controls settle.
      `whenStable()` is load-bearing, not tidiness: the four signup inputs are created by the mode
      flip, and the wiring an NgModel needs before it will accept a keystroke completes one
      microtask later. Type before that lands and the value reaches the DOM but never the signal,
      so submit() reads an empty form and refuses it -- which looks exactly like a broken component
      unless the tick is in the test. The sign-in fields need no such wait: `beforeEach` settled
      them, which is why the three sign-in specs passed from the first run. */
  async function toRegister(): Promise<void> {
    tab('Create account').click();
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  }

  /** Click submit and render what the click produced, so `errorText()` can see the `<p>` the
      template only inserts once change detection has read the error signal. */
  function clickSubmit(): void {
    submitBtn().click();
    fixture.detectChanges();
  }

  // ---------------------------------------------------------------- identity

  /** The card's mark and heading are the product's first face, and the product is one object: the
      prose-bar thumbnail that `tools/make_icon.py` writes into both favicons and `app.html` paints
      in the header — three horizontal bars at three widths, the shape of a marked-up paragraph.
      This view used to carry a shield with a tick inside it, which is a second logo on the same
      product, and to title itself "AI text detection", which is what every detector titles itself.
      The three `d` strings below are the header's, copied on purpose: change the mark in one place
      and this goes red. */
  it('carries the same mark and the same name as the rest of the app', () => {
    const heading = fixture.nativeElement.querySelector('.auth-brand h1') as HTMLElement | null;
    expect((heading?.textContent ?? '').trim()).toBe('Litmus');
    const bars = Array.from(
      fixture.nativeElement.querySelectorAll('.brand-mark svg path') as NodeListOf<Element>,
    ).map((p) => p.getAttribute('d') ?? '');
    expect(bars).toEqual(['M4.5 7.5h11', 'M4.5 12h14', 'M4.5 16.5h8']);
  });

  // ---------------------------------------------------------------- sign in

  it('signs in with an e-mail address, not a username', () => {
    expect(labels()[0]).toBe('Email');
    expect(hasInput('username')).toBe(false);
    expect(input('identifier').type).toBe('email');
    expect(input('identifier').placeholder).toContain('@');
  });

  it('posts the address as the identifier', () => {
    type('identifier', 'alice@example.org');
    type('password', 'userpass123');
    clickSubmit();

    const req = http.expectOne('/api/auth/login');
    expect(req.request.body).toEqual({ identifier: 'alice@example.org', password: 'userpass123' });
    req.flush({ user: ALICE, auth_mode: 'accounts' } as AuthResponse);
    expect(errorText()).toBe('');
  });

  it('names both halves of the failure when the credentials are wrong', () => {
    type('identifier', 'alice@example.org');
    type('password', 'wrong');
    clickSubmit();
    http
      .expectOne('/api/auth/login')
      .flush({ error: 'invalid email or password', error_code: 'BAD_CREDENTIALS' },
             { status: 401, statusText: 'Unauthorized' });
    fixture.detectChanges();
    expect(errorText()).toContain('invalid email or password');
  });

  // -------------------------------------------------------------------- sign up

  it('asks for a name, an e-mail and a password typed twice on Create account', async () => {
    await toRegister();

    expect(labels()).toEqual(['Name', 'Email', 'Password', 'Confirm password']);
    expect(hasInput('username')).toBe(false);
  });

  it('posts all four signup fields and signs the new account straight in', async () => {
    await toRegister();

    type('name', 'Alice Aronsson');
    type('email', 'alice@example.org');
    type('password', 'userpass123');
    type('confirm', 'userpass123');
    clickSubmit();

    const req = http.expectOne('/api/auth/register');
    expect(req.request.body).toEqual({
      name: 'Alice Aronsson',
      email: 'alice@example.org',
      password: 'userpass123',
      confirm: 'userpass123',
    });
    req.flush({ user: ALICE, auth_mode: 'accounts' } as AuthResponse);
    await fixture.whenStable();
    fixture.detectChanges();
    expect(errorText()).toBe('');
    // The typed secrets are wiped once the account exists -- the page stays open on a shared
    // machine, and the confirmation field is the one nobody expects to still hold a password.
    // This is why the fields are signals: under zoneless a plain `this.password = ''` never
    // reaches the input, so the wipe would have happened only in the model.
    expect(input('password').value).toBe('');
    expect(input('confirm').value).toBe('');
  });

  it('refuses two different passwords before anything leaves the page', async () => {
    await toRegister();

    type('name', 'Alice Aronsson');
    type('email', 'alice@example.org');
    type('password', 'userpass123');
    type('confirm', 'userpass124');
    clickSubmit();

    expect(errorText().toLowerCase()).toContain('match');
    http.expectNone('/api/auth/register');
  });

  it('refuses an address that is not an address before anything leaves the page', async () => {
    await toRegister();

    type('name', 'Alice Aronsson');
    type('email', 'alice@example');
    type('password', 'userpass123');
    type('confirm', 'userpass123');
    clickSubmit();
    expect(errorText().toLowerCase()).toContain('email');
    http.expectNone('/api/auth/register');
  });

  it('refuses a short password rather than letting the server reject the form', async () => {
    await toRegister();

    type('name', 'Alice Aronsson');
    type('email', 'alice@example.org');
    type('password', 'short');
    type('confirm', 'short');
    clickSubmit();
    expect(errorText()).toContain('8 characters');
    http.expectNone('/api/auth/register');
  });

  it('reports the server answer when the address already has an account', async () => {
    await toRegister();

    type('name', 'Alice Again');
    type('email', 'alice@example.org');
    type('password', 'userpass123');
    type('confirm', 'userpass123');
    clickSubmit();
    http
      .expectOne('/api/auth/register')
      .flush({ error: 'an account already exists for that email', error_code: 'EMAIL_TAKEN' },
             { status: 409, statusText: 'Conflict' });
    fixture.detectChanges();
    expect(errorText()).toContain('already exists');
  });

  it('says which field is missing instead of submitting a half a person', async () => {
    await toRegister();

    type('email', 'alice@example.org');
    type('password', 'userpass123');
    type('confirm', 'userpass123');
    clickSubmit();
    expect(errorText()).toContain('name');
    http.expectNone('/api/auth/register');
  });

  it('keeps the typed name and address when the two passwords disagree', async () => {
    await toRegister();

    type('name', 'Alice Aronsson');
    type('email', 'alice@example.org');
    type('password', 'userpass123');
    type('confirm', 'userpass124');
    clickSubmit();

    expect(input('name').value).toBe('Alice Aronsson');
    expect(input('email').value).toBe('alice@example.org');
  });
});
