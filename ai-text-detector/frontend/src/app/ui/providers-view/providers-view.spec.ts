import { ComponentFixture, TestBed } from '@angular/core/testing';
import { HttpErrorResponse } from '@angular/common/http';
import { of, throwError } from 'rxjs';

import { ProvidersView } from './providers-view';
import { BackendService } from '../../core/backend.service';
import { AuthService } from '../../core/auth.service';
import type { PublicConfig, UserKeyView } from '../../core/backend.types';

/**
 * The surface that controls models and endpoints, split out of the Ranking page.
 *
 * The ranking answers "which model judges best"; this answers "which endpoints may I spend, in
 * what order, and what is the failover". They were one page until the split, and the cost showed:
 * a card carried a pick button, a pool button and a key form far above or below it. These specs are
 * the moved behaviour, so the split cannot silently drop it — plus the one new thing the split
 * needs, which is that a locked ranking card can arrive here with its endpoint already chosen.
 *
 * The picker reads `/my/pool/options`, not `/ranking`, and that is a rule rather than a refactor:
 * a model whose probe ran out of time has no measurement, so it is absent from the ranking, and a
 * user who cannot find it there concludes the model is gone. Every spec below that cares about
 * pickability therefore asserts on a `models` row (`options`), including the `slow` and `busy`
 * ones, which are pickable precisely because being slow is a property of the pool that minute.
 *
 * Zoneless caveat carried over from the original file: `canSave` used to be a `computed()` over
 * plain ngModel fields, so a real keystroke left "Save key" disabled forever (live 2026-10-16).
 * Everything here drives the DOM the way a user does — set the value, fire the event, flush.
 */
const CONFIG = {
  paper_url: '',
  paper_title: '',
  api_key_guides: [],
  user: null,
  providers: [
    { id: 1, name: 'nvidia', label: 'NVIDIA NIM', base_url: 'https://integrate.api.nvidia.com/v1',
      free_tier: true, self_hosted: false, host: 'integrate.api.nvidia.com',
      has_admin_key: true, models: [] },
    { id: 2, name: 'openrouter', label: 'OpenRouter', base_url: 'https://openrouter.ai/api/v1',
      free_tier: true, self_hosted: false, host: 'openrouter.ai',
      has_admin_key: false, models: [] },
  ],
} as unknown as PublicConfig;

/** A gateway the caller runs himself: the seeded address is somebody else's machine, so the form
    for this endpoint has to carry his own host and port. `self_hosted` is the flag `/config`
    answers with, and the same one `POST /my/keys` checks before it accepts an override. */
const OMNI = {
  id: 3, name: 'omniroute', label: 'OmniRoute (self-hosted)',
  base_url: 'http://127.0.0.1:20128/v1', free_tier: true, self_hosted: true,
  host: '127.0.0.1', has_admin_key: false,
  models: [{ model_id: 'route/llama', display_name: null, lane: null }],
};

const entry = (over: any = {}) =>
  ({
    provider: 'nvidia', model: 'm/a', source: 'user', your_rank: 1, position: 1,
    health: 'ok', consecutive_failures: 0, last_error: '', last_ok_at: null,
    demoted: false, ...over,
  }) as any;

/** One row of `/my/pool/options`: a `models` row plus the last probe's own verdict.
    `measured: true, probe_status: 'usable'` is the quiet majority — a model the ranking judged. */
const option = (over: any = {}) =>
  ({
    provider: 'nvidia', provider_id: 1, provider_name: 'nvidia',
    base_url: 'https://integrate.api.nvidia.com/v1', free_tier: true,
    host: 'integrate.api.nvidia.com', model: 'z-ai/glm-5.3-flash',
    measured: true, timeout_ms: 120_000, notes: '',
    probe_status: 'usable', probe_note: '', answer_ms: 86_000,
    verified_key: true, admin_key: false, needs_key: false,
    in_pool: false, your_rank: null, health: 'unknown',
    offered: false, offer_seconds_left: null, ...over,
  }) as any;

/** A model that answered the screen only after the first ask timed out: still pickable, and now
    waiting on the user for as long as the window holds. */
const offered = (over: any = {}) =>
  option({
    model: 'm/recovered', measured: false, probe_status: 'pending',
    probe_note: 'timed out at 120000 ms on the first ask, answered on retry',
    answer_ms: 3_400, offered: true, offer_seconds_left: 600, ...over,
  });

const storedKey = {
  id: 4, provider_id: 1, provider_name: 'NVIDIA NIM', key_hint: 'nvapi-…w9',
  verified: true, label: 'mine',
};

async function build(opts: {
  pool?: any[];
  options?: any[];
  keys?: any[];
  config?: Partial<PublicConfig>;
  discoverReply?: any;
  replace?: any;
  keyReply?: any;
  decision?: any;
  signedIn?: boolean;
  focusProviderId?: number | null;
  focusPair?: string | null;
  autoRank?: any;
} = {}): Promise<{ fixture: ComponentFixture<ProvidersView>; mocks: any }> {
  const mocks = {
    myPool: () => of({ pool: opts.pool ?? [] }),
    addToPool: vi.fn(() => of({ pool: opts.pool ?? [] })),
    removeFromPool: vi.fn(() => of({ pool: [] })),
    setPoolOrder: vi.fn((order: any[]) =>
      of({ pool: order.map((o: any, i: number) => entry({ ...o, position: i + 1 })) })),
    autoRankPool: vi.fn(() => opts.autoRank ??
      of({ pool: opts.pool ?? [], ranked: (opts.pool ?? []).length, unranked: 0, note: '' })),
    replaceInPool: vi.fn(() => opts.replace ?? of({ pool: [entry({ model: 'z-ai/glm-5.3-flash' })] })),
    poolOptions: () => of({ options: opts.options ?? [], window_s: 600, fast_ms: 45_000, note: '' }),
    catalogDecision: vi.fn(() => opts.decision ?? of({ pool: [entry({})] })),
    discover: vi.fn(() => of(opts.discoverReply ?? { ok: true, job_id: 3, queued: true, status: 'queued' })),
    discoveryStatus: () => of({ jobs: [], screen_texts: 'probe2', note: '2 texts, probe tier' }),
    addMyKey: vi.fn(() => opts.keyReply ?? of({ id: 7, verified: false })),
    deleteMyKey: vi.fn(() => of({ deleted: 1 })),
  };
  const backend = {
    config: () => of({ ...CONFIG, ...opts.config } as unknown as PublicConfig),
    myKeys: () => of<UserKeyView[]>((opts.keys ?? []) as any),
    ...mocks,
  };
  await TestBed.configureTestingModule({
    imports: [ProvidersView],
    providers: [
      { provide: BackendService, useValue: backend },
      {
        provide: AuthService,
        useValue: {
          isAdmin: () => false,
          // The shared-keys window and the tie-break switch are gated on this; every case below is a
          // signed-in plain user on the hosted build, so both stay off.
          local: () => false,
          user: () => (opts.signedIn === false ? null : { role: 'user', username: 'nadia' }),
        },
      },
    ],
  }).compileComponents();

  const fixture = TestBed.createComponent(ProvidersView);
  if (opts.focusProviderId !== undefined) {
    fixture.componentRef.setInput('focusProviderId', opts.focusProviderId);
  }
  if (opts.focusPair !== undefined) {
    fixture.componentRef.setInput('focusPair', opts.focusPair);
  }
  fixture.detectChanges();
  await fixture.whenStable();
  fixture.detectChanges();
  await fixture.whenStable();
  return { fixture, mocks };
}

async function type(fixture: ComponentFixture<ProvidersView>, selector: string, value: string) {
  const el = fixture.nativeElement.querySelector(selector) as HTMLInputElement;
  el.value = value;
  el.dispatchEvent(new Event('input', { bubbles: true }));
  fixture.detectChanges();
  await fixture.whenStable();
  fixture.detectChanges();
  return el;
}

const rows = (f: ComponentFixture<ProvidersView>) =>
  Array.from(f.nativeElement.querySelectorAll('.pool-row')) as HTMLElement[];

/** The add-models menu: one card per model that has a `models` row, whatever its state. */
const pickCards = (f: ComponentFixture<ProvidersView>) =>
  Array.from(f.nativeElement.querySelectorAll('.pick-card')) as HTMLElement[];

const pickCard = (f: ComponentFixture<ProvidersView>, pair: string) =>
  pickCards(f).find((c) => c.getAttribute('data-pair') === pair) as HTMLElement;

/** Which pairs this click could add. The state lives on the frame and on the button: a model he
    already holds or cannot pay for gets no Add button at all, because a disabled control that
    looks like a promise is the failure this menu had before. */
const pickable = (f: ComponentFixture<ProvidersView>) =>
  pickCards(f)
    .filter((c) => c.querySelector('button[data-act="add-pool"]'))
    .map((c) => c.getAttribute('data-pair'));

const submitBtn = (f: ComponentFixture<ProvidersView>) =>
  f.nativeElement.querySelector('form button[type=submit]') as HTMLButtonElement;

/** The endpoints are stickers, pressed one at a time: "which endpoint is this key for" is a click
    on the mark the user recognises, not a line in a select he has to read to confirm. */
const stickers = (f: ComponentFixture<ProvidersView>) =>
  Array.from(f.nativeElement.querySelectorAll('button[data-act="pick-endpoint"]')) as HTMLButtonElement[];

const pressedSticker = (f: ComponentFixture<ProvidersView>) =>
  stickers(f).find((b) => b.getAttribute('aria-pressed') === 'true');

const addKeyBtn = (f: ComponentFixture<ProvidersView>) =>
  f.nativeElement.querySelector('button[data-act="add-key"]') as HTMLButtonElement;

/** Open the key form the way a user does, and settle the page afterwards. */
async function openForm(f: ComponentFixture<ProvidersView>) {
  addKeyBtn(f).click();
  f.detectChanges();
  await f.whenStable();
  f.detectChanges();
}

/** jsdom has no DragEvent, and the handlers only need preventDefault + an optional
    dataTransfer, so a plain Event exercises the same code path. */
function drag(f: ComponentFixture<ProvidersView>, from: number, to: number) {
  const r = rows(f);
  r[from].dispatchEvent(new Event('dragstart', { bubbles: true }));
  f.detectChanges();
  r[to].dispatchEvent(new Event('dragover', { bubbles: true }));
  f.detectChanges();
  r[to].dispatchEvent(new Event('drop', { bubbles: true }));
  f.detectChanges();
}

describe('ProvidersView add-a-key form', () => {
  const keyInput = (f: ComponentFixture<ProvidersView>) =>
    f.nativeElement.querySelector('input[type=password]') as HTMLInputElement;

  it('draws the endpoints as big side-by-side stickers and keeps the form shut until asked', async () => {
    // The whole surface used to be an always-open form: a page about keys opened with three empty
    // fields and a select nobody had read yet. Now the endpoints are the first thing — marks the
    // user already knows — and the fields appear when he says he is adding one.
    const { fixture } = await build();
    expect(stickers(fixture).length).toBe(2);
    expect(pressedSticker(fixture)?.getAttribute('data-provider')).toBe('nvidia');
    expect(addKeyBtn(fixture).textContent).toContain('Add new key');
    expect(keyInput(fixture), 'no key field before the user asks for one').toBeFalsy();
  });

  it('opens a form that asks for the key, a name for it, and what to verify against', async () => {
    const { fixture } = await build();
    await openForm(fixture);
    expect(keyInput(fixture)).toBeTruthy();
    expect(fixture.nativeElement.querySelector('input[name=label]')).toBeTruthy();
    expect(fixture.nativeElement.querySelector('input[name=model]')).toBeTruthy();
    // A common endpoint is one URL the deployment already knows. Asking for an address here would
    // be offering an override the backend refuses for a provider that is not self-hosted.
    expect(fixture.nativeElement.querySelector('input[name=host]')).toBeFalsy();
    expect(fixture.nativeElement.querySelector('input[name=port]')).toBeFalsy();
  });

  it('chooses the endpoint whose sticker was pressed', async () => {
    const { fixture } = await build();
    stickers(fixture)[1].click();
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(pressedSticker(fixture)?.getAttribute('data-provider')).toBe('openrouter');
    expect(pressedSticker(fixture)?.textContent).toContain('OpenRouter');
  });

  it('enables "Save key" as soon as the user types a key', async () => {
    const { fixture } = await build();
    await openForm(fixture);
    await type(fixture, 'input[type=password]', '  sk-abcdefgh12345  ');
    // the defect the original file exists for: this line was false in the built app on 2026-10-16
    expect(submitBtn(fixture).disabled).toBe(false);
  });

  it('sends the trimmed key, the sticker he chose and the name he gave the key', async () => {
    const { fixture, mocks } = await build();
    await openForm(fixture);
    await type(fixture, 'input[type=password]', '  sk-abcdefgh12345  ');
    await type(fixture, 'input[name=label]', 'work laptop');
    submitBtn(fixture).click();
    await fixture.whenStable();
    fixture.detectChanges();

    expect(mocks.addMyKey.mock.calls.length).toBe(1);
    expect(mocks.addMyKey.mock.calls[0][0]).toEqual({
      provider_id: 1,
      api_key: 'sk-abcdefgh12345',
      label: 'work laptop',
      model_id: undefined,
    });
    expect('base_url' in mocks.addMyKey.mock.calls[0][0]).toBe(false);
    expect(keyInput(fixture)).toBeFalsy();
  });

  it('verifies the key against the model the user names', async () => {
    const { fixture, mocks } = await build({ keyReply: of({ id: 7, verified: true }) });
    await openForm(fixture);
    await type(fixture, 'input[type=password]', 'sk-abcdefgh12345');
    await type(fixture, 'input[name=model]', 'z-ai/glm-5.3-flash');
    submitBtn(fixture).click();
    await fixture.whenStable();

    expect(mocks.addMyKey.mock.calls[0][0].model_id).toBe('z-ai/glm-5.3-flash');
    const status = fixture.nativeElement.querySelector('.doc-status');
    expect(status.textContent).toContain('verified working');
    expect(status.classList.contains('bad')).toBe(false);
  });

  it('renders the backend rejection instead of failing silently', async () => {
    const message = 'api_key must be 1-512 chars of [A-Za-z0-9._:+/-]';
    const { fixture, mocks } = await build({
      keyReply: throwError(() => new HttpErrorResponse({ status: 400, error: { error: message } })),
    });
    await openForm(fixture);
    await type(fixture, 'input[type=password]', 'bad key !!!');
    submitBtn(fixture).click();
    await fixture.whenStable();
    fixture.detectChanges();

    const status = fixture.nativeElement.querySelector('.doc-status');
    expect(status).toBeTruthy();
    expect(status.textContent.trim()).toBe(message);
    expect(status.classList.contains('bad')).toBe(true);
    expect(mocks.addMyKey.mock.calls.length).toBe(1);
    // a failed save leaves the form open and usable rather than stuck on "Verifying…" or — worse —
    // closed with his paste thrown away
    expect(keyInput(fixture)).toBeTruthy();
    expect(submitBtn(fixture).disabled).toBe(false);
  });

  it('refuses to submit whitespace as a key', async () => {
    const { fixture, mocks } = await build();
    await openForm(fixture);
    await type(fixture, 'input[type=password]', '   ');
    expect(submitBtn(fixture).disabled).toBe(true);
    submitBtn(fixture).click();
    await fixture.whenStable();
    expect(mocks.addMyKey.mock.calls.length).toBe(0);
  });

  it('gives a self-hosted gateway the address and port fields it has no other way to get', async () => {
    const { fixture } = await build({
      config: { providers: [...(CONFIG as any).providers, OMNI] } as any,
    });
    stickers(fixture)[2].click();
    fixture.detectChanges();
    await openForm(fixture);

    const host = fixture.nativeElement.querySelector('input[name=host]') as HTMLInputElement;
    const port = fixture.nativeElement.querySelector('input[name=port]') as HTMLInputElement;
    expect(host.value).toBe('127.0.0.1');
    expect(port.value).toBe('20128');
  });

  it('sends the address he typed as that key\'s endpoint', async () => {
    const { fixture, mocks } = await build({
      config: { providers: [...(CONFIG as any).providers, OMNI] } as any,
    });
    stickers(fixture)[2].click();
    fixture.detectChanges();
    await openForm(fixture);
    await type(fixture, 'input[type=password]', 'sk-router-123456');
    await type(fixture, 'input[name=port]', '20500');
    submitBtn(fixture).click();
    await fixture.whenStable();

    expect(mocks.addMyKey.mock.calls[0][0]).toEqual({
      provider_id: 3,
      api_key: 'sk-router-123456',
      label: undefined,
      model_id: undefined,
      base_url: 'http://127.0.0.1:20500/v1',
    });
  });

  it('sends no endpoint when he left the address the row already carries', async () => {
    // An override that repeats the seed is not an override: storing it would mean a later change
    // to the provider row silently stops applying to this key.
    const { fixture, mocks } = await build({
      config: { providers: [...(CONFIG as any).providers, OMNI] } as any,
    });
    stickers(fixture)[2].click();
    fixture.detectChanges();
    await openForm(fixture);
    await type(fixture, 'input[type=password]', 'sk-router-123456');
    submitBtn(fixture).click();
    await fixture.whenStable();

    expect('base_url' in mocks.addMyKey.mock.calls[0][0]).toBe(false);
  });

  it('shows a stored key with its hint and never the plaintext', async () => {
    const { fixture } = await build({ keys: [storedKey] });
    const row = fixture.nativeElement.querySelector('.key-row') as HTMLElement;
    expect(row.textContent).toContain('nvapi-…w9');
    expect(row.textContent).toContain('NVIDIA NIM');
    expect(row.querySelector('.chip.good')?.textContent).toContain('verified');
  });

  it('removes the key the row names', async () => {
    const { fixture, mocks } = await build({ keys: [storedKey] });
    const btn = fixture.nativeElement.querySelector('.key-row button') as HTMLButtonElement;
    expect(btn.getAttribute('aria-label')).toContain('NVIDIA NIM');
    btn.click();
    await fixture.whenStable();
    expect(mocks.deleteMyKey.mock.calls[0]).toEqual([4]);
  });

  it('lists the guides, so the page says where a key comes from', async () => {
    const { fixture } = await build({
      config: {
        api_key_guides: [
          { provider: 'nvidia', title: 'Get an NVIDIA key', url: 'https://build.nvidia.com',
            steps: 'Sign up, copy the nvapi key.' },
        ],
      },
    });
    const guide = fixture.nativeElement.querySelector('.guide') as HTMLElement;
    expect(guide, 'a user with no key has to be told where to get one').toBeTruthy();
    expect(guide.textContent).toContain('Get an NVIDIA key');
    expect(guide.querySelector('a')?.getAttribute('href')).toBe('https://build.nvidia.com');
  });
});

/**
 * The way in from a locked ranking card.
 *
 * A card with no key behind it cannot host a key form any more, so its button brings the user here
 * for THAT endpoint. Two things have to land: the endpoint is the pressed sticker, and the form is
 * open — a closed form preselected on the right endpoint is still a page where he has to work out
 * what he was sent here to do. Asserted on the DOM the user sees, not on a component field.
 */
describe('ProvidersView endpoint preselect', () => {
  it('defaults to the first endpoint when nothing asked for one', async () => {
    const { fixture } = await build();
    expect(pressedSticker(fixture)?.getAttribute('data-provider')).toBe('nvidia');
  });

  it('opens on the endpoint a ranking card asked for, with the form already open', async () => {
    const { fixture } = await build({ focusProviderId: 2 });
    expect(pressedSticker(fixture)?.getAttribute('data-provider')).toBe('openrouter');
    expect(fixture.nativeElement.querySelector('input[type=password]')).toBeTruthy();
  });
});

/**
 * The model a ranking card sent him here to find.
 *
 * "Add to using pool" on a rank card is a ROUTE, not a write: this page owns the pool, so the card
 * arrives, the picker scrolls to that model's own card, and the frame lights in accent yellow — the
 * colour this app spends on "this is the answer", and the one colour the user is told means "the one
 * you came for". Two properties the specs insist on: the highlight lands on the row the pair names
 * and on no other (a page that lights several frames says nothing about which model he asked for),
 * and a model with no card here is named as missing rather than left to look like a broken link —
 * `/ranking` is a measurement of whatever answered, so it can hold a model no `models` row exists for.
 *
 * jsdom has no layout, so `scrollIntoView` does not exist at all: the shim below records WHICH
 * element was scrolled to, which is the assertion that matters, and a real browser is where the
 * centring itself is seen.
 */
describe('ProvidersView focus from a ranking card', () => {
  let scrolled: string[];

  beforeEach(() => {
    scrolled = [];
    (HTMLElement.prototype as any).scrollIntoView = function (this: HTMLElement) {
      scrolled.push(this.getAttribute('data-pair') ?? this.className);
    };
  });

  const GLM = 'nvidia|z-ai/glm-5.3-flash';

  it('lights the frame of exactly the card it was sent to, and scrolls to it', async () => {
    const { fixture } = await build({
      options: [option({}), option({ model: 'm/other' })],
      focusPair: GLM,
    });
    expect(pickCard(fixture, GLM).classList.contains('neon')).toBe(true);
    expect(pickCard(fixture, 'nvidia|m/other').classList.contains('neon'), 'one frame, one answer')
      .toBe(false);
    expect(scrolled).toEqual([GLM]);
  });

  it('lights a model already in his pool too — coming from a framed rank card, this is the way to find it', async () => {
    const { fixture } = await build({
      options: [option({ model: 'm/held', in_pool: true, your_rank: 1 })],
      focusPair: 'nvidia|m/held',
    });
    const card = pickCard(fixture, 'nvidia|m/held');
    expect(card.classList.contains('in-pool'), 'the pool state is still stated').toBe(true);
    expect(card.classList.contains('neon'), 'and so is the reason he is here').toBe(true);
    expect(card.textContent).toContain('in your pool');
  });

  it('lights a card he cannot pay for yet, because "this is the one" is still the answer', async () => {
    const { fixture } = await build({
      options: [option({ model: 'm/nokey', verified_key: false, admin_key: false, needs_key: true })],
      focusPair: 'nvidia|m/nokey',
    });
    const card = pickCard(fixture, 'nvidia|m/nokey');
    expect(card.classList.contains('needs-key')).toBe(true);
    expect(card.classList.contains('neon')).toBe(true);
  });

  it('names the model that has no card here instead of leaving a silent jump', async () => {
    // The ranking's table and the picker's list are not the same set: a measured model with no
    // `models` row has a rank and no card. Landing on a page where nothing moved would read as the
    // app failing, which is the inference this page refuses to invite about models.
    const { fixture } = await build({
      options: [option({ model: 'm/held' })],
      focusPair: 'nvidia|z-ai/glm-5.3-flash',
    });
    const note = fixture.nativeElement.querySelector('.picker-focus-note') as HTMLElement;
    expect(note, 'the route has to say when it found nothing').toBeTruthy();
    expect(note.textContent).toContain('z-ai/glm-5.3-flash');
    expect(pickCards(fixture).every((c) => !c.classList.contains('neon'))).toBe(true);
    expect(scrolled, 'nothing to scroll to').toEqual([]);
  });

  it('adds no frame and no note when no card asked for one', async () => {
    const { fixture } = await build({ options: [option({})] });
    expect(pickCards(fixture).every((c) => !c.classList.contains('neon'))).toBe(true);
    expect(fixture.nativeElement.querySelector('.picker-focus-note')).toBe(null);
    expect(scrolled).toEqual([]);
  });

  it('does not confuse two endpoints that hold the same model id', async () => {
    const { fixture } = await build({
      options: [
        option({ model: 'shared/model' }),
        option({ model: 'shared/model', provider: 'openrouter', provider_id: 2, host: 'openrouter.ai' }),
      ],
      focusPair: 'openrouter|shared/model',
    });
    const lit = pickCards(fixture).filter((c) => c.classList.contains('neon'));
    expect(lit.length).toBe(1);
    expect(lit[0].getAttribute('data-pair')).toBe('openrouter|shared/model');
  });
});

describe('ProvidersView using pool', () => {
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

  it('adds a model from its own picker, which offers what he can pay for — slow ones included', async () => {
    const { fixture, mocks } = await build({
      options: [
        option({}),
        option({ model: 'm/held', in_pool: true, your_rank: 1 }),
        option({ model: 'm/nokey', verified_key: false, admin_key: false, needs_key: true }),
        // The row this page exists for: no measurement at all, because its probe ran out of time
        // twice. Being slow is not being gone, so it must be in the list.
        option({ model: 'm/slow', measured: false, probe_status: 'slow', answer_ms: null }),
        option({ model: 'm/refused', probe_status: 'declined' }),
      ],
      pool: [entry({ model: 'm/held' })],
    });
    const values = pickable(fixture);
    const listed = pickCards(fixture).map((c) => c.getAttribute('data-pair'));
    expect(listed, 'the menu is a list of its own, not a dropdown nobody can read at a glance')
      .toContain('nvidia|z-ai/glm-5.3-flash');
    expect(values).toContain('nvidia|z-ai/glm-5.3-flash');
    expect(listed, 'a model already in the pool is still on the menu — it is the frame that changes')
      .toContain('nvidia|m/held');
    expect(values, 'a model already in the pool cannot be added twice').not.toContain('nvidia|m/held');
    expect(listed, 'a model with no key behind it is listed, with the reason on the card')
      .toContain('nvidia|m/nokey');
    expect(values, 'a model with no key behind it cannot be run').not.toContain('nvidia|m/nokey');
    expect(values, 'a probe that timed out does not retire the model').toContain('nvidia|m/slow');
    expect(values, 'a refusal is a not-now, not a deletion').toContain('nvidia|m/refused');

    const slow = pickCard(fixture, 'nvidia|m/slow');
    expect(slow.textContent, 'and the card says why it has no score').toContain('gets asked again');
    expect(slow.textContent).toContain('never measured');

    const glm = pickCard(fixture, 'nvidia|z-ai/glm-5.3-flash');
    (glm.querySelector('button[data-act=add-pool]') as HTMLButtonElement).click();
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
    const { fixture, mocks } = await build({ keys: [storedKey] });
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
      keys: [storedKey],
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
    const { fixture } = await build({ signedIn: false, pool: [entry({})] });
    expect(fixture.nativeElement.querySelector('.pool-panel')).toBe(null);
  });
});

/**
 * The three states of one model, and the frame that tells them apart.
 *
 * The menu used to be a `<select>` whose options were a sentence each, so the only state it could
 * show was "addable" — a model he already held simply vanished from the list, and a model with no
 * key behind it vanished the same way. He could not tell "you already have this" from "this one is
 * gone", which is the exact confusion the timeout rule exists to prevent. Now every row is a card,
 * and the two cannot-states get their own frame instead of their own absence: held models are
 * outlined and carry his rank, unpayable ones carry the endpoint's key button.
 */
describe('ProvidersView add-models menu', () => {
  const mix = [
    option({}),
    option({ model: 'm/held', in_pool: true, your_rank: 2 }),
    option({ provider: 'openrouter', provider_id: 2, provider_name: 'openrouter',
             model: 'm/or-nokey', needs_key: true, verified_key: false, admin_key: false }),
  ];

  it('frames a model he already holds and gives him his rank, not a second Add', async () => {
    const { fixture } = await build({ options: mix, pool: [entry({ model: 'm/held', your_rank: 2 })] });
    const held = pickCard(fixture, 'nvidia|m/held');
    expect(held, 'it is on the menu, so he can see it is his rather than wonder where it went')
      .toBeTruthy();
    expect(held.classList.contains('in-pool')).toBe(true);
    expect(held.textContent).toContain('#2');
    expect(held.querySelector('button[data-act="add-pool"]')).toBe(null);
    // and the one he does not hold carries no frame, so the outline means something
    expect(pickCard(fixture, 'nvidia|z-ai/glm-5.3-flash').classList.contains('in-pool')).toBe(false);
  });

  it('frames a model with no key and points at the endpoint that needs one', async () => {
    const { fixture } = await build({ options: mix });
    const locked = pickCard(fixture, 'openrouter|m/or-nokey');
    expect(locked.classList.contains('needs-key')).toBe(true);
    expect(locked.querySelector('button[data-act="add-pool"]'), 'no key means no run, so no Add')
      .toBe(null);
    const group = locked.closest('.pick-group') as HTMLElement;
    const cta = group.querySelector('button[data-act="get-key"]') as HTMLButtonElement;
    expect(cta.textContent).toContain('OpenRouter');
  });

  it('names each endpoint the way the keys page names it, not by the slug on the row', async () => {
    // Live on the packaged app, 2026-10-20: `/my/pool/options` fills `provider_name` from the same
    // column as `provider`, so a menu that trusted it read "Get a key for openrouter" one scroll
    // away from a keys page that says "OpenRouter". The name has to come from `/config`, which is
    // the source the keys page itself renders from.
    const { fixture } = await build({ options: mix });
    const heads = (Array.from(fixture.nativeElement.querySelectorAll('.pick-ghead')) as HTMLElement[])
      .map((h) => h.textContent?.replace(/\s+/g, ' ').trim());
    expect(heads[0]).toContain('NVIDIA NIM');
    expect(heads[1]).toContain('OpenRouter');
    expect(pickCard(fixture, 'openrouter|m/or-nokey').textContent)
      .toContain('needs a key for OpenRouter');
  });

  it('opens the key form on the endpoint the card named', async () => {
    const { fixture } = await build({ options: mix });
    const cta = fixture.nativeElement.querySelector('button[data-act="get-key"]') as HTMLButtonElement;
    cta.click();
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(pressedSticker(fixture)?.getAttribute('data-provider'),
           'the handoff a locked card started has to finish here').toBe('openrouter');
    expect(fixture.nativeElement.querySelector('input[type=password]')).toBeTruthy();
    const status = fixture.nativeElement.querySelector('.doc-status') as HTMLElement;
    expect(status.textContent).toContain('OpenRouter');
  });

  it('lights an endpoint mark only where a key can actually pay', async () => {
    const { fixture } = await build({ options: mix });
    const lit = (Array.from(fixture.nativeElement.querySelectorAll('.pick-group')) as HTMLElement[])
      .map((g) => [
        g.getAttribute('data-provider'),
        g.querySelector('.mark')?.classList.contains('lit'),
      ]);
    expect(lit).toEqual([['nvidia', true], ['openrouter', false]]);
  });

  it('auto-ranks the pool on the measurement and says what it could not rank', async () => {
    const { fixture, mocks } = await build({
      pool: [entry({ model: 'm/noneed', your_rank: 1 }), entry({ model: 'm/best', your_rank: 2 })],
      autoRank: of({
        pool: [entry({ model: 'm/best', your_rank: 1, position: 1 }),
               entry({ model: 'm/noneed', your_rank: 2, position: 2 })],
        ranked: 1, unranked: 1,
        note: 'ordered by the Ranking table: 1 ranked best first, 1 left at the back',
      }),
    });
    const btn = fixture.nativeElement.querySelector('button[data-act="auto-rank"]') as HTMLButtonElement;
    expect(btn).toBeTruthy();
    btn.click();
    await fixture.whenStable();
    fixture.detectChanges();

    expect(mocks.autoRankPool.mock.calls.length).toBe(1);
    expect(rows(fixture).length).toBe(2);
    expect(rows(fixture)[0].textContent, 'the measured model is served first now').toContain('m/best');
    const status = fixture.nativeElement.querySelector('.pool-panel .doc-status');
    expect(status.textContent).toContain('1 ranked best first');
    expect(status.classList.contains('bad'), 'an unrankable model is a fact, not a failure')
      .toBe(false);
  });

  it('keeps the order it was given when the auto-rank answers that nothing is measured', async () => {
    const { fixture, mocks } = await build({
      pool: [entry({ model: 'm/a' })],
      autoRank: of({ pool: [entry({ model: 'm/a' })], ranked: 0, unranked: 1,
                     note: 'nothing in your pool has a measurement to rank on: no measurement has '
                           + 'been captured for these models yet.' }),
    });
    (fixture.nativeElement.querySelector('button[data-act="auto-rank"]') as HTMLButtonElement).click();
    await fixture.whenStable();
    fixture.detectChanges();
    expect(mocks.setPoolOrder.mock.calls.length, 'a no-op must not write an order twice').toBe(0);
    expect(rows(fixture)[0].textContent).toContain('m/a');
    expect(fixture.nativeElement.querySelector('.pool-panel .doc-status').textContent)
      .toContain('no measurement');
  });
});

/**
 * Changing which model holds a slot — one atomic call, and an order written in the user's ranks.
 *
 * Add-then-remove-then-re-drag costs the user his slot between the calls, and the panel promises a
 * demoted model comes back where he put it. So a change is one call, and an order write is
 * expressed in `your_rank` space, not in the health-served order it is drawn in.
 */
describe('ProvidersView pool edit', () => {
  it('opens an editor on the row that offers only models he can pay for and not one he already holds', async () => {
    const { fixture } = await build({
      pool: [entry({ model: 'm/a' })],
      options: [
        option({}),
        option({ model: 'm/a', verified_key: true, in_pool: true, your_rank: 1 }),
        option({ model: 'm/nokey', verified_key: false, admin_key: false, needs_key: true }),
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
    const { fixture, mocks } = await build({ pool: [entry({ model: 'm/a' })], options: [option({})] });
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
      options: [option({})],
      replace: throwError(
        () => new HttpErrorResponse({ status: 400, error: { error: 'that model is already in your pool' } }),
      ),
    });
    (rows(fixture)[0].querySelector('button[data-act="change"]') as HTMLButtonElement).click();
    fixture.detectChanges();
    const select = rows(fixture)[0].querySelector('select.pool-change-target') as HTMLSelectElement;
    select.value = 'nvidia|z-ai/glm-5.3-flash';
    select.dispatchEvent(new Event('change', { bubbles: true }));
    // Flush before clicking, exactly as the successful spec does: choosing a model is what enables
    // "save", and a zoneless app only re-checks that binding when the scheduler runs.
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
    const { fixture, mocks } = await build({ pool: [entry({ model: 'm/a' })], options: [option({})] });
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

/**
 * The short accept-or-refuse window on a model that only answered after a retry.
 *
 * The rule the backend now enforces is that a timeout never writes a model off and never pins it
 * either: the job records the row as `pending` and the user gets a bounded window to say yes or no.
 * These are the two halves of that promise the page itself can break — a window that does not
 * actually count down (or that expires into a silent pin), and a `no` that costs the model its
 * place in the picker.
 *
 * The countdown is arithmetic on the seconds the backend said were left when the list was read, so
 * these specs move the component's own clock rather than waiting a real ten minutes for one.
 */
describe('ProvidersView accept-or-refuse window', () => {
  const cards = (f: ComponentFixture<ProvidersView>) =>
    Array.from(f.nativeElement.querySelectorAll('.offer-row')) as HTMLElement[];
  const act = (f: ComponentFixture<ProvidersView>, name: string) =>
    f.nativeElement.querySelector(`button[data-act="${name}-offer"]`) as HTMLButtonElement;
  /** Advance the page's clock, the way the one-second timer does. */
  const elapse = (f: ComponentFixture<ProvidersView>, seconds: number) => {
    const c: any = f.componentInstance;
    for (let i = 0; i < seconds; i++) c.tick.update((t: number) => t + 1);
    c.optionsAt.update((at: number) => at - seconds * 1000);
    f.detectChanges();
  };

  it('shows a timed-out-then-answered model as an offer with its window, still pickable', async () => {
    const { fixture } = await build({ options: [offered({}), option({})] });
    const card = cards(fixture)[0];
    expect(card, 'one card, for the one row the probe left waiting').toBeTruthy();
    expect(card.textContent).toContain('m/recovered');
    expect(card.textContent, "the engine's own words about why this is a question at all")
      .toContain('answered on retry');
    expect(card.textContent).toContain('10:00');
    expect(act(fixture, 'accept').disabled).toBe(false);
    expect(act(fixture, 'refuse').disabled).toBe(false);
    expect(pickable(fixture), 'an offer is not the only way in: the row is pickable anyway')
      .toContain('nvidia|m/recovered');
  });

  it('counts the window down without another request', async () => {
    const { fixture } = await build({ options: [offered({ offer_seconds_left: 300 })] });
    expect(cards(fixture)[0].textContent).toContain('5:00');
    elapse(fixture, 130);
    expect(cards(fixture)[0].textContent).toContain('2:50');
  });

  it('lets an unread window expire into nothing: no pin, no veto, still pickable', async () => {
    const { fixture, mocks } = await build({ options: [offered({ offer_seconds_left: 30 })] });
    elapse(fixture, 31);
    expect(cards(fixture).length, 'silence is not consent, and it is not a refusal either').toBe(0);
    expect(mocks.catalogDecision.mock.calls.length, 'nobody answered, so nothing was written').toBe(0);
    expect(mocks.addToPool.mock.calls.length).toBe(0);
    expect(pickable(fixture)).toContain('nvidia|m/recovered');
  });

  it('accepting an offer adds it as the user\'s own choice and ends the offer', async () => {
    const opts: any = { options: [offered({})] };
    const { fixture, mocks } = await build(opts);
    // What the read-back after the decision will see: the row is `usable` now, so the offer is over.
    opts.options = [option({ model: 'm/recovered', measured: false, probe_status: 'usable' })];
    expect(act(fixture, 'accept').getAttribute('aria-label')).toContain('m/recovered');
    act(fixture, 'accept').click();
    await fixture.whenStable();
    fixture.detectChanges();

    expect(mocks.catalogDecision.mock.calls[0]).toEqual(['nvidia', 'm/recovered', 'accept']);
    expect(mocks.addToPool.mock.calls.length, 'the decision route adds it, not the old picker call')
      .toBe(0);
    expect(cards(fixture).length).toBe(0);
    const status = fixture.nativeElement.querySelector('.pool-panel .doc-status');
    expect(status.textContent).toContain('m/recovered is in your pool');
    expect(status.classList.contains('bad')).toBe(false);
  });

  it('refusing keeps the model pickable and never touches the pool', async () => {
    const opts: any = { options: [offered({})] };
    const { fixture, mocks } = await build(opts);
    opts.options = [option({ model: 'm/recovered', measured: false, probe_status: 'declined' })];
    act(fixture, 'refuse').click();
    await fixture.whenStable();
    fixture.detectChanges();

    expect(mocks.catalogDecision.mock.calls[0]).toEqual(['nvidia', 'm/recovered', 'refuse']);
    expect(mocks.addToPool.mock.calls.length).toBe(0);
    expect(cards(fixture).length).toBe(0);
    const status = fixture.nativeElement.querySelector('.pool-panel .doc-status');
    expect(status.textContent).toContain('not in your pool');
    expect(status.classList.contains('bad'), 'a refusal is the user saying no, not a failure')
      .toBe(false);
    expect(pickable(fixture), 'a no today is not a deletion').toContain('nvidia|m/recovered');
  });

  it('reports a refused decision instead of looking like it worked', async () => {
    const { fixture, mocks } = await build({
      options: [offered({})],
      decision: throwError(
        () => new HttpErrorResponse({ status: 404, error: { error: 'no probe on that endpoint to decide about' } }),
      ),
    });
    act(fixture, 'accept').click();
    await fixture.whenStable();
    fixture.detectChanges();

    expect(mocks.addToPool.mock.calls.length).toBe(0);
    const status = fixture.nativeElement.querySelector('.pool-panel .doc-status');
    expect(status.textContent).toContain('no probe on that endpoint');
    expect(status.classList.contains('bad')).toBe(true);
    expect(act(fixture, 'accept').disabled, 'the card is usable again, not stuck mid-decision')
      .toBe(false);
  });

  it('draws no card for a row the backend did not offer', async () => {
    // The card follows `offered` plus a live window, never the status string: a `slow` row is
    // pickable and waiting for the next run, not waiting on the user. A `dead` model cannot even
    // appear here, because the probe wrote no `models` row for it — that is the one and only
    // place a timeout is allowed to remove a model, and it is the backend's decision, not this one.
    const { fixture } = await build({
      options: [
        option({}),
        option({ model: 'm/slow', measured: false, probe_status: 'slow', answer_ms: null }),
      ],
    });
    expect(cards(fixture).length).toBe(0);
    expect(pickable(fixture)).toContain('nvidia|m/slow');
  });
});
