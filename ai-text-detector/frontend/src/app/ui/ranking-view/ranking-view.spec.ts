import { ComponentFixture, TestBed } from '@angular/core/testing';
import { HttpErrorResponse } from '@angular/common/http';
import { of, throwError } from 'rxjs';

import { RankingView } from './ranking-view';
import { BackendService } from '../../core/backend.service';
import { AuthService } from '../../core/auth.service';
import type { PublicConfig, RankingResponse, UserKeyView } from '../../core/backend.types';

/**
 * Regression cover for the add-a-key form on the shared (non-admin) Ranking page.
 *
 * The app is zoneless — there is no zone.js in package.json — so a template binding cannot
 * re-check itself when a plain class field changes. `canSave` used to be a `computed()` over
 * plain ngModel fields, which stayed cached at its first (false) value: live on 2026-10-16 a
 * real keystroke fill left the input non-empty while "Save key" remained disabled, so no user
 * could save a key at all. These specs drive the DOM the way a user does — set the value, fire
 * the input event, flush the scheduler — so they fail against that shape and pass against the
 * signal-bound one. No component-property pokes: that is the whole point.
 */
const CONFIG = {
  paper_url: '',
  paper_title: '',
  api_key_guides: [],
  user: null,
  providers: [
    { id: 1, name: 'nvidia', label: 'NVIDIA NIM' },
    { id: 2, name: 'openrouter', label: 'OpenRouter' },
  ],
} as unknown as PublicConfig;

const EMPTY_RANKING = { ranked: [] } as unknown as RankingResponse;

describe('RankingView add-a-key form', () => {
  let fixture: ComponentFixture<RankingView>;
  let addMyKey: any;

  const formEl = () => fixture.nativeElement.querySelector('form') as HTMLFormElement;
  const keyInput = () =>
    fixture.nativeElement.querySelector('input[type=password]') as HTMLInputElement;
  const submitBtn = () => formEl().querySelector('button[type=submit]') as HTMLButtonElement;

  async function type(selector: string, value: string) {
    const el = fixture.nativeElement.querySelector(selector) as HTMLInputElement;
    el.value = value;
    el.dispatchEvent(new Event('input', { bubbles: true }));
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    return el;
  }

  beforeEach(async () => {
    addMyKey = vi.fn();
    const backend = {
      config: () => of(CONFIG),
      ranking: () => of(EMPTY_RANKING),
      myKeys: () => of<UserKeyView[]>([]),
      addMyKey,
      deleteMyKey: () => of({ deleted: 1 }),
    };

    await TestBed.configureTestingModule({
      imports: [RankingView],
      providers: [
        { provide: BackendService, useValue: backend },
        { provide: AuthService, useValue: { isAdmin: () => false, user: () => ({ role: 'user', username: 'nadia' }) } },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(RankingView);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
  });

  it('renders the key field for a signed-in non-admin and keeps the button off until typed', () => {
    expect(keyInput()).toBeTruthy();
    expect(submitBtn().disabled).toBe(true);
  });

  it('enables "Save key" as soon as the user types a key', async () => {
    await type('input[type=password]', '  sk-abcdefgh12345  ');
    // the defect this file exists for: this line was false in the built app on 2026-10-16
    expect(submitBtn().disabled).toBe(false);
  });

  it('sends the trimmed key and the chosen endpoint, then clears the plaintext', async () => {
    addMyKey.mockReturnValue(of({ id: 7, verified: false }));
    await type('input[type=password]', '  sk-abcdefgh12345  ');
    submitBtn().click();
    await fixture.whenStable();
    fixture.detectChanges();

    expect(addMyKey.mock.calls.length).toBe(1);
    expect(addMyKey.mock.calls[0][0]).toEqual({
      provider_id: 1,
      api_key: 'sk-abcdefgh12345',
      model_id: undefined,
    });
    expect(keyInput().value).toBe('');
  });

  it('verifies the key against the model the user names', async () => {
    addMyKey.mockReturnValue(of({ id: 7, verified: true }));
    await type('input[type=password]', 'sk-abcdefgh12345');
    await type('input[name=model]', 'z-ai/glm-5.3-flash');
    submitBtn().click();
    await fixture.whenStable();

    expect(addMyKey.mock.calls[0][0].model_id).toBe('z-ai/glm-5.3-flash');
    const status = fixture.nativeElement.querySelector('.doc-status');
    expect(status.textContent).toContain('verified working');
    expect(status.classList.contains('bad')).toBe(false);
  });

  it('renders the backend rejection instead of failing silently', async () => {
    const message = 'api_key must be 1-512 chars of [A-Za-z0-9._:+/-]';
    addMyKey.mockReturnValue(
      throwError(() => new HttpErrorResponse({ status: 400, error: { error: message } })),
    );
    await type('input[type=password]', 'bad key !!!');
    submitBtn().click();
    await fixture.whenStable();
    fixture.detectChanges();

    const status = fixture.nativeElement.querySelector('.doc-status');
    expect(status).toBeTruthy();
    expect(status.textContent.trim()).toBe(message);
    expect(status.classList.contains('bad')).toBe(true);
    // and the form is usable again rather than stuck on "Verifying…"
    expect(submitBtn().disabled).toBe(false);
  });

  it('refuses to submit whitespace as a key', async () => {
    await type('input[type=password]', '   ');
    expect(submitBtn().disabled).toBe(true);
    submitBtn().click();
    await fixture.whenStable();
    expect(addMyKey.mock.calls.length).toBe(0);
  });
});

/**
 * The fresh-install state, which is a real bug report rather than a nice-to-have: on a packaged
 * first boot (2026-10-16) the DB held zero keys, so the ranking rendered 48 cards with no pick
 * button and the page gave no reason — a user could only conclude the app was broken, while
 * detection in fact still ran on the deployed lane. These specs pin the explanation to the
 * pickable count and to the session, and keep it out of the way as soon as one key exists.
 */
describe('RankingView scoring lock', () => {
  const card = (over: any) =>
    ({
      model: 'z-ai/glm-5.3-flash',
      tag: 'glm',
      rank: 1,
      accuracy: 0.94,
      lane: null,
      provider: 'nvidia',
      provider_name: 'NVIDIA NIM',
      free_tier: true,
      provisional: false,
      n_clean: 48,
      correct: 45,
      wrong: 0,
      review: 3,
      coverage: 0.94,
      verified_key: false,
      admin_key: false,
      ...over,
    }) as any;

  async function build(ranked: any[], signedIn: boolean) {
    const backend = {
      config: () => of({ ...CONFIG, api_key_guides: [{ provider: 'nvidia', url: 'https://example.com' }] }),
      ranking: () => of({ ranked } as unknown as RankingResponse),
      myKeys: () => of<UserKeyView[]>([]),
      addMyKey: vi.fn(),
      deleteMyKey: () => of({ deleted: 1 }),
    };
    await TestBed.configureTestingModule({
      imports: [RankingView],
      providers: [
        { provide: BackendService, useValue: backend },
        {
          provide: AuthService,
          useValue: {
            isAdmin: () => false,
            user: () => (signedIn ? { role: 'user', username: 'nadia' } : null),
          },
        },
      ],
    }).compileComponents();

    const fixture = TestBed.createComponent(RankingView);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    return fixture;
  }

  it('explains the lock and offers the key form when no card can be picked', async () => {
    const f = await build([card({}), card({ model: 'google/gemma-4-31b-it', rank: 2 })], true);
    const note = f.nativeElement.querySelector('.lock-note');
    expect(note, 'the page must say why nothing is pickable').toBeTruthy();
    expect(note.textContent).toContain('no verified key');
    // the honest half: the app is not broken, only the choice of scorer is unavailable
    expect(note.textContent).toContain('Detection still works');
    expect(f.nativeElement.querySelector('button.pick-btn')).toBe(null);
    expect(f.nativeElement.querySelectorAll('.rank-lock').length).toBe(2);
    const btn = note.querySelector('button') as HTMLButtonElement;
    expect(btn.textContent).toContain('Add a key');
  });

  it('focuses the key field from the note instead of only describing it', async () => {
    const f = await build([card({})], true);
    const input = f.nativeElement.querySelector('input[type=password]') as HTMLInputElement;
    (f.nativeElement.querySelector('.lock-note button') as HTMLButtonElement).click();
    f.detectChanges();
    expect(document.activeElement).toBe(input);
  });

  it('disappears the moment one card has a key behind it', async () => {
    const f = await build([card({ admin_key: true }), card({ model: 'other/x', rank: 2 })], true);
    expect(f.nativeElement.querySelector('.lock-note')).toBe(null);
    expect(f.nativeElement.querySelectorAll('button.pick-btn').length).toBe(1);
    expect(f.nativeElement.querySelectorAll('.rank-lock').length).toBe(1);
  });

  it('tells a signed-out visitor to sign in rather than offering a form they cannot use', async () => {
    const f = await build([card({})], false);
    const note = f.nativeElement.querySelector('.lock-note');
    expect(note.textContent).toContain('Sign in first');
    expect(note.querySelector('button')).toBe(null);
  });

  it('says nothing while the ranking is empty or failed', async () => {
    const empty = await build([], true);
    expect(empty.nativeElement.querySelector('.lock-note')).toBe(null);
  });
});
