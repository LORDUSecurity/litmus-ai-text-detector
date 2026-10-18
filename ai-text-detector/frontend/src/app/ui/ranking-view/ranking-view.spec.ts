import { Component, signal } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { HttpErrorResponse } from '@angular/common/http';
import { of, throwError } from 'rxjs';

import { RankingView } from './ranking-view';
import { BackendService } from '../../core/backend.service';
import { AuthService } from '../../core/auth.service';
import { DetectionService } from '../../core/detection.service';
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

/** A host, so a parent's listener is asserted the way the shell really binds it
    (`<app-ranking-view (clearModel)="…">`) rather than by poking the child's EventEmitter. */
@Component({
  selector: 'app-host-ranking',
  standalone: true,
  imports: [RankingView],
  template: '<app-ranking-view (clearModel)="released = true" />',
})
class HostRanking {
  released = false;
}

/** The pool/discovery calls the view makes on load. Every stub in this file needs them: the
    component reads them in ngOnInit, and a missing method would fail the spec for the wrong
    reason (a TypeError, not a behaviour change). Fresh mocks per call, so no spec inherits
    another's recorded calls. */
const poolStubs = () => ({
  myPool: () => of({ pool: [] }),
  addToPool: vi.fn(),
  removeFromPool: vi.fn(),
  setPoolOrder: vi.fn(),
  replaceInPool: vi.fn(),
  discover: vi.fn(),
  discoveryStatus: () => of({ jobs: [], screen_texts: 'probe2', note: '' }),
});

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
      ...poolStubs(),
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
      ...poolStubs(),
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

/**
 * The citation slot on the Ranking page.
 *
 * The report used to be reachable only when an admin had typed a URL, and a fresh install has none —
 * so the page that claims "the best result we found" linked to nothing. The report now ships inside
 * this bundle (`public/research/report.html`) and that in-site copy is the fallback; `paper_url` is
 * only an override, because the settings validator rejects a relative path so an in-site target
 * could never have been stored there anyway. These specs are what keep the two states distinct:
 * blank must land on the shipped copy, not on an empty href, and a saved DOI must win.
 */
describe('RankingView report link', () => {
  async function buildWith(config: Partial<PublicConfig>) {
    const backend = {
      config: () => of({ ...CONFIG, ...config } as unknown as PublicConfig),
      ranking: () => of(EMPTY_RANKING),
      myKeys: () => of<UserKeyView[]>([]),
      addMyKey: vi.fn(),
      deleteMyKey: () => of({ deleted: 1 }),
      ...poolStubs(),
    };
    await TestBed.configureTestingModule({
      imports: [RankingView],
      providers: [
        { provide: BackendService, useValue: backend },
        {
          provide: AuthService,
          useValue: { isAdmin: () => false, user: () => ({ role: 'user', username: 'nadia' }) },
        },
      ],
    }).compileComponents();

    const f = TestBed.createComponent(RankingView);
    f.detectChanges();
    await f.whenStable();
    f.detectChanges();
    return f;
  }

  const link = (f: ComponentFixture<RankingView>) =>
    f.nativeElement.querySelector('a.paper-link') as HTMLAnchorElement;

  it('falls back to the copy this site serves when no paper URL is configured', async () => {
    const f = await buildWith({ paper_url: '', paper_title: '' });
    expect(link(f), 'the citation must not vanish on a fresh install').toBeTruthy();
    expect(link(f).getAttribute('href')).toBe('/research/report.html');
    expect(link(f).textContent).toContain('Technical report');
  });

  it('prefers the admin\'s archival URL and their own label', async () => {
    const f = await buildWith({
      paper_url: 'https://zenodo.org/doi/10.5281/zenodo.2',
      paper_title: 'Read the preprint',
    });
    expect(link(f).getAttribute('href')).toBe('https://zenodo.org/doi/10.5281/zenodo.2');
    expect(link(f).textContent).toContain('Read the preprint');
  });

  it('labels a saved URL with a default rather than showing an empty link', async () => {
    const f = await buildWith({
      paper_url: 'https://zenodo.org/doi/10.5281/zenodo.2',
      paper_title: '',
    });
    expect(link(f).getAttribute('href')).toContain('zenodo.org');
    expect(link(f).textContent).toContain('Read the paper');
  });
});

/**
 * The using pool: the user's ordered list of models, which is what detection now follows and
 * what a failover walks down.
 *
 * Two things here are easy to get subtly wrong and expensive when wrong. First, the list is
 * rendered in *served* order, which is not the user's order once a model has stopped answering
 * — so the row has to carry both numbers and say why they differ, otherwise a demotion looks
 * like the app silently re-ranked the user's choice. Second, the order is written by the drag
 * AND by the arrow buttons: an order that only a pointer can change is not a control a keyboard
 * user has. These specs drive the DOM, per the file's existing discipline.
 */
describe('RankingView using pool', () => {
  const entry = (over: any = {}) =>
    ({
      provider: 'nvidia',
      model: 'm/a',
      source: 'user',
      your_rank: 1,
      position: 1,
      health: 'ok',
      consecutive_failures: 0,
      last_error: '',
      last_ok_at: null,
      demoted: false,
      ...over,
    }) as any;

  const card = (over: any = {}) =>
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
      verified_key: true,
      admin_key: false,
      in_pool: false,
      health: 'unknown',
      ...over,
    }) as any;

  async function build(opts: {
    pool?: any[];
    ranked?: any[];
    keys?: any[];
    discoverReply?: any;
    signedIn?: boolean;
  } = {}) {
    const mocks = {
      ...poolStubs(),
      addToPool: vi.fn(() => of({ pool: opts.pool ?? [] })),
      removeFromPool: vi.fn(() => of({ pool: [] })),
      setPoolOrder: vi.fn((order: any[]) => of({ pool: order.map((o: any, i: number) => entry({ ...o, position: i + 1 })) })),
      discover: vi.fn(() => of(opts.discoverReply ?? { ok: true, job_id: 3, queued: true, status: 'queued' })),
      myPool: () => of({ pool: opts.pool ?? [] }),
      discoveryStatus: () => of({ jobs: [], screen_texts: 'probe2', note: '2 texts, probe tier' }),
    };
    const backend = {
      config: () => of(CONFIG),
      ranking: () => of({ ranked: opts.ranked ?? [] } as unknown as RankingResponse),
      myKeys: () => of<UserKeyView[]>((opts.keys ?? []) as any),
      addMyKey: vi.fn(),
      deleteMyKey: () => of({ deleted: 1 }),
      ...mocks,
    };
    await TestBed.configureTestingModule({
      imports: [RankingView],
      providers: [
        { provide: BackendService, useValue: backend },
        {
          provide: AuthService,
          useValue: {
            isAdmin: () => false,
            user: () => (opts.signedIn === false ? null : { role: 'user', username: 'nadia' }),
          },
        },
      ],
    }).compileComponents();

    const fixture = TestBed.createComponent(RankingView);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    return { fixture, mocks, panel: () => fixture.nativeElement.querySelector('.pool-panel') };
  }

  const rows = (f: ComponentFixture<RankingView>) =>
    Array.from(f.nativeElement.querySelectorAll('.pool-row')) as HTMLElement[];

  /** jsdom has no DragEvent, and the handlers only need preventDefault + an optional
      dataTransfer, so a plain Event exercises the same code path. */
  function drag(f: ComponentFixture<RankingView>, from: number, to: number) {
    const r = rows(f);
    r[from].dispatchEvent(new Event('dragstart', { bubbles: true }));
    f.detectChanges();
    r[to].dispatchEvent(new Event('dragover', { bubbles: true }));
    f.detectChanges();
    r[to].dispatchEvent(new Event('drop', { bubbles: true }));
    f.detectChanges();
  }

  it('renders the served order and explains a demotion with both numbers', async () => {
    const { fixture } = await build({
      pool: [
        entry({ model: 'm/b', your_rank: 2, position: 1 }),
        entry({ model: 'm/a', your_rank: 1, position: 2, health: 'down', demoted: true,
                consecutive_failures: 3, last_error: '404' }),
      ],
    });
    const r = rows(fixture);
    expect(r.length).toBe(2);
    expect(r[0].textContent).toContain('m/b');
    expect(r[1].textContent).toContain('m/a');
    expect(r[1].classList.contains('demoted')).toBe(true);
    // the sentence that keeps a health demotion from looking like a silent re-ranking
    expect(r[1].textContent).toContain('you ranked it #1');
    expect(r[1].textContent).toContain('stopped answering');
  });

  it('writes the drag order to the server and keeps the local list until it answers', async () => {
    const { fixture, mocks } = await build({
      pool: [entry({ model: 'm/a' }), entry({ model: 'm/b', your_rank: 2, position: 2 }),
             entry({ model: 'm/c', your_rank: 3, position: 3 })],
    });
    drag(fixture, 0, 2);
    await fixture.whenStable();
    fixture.detectChanges();

    expect(mocks.setPoolOrder.mock.calls.length).toBe(1);
    expect(mocks.setPoolOrder.mock.calls[0][0]).toEqual([
      { provider: 'nvidia', model: 'm/b' },
      { provider: 'nvidia', model: 'm/c' },
      { provider: 'nvidia', model: 'm/a' },
    ]);
  });

  it('moves a row with the arrow buttons, so the order does not depend on a pointer', async () => {
    const { fixture, mocks } = await build({
      pool: [entry({ model: 'm/a' }), entry({ model: 'm/b', your_rank: 2, position: 2 })],
    });
    const down = rows(fixture)[0].querySelector('button[aria-label^="Move m/a down"]') as HTMLButtonElement;
    expect(down.disabled).toBe(false);
    // the first row cannot go up: a no-op move must not reach the server
    const up = rows(fixture)[0].querySelector('button[aria-label^="Move m/a up"]') as HTMLButtonElement;
    expect(up.disabled).toBe(true);

    down.click();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(mocks.setPoolOrder.mock.calls[0][0]).toEqual([
      { provider: 'nvidia', model: 'm/b' },
      { provider: 'nvidia', model: 'm/a' },
    ]);
  });

  // Add and remove get their own tests on purpose: `build()` configures a TestBed, and Angular
  // refuses to configure one that is already instantiated, so a second `build()` inside the same
  // `it` is not a fresh fixture -- it is an error wearing the shape of a passing test.
  it('adds a model to the using pool from its rank card', async () => {
    const { fixture, mocks } = await build({ ranked: [card({})], pool: [] });
    const add = Array.from(fixture.nativeElement.querySelectorAll('.pool-actions button'))
      .find((b: any) => b.textContent.includes('Add to using pool')) as HTMLButtonElement;
    expect(add, 'a card with a key behind it must offer the pool').toBeTruthy();
    add.click();
    await fixture.whenStable();
    expect(mocks.addToPool.mock.calls[0]).toEqual(['nvidia', 'z-ai/glm-5.3-flash']);
  });

  it('removes a model from the pool row, and says which model it is removing', async () => {
    const { fixture, mocks } = await build({ pool: [entry({ model: 'm/a' })] });
    const remove = Array.from(fixture.nativeElement.querySelectorAll('.pool-row button'))
      .find((b: any) => b.textContent.includes('Remove')) as HTMLButtonElement;
    expect(remove.getAttribute('aria-label')).toContain('m/a');
    remove.click();
    await fixture.whenStable();
    expect(mocks.removeFromPool.mock.calls[0]).toEqual(['nvidia', 'm/a']);
  });

  it('offers discovery only on endpoints the caller has a key for', async () => {
    const { fixture, mocks } = await build({
      keys: [{ id: 4, provider_id: 1, key_hint: 'sk-…', verified: true, label: 'mine' }],
    });
    const btns = Array.from(fixture.nativeElement.querySelectorAll('.discover-actions button')) as
      HTMLButtonElement[];
    expect(btns.length, 'only nvidia has a key; openrouter must not be offered').toBe(1);
    expect(btns[0].textContent).toContain('NVIDIA NIM');
    btns[0].click();
    await fixture.whenStable();
    expect(mocks.discover.mock.calls[0]).toEqual([1]);
  });

  it('reports a refused discovery as refused, not as queued', async () => {
    const { fixture } = await build({
      keys: [{ id: 4, provider_id: 1, key_hint: 'sk-…', verified: true, label: 'mine' }],
      discoverReply: {
        job_id: null, queued: false, status: 'disabled',
        reason: 'the discovery worker is not running in this process',
      },
    });
    (fixture.nativeElement.querySelector('.discover-actions button') as HTMLButtonElement).click();
    await fixture.whenStable();
    fixture.detectChanges();
    const status = fixture.nativeElement.querySelector('.pool-panel .doc-status');
    expect(status.textContent).toContain('Not queued');
    expect(status.textContent).toContain('not running');
    expect(status.classList.contains('bad')).toBe(true);
  });

  it('is absent for a signed-out visitor, who has no pool to order', async () => {
    const { panel } = await build({ signedIn: false, pool: [entry({})] });
    expect(panel()).toBe(null);
  });
});

/**
 * The "which model answers my runs" bar, and the direct PDF link.
 *
 * The ranking table answers "which model judges best"; it never answered "so what runs on my
 * text" — and that second question is the one the brief makes the whole page for ("these ranking
 * are the one used in auto order"). #142 is what a page that cannot answer it costs: every plain
 * user's Detect carried a baked lane, so the deployed default the admin had promoted never
 * answered anything while the page went on naming it. So the bar renders what /ranking reports
 * from the same functions /detect walks, and it says the three things a user otherwise has to
 * guess: the model that answers, the order the auto-switch follows behind it, and which one is
 * the admin's default. `lane_authority` absent (an older backend) renders no bar rather than a
 * bar built on a client-side guess.
 */
describe('RankingView lane authority', () => {
  const AUTH = (over: any = {}) =>
    ({
      deployed_default: { model: 'deployed/primary', provider: 'nvidia' },
      deployed_tie_break: null,
      next: { model: 'deployed/primary', provider: 'nvidia', source: 'deployed-default' },
      failover: [],
      ...over,
    }) as any;

  const card = (over: any = {}) =>
    ({
      model: 'z-ai/glm-5.3-flash', tag: 'glm', rank: 1, accuracy: 0.94, lane: null,
      provider: 'nvidia', provider_name: 'NVIDIA NIM', free_tier: true, provisional: false,
      n_clean: 48, correct: 45, wrong: 0, review: 3, coverage: 0.94,
      verified_key: true, admin_key: false, in_pool: false, health: 'unknown', ...over,
    }) as any;

  async function build(opts: { authority?: any; picked?: string | null } = {}) {
    const backend = {
      config: () => of(CONFIG),
      ranking: () => of({ ranked: [card()], lane_authority: opts.authority } as unknown as RankingResponse),
      myKeys: () => of<UserKeyView[]>([]),
      addMyKey: vi.fn(),
      deleteMyKey: () => of({ deleted: 1 }),
      ...poolStubs(),
    };
    await TestBed.configureTestingModule({
      imports: [HostRanking, RankingView],
      providers: [
        { provide: BackendService, useValue: backend },
        {
          provide: AuthService,
          useValue: { isAdmin: () => false, user: () => ({ role: 'user', username: 'nadia' }) },
        },
        {
          provide: DetectionService,
          useValue: {
            pickedModel: () => opts.picked ?? null,
            usePickedModel: vi.fn(),
            clearPickedModel: vi.fn(),
          },
        },
      ],
    }).compileComponents();

    const fixture = TestBed.createComponent(HostRanking);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    return fixture;
  }

  const bar = (f: ComponentFixture<HostRanking>) =>
    f.nativeElement.querySelector('.lane-authority') as HTMLElement;

  it('names the model that answers, the admin default, and the tie-break state', async () => {
    const f = await build({ authority: AUTH() });
    const text = bar(f).textContent?.replace(/\s+/g, ' ') ?? '';
    expect(text).toContain('deployed/primary');
    expect(text, 'the admin row has to be labelled as the admin row').toContain('Deployed default');
    expect(text).toContain('tie-break lane off');
  });

  it('lists the auto-switch order the user pool gives him, in his order', async () => {
    const f = await build({
      authority: AUTH({
        next: { model: 'm/one', provider: 'nvidia', source: 'user-pool' },
        failover: [{ model: 'm/two', provider: 'nvidia' }, { model: 'm/three', provider: 'nvidia' }],
      }),
    });
    const text = bar(f).textContent?.replace(/\s+/g, ' ') ?? '';
    expect(text).toContain('m/one');
    const two = text.indexOf('m/two');
    const three = text.indexOf('m/three');
    expect(two).toBeGreaterThan(-1);
    expect(three, 'the second rescue must be named after the first').toBeGreaterThan(two);
  });

  it('says out loud when a failing model has nothing behind it', async () => {
    const f = await build({
      authority: AUTH({ next: { model: 'm/one', provider: 'nvidia', source: 'user-pool' } }),
    });
    expect(bar(f).textContent).toContain('nothing behind it');
  });

  it('marks a pinned model as one run only and releases it from this page', async () => {
    const f = await build({ authority: AUTH(), picked: 'pinned/model-x' });
    const text = bar(f).textContent?.replace(/\s+/g, ' ') ?? '';
    expect(text).toContain('pinned/model-x');
    expect(text).toContain('this run only');
    const btn = bar(f).querySelector('button.release-pick') as HTMLButtonElement;
    expect(btn, 'the bar that names the pin has to be the bar that clears it').toBeTruthy();
    expect(f.componentInstance.released).toBe(false);
    btn.click();
    expect(f.componentInstance.released).toBe(true);
  });

  it('says nothing is configured instead of naming a model nobody enabled', async () => {
    const f = await build({
      authority: AUTH({ deployed_default: null, next: null }),
    });
    const text = bar(f).textContent?.replace(/\s+/g, ' ') ?? '';
    expect(text).toContain('No primary model is enabled');
    expect(text).not.toContain('deployed/primary');
  });

  it('renders no claim at all when the backend reports no authority', async () => {
    const f = await build({ authority: undefined });
    expect(bar(f)).toBe(null);
  });

  it('links the printable PDF next to the report, not only inside it', async () => {
    const f = await build({ authority: AUTH() });
    const pdf = f.nativeElement.querySelector('a.pdf-link') as HTMLAnchorElement;
    expect(pdf, 'the research PDF has to be reachable from the ranking in one click').toBeTruthy();
    expect(pdf.getAttribute('href')).toBe('/research/report.pdf');
    expect(pdf.getAttribute('target')).toBe('_blank');
  });

  it('chips the card that answers his runs and the card that is the admin default', async () => {
    const f = await build({
      authority: AUTH({
        deployed_default: { model: 'z-ai/glm-5.3-flash', provider: 'nvidia' },
        next: { model: 'z-ai/glm-5.3-flash', provider: 'nvidia', source: 'deployed-default' },
      }),
    });
    f.detectChanges();
    const chips = Array.from(
      f.nativeElement.querySelectorAll('.rank-card .chip'),
    ) as HTMLElement[];
    const labels = chips.map((c) => c.textContent?.trim());
    expect(labels).toContain('deployed default');
    expect(labels).toContain('used for your runs');
  });

  it('does not chip a model that answers neither', async () => {
    const f = await build({
      authority: AUTH({
        deployed_default: { model: 'other/model', provider: 'nvidia' },
        next: { model: 'other/model', provider: 'nvidia', source: 'deployed-default' },
      }),
    });
    f.detectChanges();
    const labels = Array.from(
      f.nativeElement.querySelectorAll('.rank-card .chip'),
    ).map((c) => (c as HTMLElement).textContent?.trim());
    expect(labels).not.toContain('used for your runs');
    expect(labels).not.toContain('deployed default');
  });
});

/**
 * Editing the using pool: change which model fills a slot, without moving anything else.
 *
 * Add-then-remove-then-re-drag costs the user his slot between the calls, and a drag that only
 * ever meant two rows silently rewrote the rank of a third one that happened to be sitting in
 * the *served* list because it had stopped answering (the panel's own promise is that a demoted
 * model comes back where the user put it). So a change is one atomic call, and an order write
 * is expressed in the user's own rank space, not in the health-served order it is drawn in.
 */
describe('RankingView pool edit', () => {
  const entry = (over: any = {}) =>
    ({
      provider: 'nvidia', model: 'm/a', source: 'user', your_rank: 1, position: 1,
      health: 'ok', consecutive_failures: 0, last_error: '', last_ok_at: null,
      demoted: false, ...over,
    }) as any;

  const card = (over: any = {}) =>
    ({
      model: 'z-ai/glm-5.3-flash', tag: 'glm', rank: 1, accuracy: 0.94, lane: null,
      provider: 'nvidia', provider_name: 'NVIDIA NIM', free_tier: true, provisional: false,
      n_clean: 48, correct: 45, wrong: 0, review: 3, coverage: 0.94,
      verified_key: true, admin_key: false, in_pool: false, health: 'unknown', ...over,
    }) as any;

  async function build(opts: { pool?: any[]; ranked?: any[]; replace?: any } = {}) {
    const mocks = {
      ...poolStubs(),
      myPool: () => of({ pool: opts.pool ?? [] }),
      setPoolOrder: vi.fn((order: any[]) =>
        of({ pool: order.map((o: any, i: number) => entry({ ...o, your_rank: i + 1, position: i + 1 })) })),
      replaceInPool: vi.fn(() => opts.replace ?? of({
        pool: [entry({ model: 'z-ai/glm-5.3-flash' })],
      })),
    };
    const backend = {
      config: () => of(CONFIG),
      ranking: () => of({ ranked: opts.ranked ?? [] } as unknown as RankingResponse),
      myKeys: () => of<UserKeyView[]>([]),
      addMyKey: vi.fn(),
      deleteMyKey: () => of({ deleted: 1 }),
      ...mocks,
    };
    await TestBed.configureTestingModule({
      imports: [RankingView],
      providers: [
        { provide: BackendService, useValue: backend },
        {
          provide: AuthService,
          useValue: { isAdmin: () => false, user: () => ({ role: 'user', username: 'nadia' }) },
        },
        {
          provide: DetectionService,
          useValue: { pickedModel: () => null, usePickedModel: vi.fn(), clearPickedModel: vi.fn() },
        },
      ],
    }).compileComponents();

    const fixture = TestBed.createComponent(RankingView);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    return { fixture, mocks };
  }

  const rows = (f: ComponentFixture<RankingView>) =>
    Array.from(f.nativeElement.querySelectorAll('.pool-row')) as HTMLElement[];

  function drag(f: ComponentFixture<RankingView>, from: number, to: number) {
    const r = rows(f);
    r[from].dispatchEvent(new Event('dragstart', { bubbles: true }));
    f.detectChanges();
    r[to].dispatchEvent(new Event('dragover', { bubbles: true }));
    f.detectChanges();
    r[to].dispatchEvent(new Event('drop', { bubbles: true }));
    f.detectChanges();
  }

  it('opens an editor on the row that offers only models he can pay for and not one he already holds', async () => {
    const { fixture } = await build({
      pool: [entry({ model: 'm/a' })],
      ranked: [
        card({}),
        card({ model: 'm/a', rank: 2, verified_key: true, in_pool: true }),
        card({ model: 'm/nokey', rank: 3, verified_key: false, admin_key: false }),
      ],
    });
    const change = rows(fixture)[0].querySelector('button[data-act="change"]') as HTMLButtonElement;
    expect(change, 'an entry the user put in the pool must be editable, not only removable')
      .toBeTruthy();
    change.click();
    fixture.detectChanges();
    const select = rows(fixture)[0].querySelector('select.pool-change-target') as HTMLSelectElement;
    expect(select).toBeTruthy();
    const values = Array.from(select.options).map((o) => o.value);
    expect(values).toContain('nvidia|z-ai/glm-5.3-flash');
    expect(values, 'a model already in the pool cannot replace another one').not.toContain('nvidia|m/a');
    expect(values, 'a model with no key behind it cannot be run').not.toContain('nvidia|m/nokey');
  });

  it('changes the model in place with one atomic call and shows the answer', async () => {
    const { fixture, mocks } = await build({
      pool: [entry({ model: 'm/a' })],
      ranked: [card({})],
    });
    (rows(fixture)[0].querySelector('button[data-act="change"]') as HTMLButtonElement).click();
    fixture.detectChanges();
    const select = rows(fixture)[0].querySelector('select.pool-change-target') as HTMLSelectElement;
    select.value = 'nvidia|z-ai/glm-5.3-flash';
    select.dispatchEvent(new Event('change', { bubbles: true }));
    fixture.detectChanges();
    (rows(fixture)[0].querySelector('button[data-act="save-change"]') as HTMLButtonElement).click();
    await fixture.whenStable();
    fixture.detectChanges();

    expect(mocks.replaceInPool.mock.calls.length).toBe(1);
    expect(mocks.replaceInPool.mock.calls[0]).toEqual(['nvidia', 'm/a', 'nvidia', 'z-ai/glm-5.3-flash']);
    expect(rows(fixture).length).toBe(1);
    expect(rows(fixture)[0].textContent).toContain('z-ai/glm-5.3-flash');
    expect(rows(fixture)[0].textContent).not.toContain('m/a');
  });

  it('reports a refused change and leaves the row exactly as it was', async () => {
    const { fixture, mocks } = await build({
      pool: [entry({ model: 'm/a' })],
      ranked: [card({})],
      replace: throwError(
        () => new HttpErrorResponse({ status: 400, error: { error: 'that model is already in your pool' } }),
      ),
    });
    (rows(fixture)[0].querySelector('button[data-act="change"]') as HTMLButtonElement).click();
    fixture.detectChanges();
    const select = rows(fixture)[0].querySelector('select.pool-change-target') as HTMLSelectElement;
    select.value = 'nvidia|z-ai/glm-5.3-flash';
    select.dispatchEvent(new Event('change', { bubbles: true }));
    // Flush before clicking, exactly as the successful spec does: choosing a model is what
    // enables "save", and a zoneless app only re-checks that binding when the scheduler runs.
    // Without this the synthetic click lands on a still-disabled control, which jsdom correctly
    // ignores -- measured 2026-10-18, the click was silently dropped and no status ever rendered.
    fixture.detectChanges();
    (rows(fixture)[0].querySelector('button[data-act="save-change"]') as HTMLButtonElement).click();
    await fixture.whenStable();
    fixture.detectChanges();

    expect(mocks.removeFromPool.mock.calls.length, 'a refused swap must not delete the old row')
      .toBe(0);
    const status = fixture.nativeElement.querySelector('.pool-panel .doc-status');
    expect(status.textContent).toContain('already in your pool');
    expect(status.classList.contains('bad')).toBe(true);
    expect(rows(fixture)[0].textContent).toContain('m/a');
  });

  it('cancels the editor without writing anything', async () => {
    const { fixture, mocks } = await build({ pool: [entry({ model: 'm/a' })], ranked: [card({})] });
    (rows(fixture)[0].querySelector('button[data-act="change"]') as HTMLButtonElement).click();
    fixture.detectChanges();
    (rows(fixture)[0].querySelector('button[data-act="cancel-change"]') as HTMLButtonElement).click();
    fixture.detectChanges();
    expect(rows(fixture)[0].querySelector('select.pool-change-target')).toBe(null);
    expect(mocks.replaceInPool.mock.calls.length).toBe(0);
  });

  it('writes an order in the user ranks, so a demoted row keeps the rank he gave it', async () => {
    // Served order is a (health, rank) read: c is the user's #1 but stopped answering, so it is
    // drawn last. Moving a row here must not quietly make c his #3.
    const { fixture, mocks } = await build({
      pool: [
        entry({ model: 'm/a', your_rank: 2, position: 1 }),
        entry({ model: 'm/b', your_rank: 3, position: 2 }),
        entry({ model: 'm/c', your_rank: 1, position: 3, health: 'down', demoted: true }),
      ],
    });
    drag(fixture, 0, 1);
    await fixture.whenStable();
    expect(mocks.setPoolOrder.mock.calls[0][0]).toEqual([
      { provider: 'nvidia', model: 'm/c' },
      { provider: 'nvidia', model: 'm/b' },
      { provider: 'nvidia', model: 'm/a' },
    ]);
  });
});
