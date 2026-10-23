import { Component } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { of } from 'rxjs';

import { RankingView } from './ranking-view';
import { BackendService } from '../../core/backend.service';
import { AuthService } from '../../core/auth.service';
import { DetectionService } from '../../core/detection.service';
import type { PublicConfig, RankedModel, RankingResponse } from '../../core/backend.types';

/**
 * Cover for the ranking page AFTER the split: it measures, explains, and points.
 *
 * Everything that controls models and endpoints — BYOK keys, the key guides, the using pool,
 * discovery — lives in `ProvidersView`. A card carries the pick, a Test that performs the very same
 * pick (the user's call: a wake-check nobody could read as an action was worth less than a second
 * way to say "score my next run with this one"), and the route to the pool menu for that exact
 * model. When the user cannot pay for an endpoint at all, the only action left is the way to get a
 * key for *that* provider rather than a paragraph saying he can't.
 *
 * The split is asserted at the wire, not just in the markup: the stubbed BackendService answers
 * only `config` and `ranking`, and every other method — `probeMyModel` included, since Test no
 * longer spends a call — THROWS. So a leftover `myKeys()` in ngOnInit or a resurrected probe fails
 * loudly with its reason instead of quietly passing on a stub that returned an empty array, which
 * is exactly how a half-finished change would hide.
 *
 * Specs drive the DOM, per this file's existing discipline: the app is zoneless, so poking a
 * component property proves nothing about what a user sees.
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

/** A host, so an emitted intent is asserted the way the shell really binds it
    (`<app-ranking-view (getKeys)="…">`) rather than by poking the child's EventEmitter. */
@Component({
  selector: 'app-host-ranking',
  standalone: true,
  imports: [RankingView],
  template: `
    <app-ranking-view
      (useModel)="used = $event?.model"
      (clearModel)="released = true"
      (getKeys)="askedKey = $event"
      (openPool)="poolPair = $event?.provider + '|' + $event?.model" />
  `,
})
class HostRanking {
  used: string | null = null;
  released = false;
  /** `undefined` = never emitted, `null` = "no particular endpoint", a number = preselect that one. */
  askedKey: number | null | undefined = undefined;
  /** The "provider|model" pair the keys-and-models surface is asked to land on. */
  poolPair: string | null = null;
}

const card = (over: any = {}): RankedModel =>
  ({
    model: 'z-ai/glm-5.3-flash',
    tag: 'glm',
    rank: 1,
    accuracy: 0.94,
    lane: null,
    provider: 'nvidia',
    provider_id: 1,
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

/** The whole page's data contract: two reads and nothing else. `probeMyModel` is off-limits now
    that Test is a pick rather than a call, so a resurrected probe fails with its reason. */
function buildBackend(ranked: RankedModel[], authority?: any) {
  const offLimits = (name: string) =>
    vi.fn(() => {
      throw new Error(
        `RankingView called ${name}(): controlling models and endpoints is ProvidersView's job now`,
      );
    });
  return {
    config: () => of(CONFIG),
    ranking: () => of({ ranked, lane_authority: authority } as unknown as RankingResponse),
    probeMyModel: offLimits('probeMyModel'),
    myKeys: offLimits('myKeys'),
    addMyKey: offLimits('addMyKey'),
    deleteMyKey: offLimits('deleteMyKey'),
    myPool: offLimits('myPool'),
    addToPool: offLimits('addToPool'),
    removeFromPool: offLimits('removeFromPool'),
    setPoolOrder: offLimits('setPoolOrder'),
    replaceInPool: offLimits('replaceInPool'),
    discover: offLimits('discover'),
    discoveryStatus: offLimits('discoveryStatus'),
  };
}

async function build(
  opts: {
    ranked?: RankedModel[];
    authority?: any;
    picked?: string | null;
    signedIn?: boolean;
    local?: boolean;
    config?: Partial<PublicConfig>;
  } = {},
) {
  const backend = buildBackend(opts.ranked ?? [], opts.authority);
  if (opts.config) {
    const over = opts.config;
    backend.config = () => of({ ...CONFIG, ...over } as unknown as PublicConfig);
  }
  const signedIn = opts.signedIn !== false;
  await TestBed.configureTestingModule({
    imports: [HostRanking, RankingView],
    providers: [
      { provide: BackendService, useValue: backend },
      {
        provide: AuthService,
        useValue: {
          isAdmin: () => false,
          local: () => opts.local === true,
          user: () => (signedIn ? { role: 'user', username: 'nadia' } : null),
        },
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

const cards = (f: ComponentFixture<HostRanking>) =>
  Array.from(f.nativeElement.querySelectorAll('.rank-card')) as HTMLElement[];

const chipLabels = (card: HTMLElement) =>
  Array.from(card.querySelectorAll('.chip')).map((c) => c.textContent?.trim());

const buttonsIn = (el: HTMLElement) =>
  Array.from(el.querySelectorAll('button')) as HTMLButtonElement[];

/** The card's test affordance, found by what it is for rather than by its position: a card can
    carry a pick, a test and — on a locked one — a key route, and a `buttonsIn(el)[1]` would follow
    whichever of those happens to be rendered. */
const testBtnIn = (el: HTMLElement) =>
  el.querySelector('button[data-act="test"]') as HTMLButtonElement | null;

const markIn = (el: HTMLElement) => el.querySelector('.mark') as HTMLElement | null;

const poolBtnIn = (el: HTMLElement) =>
  el.querySelector('button[data-act="open-pool"]') as HTMLButtonElement | null;

describe('RankingView card actions', () => {
  it('reads only the ranking and the config, and never the model-control calls', async () => {
    const f = await build({ ranked: [card()] });
    expect(cards(f).length).toBe(1);
    const backend = TestBed.inject(BackendService) as any;
    for (const name of ['myKeys', 'myPool', 'discoveryStatus', 'addToPool', 'discover']) {
      expect(backend[name], `RankingView must not touch ${name}()`).not.toHaveBeenCalled();
    }
  });

  it('points with the pick button, which is the one that shows what is pinned', async () => {
    const f = await build({ ranked: [card()] });
    const el = cards(f)[0];
    const picks = el.querySelectorAll('button.pick-btn');
    expect(picks.length, 'one card, one pinned-state control').toBe(1);
    expect(picks[0].textContent).toContain('Score with this model');
    (picks[0] as HTMLButtonElement).click();
    f.detectChanges();
    expect(f.componentInstance.used).toBe('z-ai/glm-5.3-flash');
  });

  it('shows the pinned state on the pick button, not on the test', async () => {
    const f = await build({ ranked: [card()], picked: 'z-ai/glm-5.3-flash' });
    const pick = cards(f)[0].querySelector('button.pick-btn') as HTMLButtonElement;
    expect(pick.classList.contains('active')).toBe(true);
    expect(pick.textContent).toContain('switch back');
    expect(testBtnIn(cards(f)[0])!.classList.contains('active'), 'a short label cannot also be a toggle')
      .toBe(false);
  });

  it('routes to the model\'s own card in the pool menu, and edits nothing from here', async () => {
    const f = await build({ ranked: [card()] });
    const el = cards(f)[0];
    // The route is the whole control: adding is one click on that card once the user is there, and
    // the menu — not this table — knows what the pool already holds.
    expect(el.querySelector('.pool-actions'), 'no pool order or membership control lives here').toBe(null);
    expect(el.querySelector('button[data-act="add-pool"]'), 'the writer stays on the other page').toBe(null);
    const btn = poolBtnIn(el);
    expect(btn, 'every pickable card has to be able to send him to the pool').toBeTruthy();
    expect(btn!.textContent).toContain('Add to using pool');
    btn!.click();
    expect(f.componentInstance.poolPair).toBe('nvidia|z-ai/glm-5.3-flash');
    expect(f.componentInstance.used, 'a route is not a pick').toBe(null);
    const backend = TestBed.inject(BackendService) as any;
    expect(backend.addToPool).not.toHaveBeenCalled();
  });

  it('offers the same route on a model he already holds, because it is a way to find it', async () => {
    const f = await build({ ranked: [card({ in_pool: true, your_rank: 2 })] });
    const btn = poolBtnIn(cards(f)[0]);
    expect(btn, 'an in-pool card has nothing to add, but he still has to be able to get to its row')
      .toBeTruthy();
    btn!.click();
    expect(f.componentInstance.poolPair).toBe('nvidia|z-ai/glm-5.3-flash');
  });

  it('routes by the pair the picker keys its cards by, not by the endpoint\'s display name', async () => {
    // `/ranking` puts the provider's LABEL on the row and `/my/pool/options` cards are addressed by
    // the SLUG (`data-pair` is "nvidia|model"); a route built from the label would land on the right
    // page with nothing highlighted, which is the failure this control exists to prevent.
    const f = await build({
      ranked: [card({ provider: 'openrouter', provider_name: 'OpenRouter', provider_id: 2 })],
    });
    poolBtnIn(cards(f)[0])!.click();
    expect(f.componentInstance.poolPair).toBe('openrouter|z-ai/glm-5.3-flash');
  });

  it('offers no pool route on a card he cannot pay for, where the only way out is a key', async () => {
    const f = await build({ ranked: [card({ verified_key: false, admin_key: false })] });
    expect(poolBtnIn(cards(f)[0])).toBe(null);
    expect(buttonsIn(cards(f)[0])[0].classList.contains('key-btn')).toBe(true);
  });

  it('turns the one action into getting that endpoint\'s key when the user has none', async () => {
    const f = await build({
      ranked: [
        card({
          verified_key: false,
          admin_key: false,
          provider_id: 2,
          provider: 'openrouter',
          provider_name: 'OpenRouter',
        }),
      ],
    });
    const el = cards(f)[0];
    const btns = buttonsIn(el);
    expect(btns.length, 'a locked card still has one action, and it is the way out').toBe(1);
    expect(el.querySelector('.pick-btn')).toBe(null);
    expect(btns[0].textContent).toContain('OpenRouter');
    btns[0].click();
    expect(f.componentInstance.askedKey).toBe(2);
    expect(f.componentInstance.used, 'asking for a key must not pin a scorer').toBe(null);
  });

  it('still names and preselects the endpoint when the row carries no provider id', async () => {
    // The shape /ranking actually sends for a measured model that has no `models` row: the run
    // records its endpoint by NAME and leaves provider_id null -- which is most of the table on a
    // fresh install. Measured live on the packaged app 2026-10-19: the click opened the keys
    // surface on NVIDIA, because the id the handoff needs was never in the row, and the button
    // said "Add a key for openrouter" in the slug rather than the name the user knows.
    const f = await build({
      ranked: [
        card({
          verified_key: false,
          admin_key: false,
          provider: 'openrouter',
          provider_id: null,
          provider_name: null,
        }),
      ],
    });
    const btn = buttonsIn(cards(f)[0])[0];
    expect(btn.textContent).toContain('OpenRouter');
    btn.click();
    expect(f.componentInstance.askedKey).toBe(2);
  });

  it('keeps the endpoint a card belongs to visible, since that is what the button asks for', async () => {
    // The endpoint used to be a plain chip of text; the mark carries the same name now, so the
    // rule being protected is "the card names the endpoint the way the keys page names it", not
    // "there is a chip in position zero".
    const f = await build({
      ranked: [card({ verified_key: false, admin_key: false, provider: 'openrouter', provider_name: null })],
    });
    expect(markIn(cards(f)[0]), 'the endpoint has to be named on the card').toBeTruthy();
    expect(cards(f)[0].querySelector('app-provider-mark')?.textContent).toContain('OpenRouter');
  });

  it('keeps a measured health badge on the card, out of any pool control', async () => {
    const f = await build({ ranked: [card({ health: 'down', consecutive_failures: 2 })] });
    const badge = cards(f)[0].querySelector('.rank-meta .chip.bad') as HTMLElement;
    expect(badge).toBeTruthy();
    expect(badge.textContent).toContain('down');
    expect(badge.textContent).toContain('2 failed');
  });
});

/**
 * The Test button, and what replaced the call it used to spend.
 *
 * It used to be a wake-check: one 8-token `POST /my/probe` on the caller's own key that writes
 * nothing server-side, and printed "answered in 1.8 s" beside the button. Measured against what the
 * page is for, that answer is worth less than the action: the user's instruction is that Test does
 * what "Score with this model" does. So Test is now a second way to point the next run at this
 * model — it emits the same pick, costs nothing, and the model is really tested by the detection it
 * lands him on. What the specs still insist on: Test is available exactly where the pick is (a
 * shared-key card can score a run, so it can be tested too), and no card reaches for the probe
 * endpoint any more, which `buildBackend` enforces by throwing.
 */
describe('RankingView test button', () => {
  it('does what "Score with this model" does, and spends no call to do it', async () => {
    const f = await build({ ranked: [card()] });
    const btn = testBtnIn(cards(f)[0]);
    expect(btn, 'the Test affordance has to stay on the rank card').toBeTruthy();
    // `probeMyModel` THROWS in this stub, so the assertion that matters is the one above the click:
    // if the old path were still wired, this test would fail with its reason, not with a diff.
    btn!.click();
    f.detectChanges();
    expect(f.componentInstance.used).toBe('z-ai/glm-5.3-flash');
  });

  it('emits the card itself, so the shell toggles a pinned model from Test too', async () => {
    const f = await build({ ranked: [card()], picked: 'z-ai/glm-5.3-flash' });
    testBtnIn(cards(f)[0])!.click();
    f.detectChanges();
    // The shell owns switch-back (`useRankedModel` releases when the same model is emitted), and
    // this view only reports the intent — so Test on the picked card is the release, exactly as the
    // pick button is.
    expect(f.componentInstance.used).toBe('z-ai/glm-5.3-flash');
  });

  it('is live on a card the deployment\'s shared key pays for, because a run there costs him nothing', async () => {
    // The old rule was the probe's, not the pick's: a test could not spend the shared key, so the
    // button sat disabled on an admin-key card with the reason on its title. Now Test is the pick,
    // and a shared-key card can be picked.
    const f = await build({ ranked: [card({ verified_key: false, admin_key: true })] });
    const btn = testBtnIn(cards(f)[0])!;
    expect(btn.disabled, 'disabled is now a bug, not a policy').toBe(false);
    btn.click();
    expect(f.componentInstance.used).toBe('z-ai/glm-5.3-flash');
  });

  it('says in its own words what pressing it does', async () => {
    const f = await build({ ranked: [card()] });
    const btn = testBtnIn(cards(f)[0])!;
    expect((btn.textContent ?? '').trim()).toBe('Test');
    const title = btn.getAttribute('title') ?? '';
    expect(title).toContain('Score with this model');
    expect(title.toLowerCase(), 'a label that promised a wake-check would be a lie now')
      .not.toContain('awake');
  });

  it('offers no test on a card that has no key at all, where the only action is getting one', async () => {
    const f = await build({ ranked: [card({ verified_key: false, admin_key: false })] });
    const el = cards(f)[0];
    expect(testBtnIn(el)).toBe(null);
    expect(poolBtnIn(el)).toBe(null);
    expect(buttonsIn(el).length, 'a locked card still has exactly one action').toBe(1);
  });
});

/**
 * The two states a card has to show without a control for them.
 *
 * A model that is already in his using pool, and a model he cannot pay for, used to look identical
 * to a model he could simply add -- and the misreading that "this model is gone" is the one this
 * project refuses to invite. Both are now stated by the frame and by the button, not by a menu:
 * the pool is still edited on the other surface, so nothing here can change an order.
 */
describe('RankingView card state', () => {
  it('frames a card whose model is already in his using pool', async () => {
    const f = await build({ ranked: [card({ in_pool: true, your_rank: 2 })] });
    expect(cards(f)[0].classList.contains('in-pool')).toBe(true);
  });

  it('leaves a card out of his pool unframed', async () => {
    const f = await build({ ranked: [card({ in_pool: false })] });
    expect(cards(f)[0].classList.contains('in-pool')).toBe(false);
  });

  it('makes getting the key the loud action on a card he cannot pay for', async () => {
    const f = await build({ ranked: [card({ verified_key: false, admin_key: false })] });
    const btn = buttonsIn(cards(f)[0])[0];
    expect(btn.classList.contains('key-btn')).toBe(true);
    expect(btn.classList.contains('ghost'), 'a ghost button reads as the quiet option').toBe(false);
  });

  it('lights the provider mark only where his own key pays for it', async () => {
    const ready = await build({ ranked: [card({ verified_key: true })] });
    expect(markIn(cards(ready)[0])?.classList.contains('lit')).toBe(true);
  });

  it('keeps the mark grey on a card whose endpoint he cannot pay for', async () => {
    const locked = await build({ ranked: [card({ verified_key: false, admin_key: true })] });
    expect(markIn(cards(locked)[0])?.classList.contains('lit')).toBe(false);
  });
});

/**
 * The fresh-install state, which is a real bug report rather than a nice-to-have: on a packaged
 * first boot (2026-10-16) the DB held zero keys, so the ranking rendered 48 cards with no pick
 * button and the page gave no reason — a user could only conclude the app was broken, while
 * detection in fact still ran on the deployed lane. The explanation stays on this page (it is about
 * the table); its way out now opens the surface that actually holds keys.
 */
describe('RankingView scoring lock', () => {
  it('explains the lock and sends the way out to the key surface', async () => {
    const f = await build({
      ranked: [
        card({ verified_key: false }),
        card({ model: 'google/gemma-4-31b-it', rank: 2, verified_key: false }),
      ],
    });
    const note = f.nativeElement.querySelector('.lock-note');
    expect(note, 'the page must say why nothing is pickable').toBeTruthy();
    expect(note.textContent).toContain('no verified key');
    // the honest half: the app is not broken, only the choice of scorer is unavailable
    expect(note.textContent).toContain('Detection still works');
    expect(f.nativeElement.querySelector('button.pick-btn')).toBe(null);
    const btn = note.querySelector('button') as HTMLButtonElement;
    // The button names its destination, because it is a route and not an in-place act.
    expect(btn.textContent).toContain('Open Models & keys');
    btn.click();
    // no endpoint named: this is the general "I have no key at all" case
    expect(f.componentInstance.askedKey).toBe(null);
  });

  it('disappears the moment one card has a key behind it', async () => {
    const f = await build({
      ranked: [card({ admin_key: true }), card({ model: 'other/x', rank: 2, admin_key: true })],
    });
    expect(f.nativeElement.querySelector('.lock-note')).toBe(null);
    expect(f.nativeElement.querySelectorAll('button.pick-btn').length).toBe(2);
  });

  it('tells a signed-out visitor to sign in rather than offering a form they cannot use', async () => {
    const f = await build({ ranked: [card({ verified_key: false })], signedIn: false });
    const note = f.nativeElement.querySelector('.lock-note');
    expect(note.textContent).toContain('Sign in first');
    expect(note.querySelector('button')).toBe(null);
  });

  it('says nothing while the ranking is empty', async () => {
    const f = await build({ ranked: [] });
    expect(f.nativeElement.querySelector('.lock-note')).toBe(null);
  });

  // Two fixtures, two `it`s: `build()` configures a TestBed, and Angular refuses to configure one
  // that has already been instantiated -- so the local and hosted wordings are separate tests, not
  // one test that builds twice.
  it('names the surface that fills an empty ranking as Advanced parameters on a local install', async () => {
    const local = await build({ ranked: [], local: true });
    const text = local.nativeElement.querySelector('.empty').textContent ?? '';
    expect(text).toContain('Advanced parameters');
    expect(text).not.toContain('Admin tab');
  });

  it('names the Admin tab as the surface that fills an empty ranking when accounts exist', async () => {
    const hosted = await build({ ranked: [] });
    expect(hosted.nativeElement.querySelector('.empty').textContent).toContain('Admin tab');
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
 *
 * The head's control opens a sheet over the table rather than a browser tab, so the two states are
 * now read out of what the sheet says: it always frames the bundle's own copy, and it names an
 * outside record only when one is stored.
 */
describe('RankingView report link', () => {
  const trigger = (f: ComponentFixture<HostRanking>) =>
    f.nativeElement.querySelector('button.paper-link') as HTMLButtonElement;
  const open = async (f: ComponentFixture<HostRanking>) => {
    trigger(f).click();
    f.detectChanges();
    await f.whenStable();
    return f;
  };
  const sheet = (f: ComponentFixture<HostRanking>) =>
    f.nativeElement.querySelector('.sheet') as HTMLElement | null;

  it('opens the study beside the table and puts the table back on closing', async () => {
    const f = await build({
      ranked: [],
      config: { paper_url: 'https://zenodo.org/doi/10.5281/zenodo.2', paper_title: 'Read the preprint' },
    });
    expect(sheet(f)).toBe(null);
    expect(trigger(f).textContent).toContain('Read the preprint');

    await open(f);
    expect(sheet(f), 'the click must answer').toBeTruthy();
    // The framed page is the copy this bundle ships — an archive refuses to be framed, so the DOI
    // is named and linked instead of being loaded into an empty box.
    expect(f.nativeElement.querySelector('.report-frame').getAttribute('src')).toBe(
      '/research/report.html',
    );
    expect(f.nativeElement.querySelector('.report-record').getAttribute('href')).toBe(
      'https://zenodo.org/doi/10.5281/zenodo.2',
    );

    (f.nativeElement.querySelector('.sheet-close') as HTMLButtonElement).click();
    f.detectChanges();
    expect(sheet(f)).toBe(null);
  });

  it('still cites the shipped copy when no paper URL is configured', async () => {
    const f = await open(
      await build({ ranked: [], config: { paper_url: '', paper_title: '' } }),
    );
    expect(trigger(f).textContent).toContain('Technical report');
    expect(f.nativeElement.querySelector('.report-frame').getAttribute('src')).toBe(
      '/research/report.html',
    );
    // No record stored means no record printed: a DOI here would be invented metadata.
    expect(f.nativeElement.querySelector('.report-record')).toBe(null);
    expect(f.nativeElement.textContent).toContain('/research/report.html');
  });

  it('labels a saved URL with a default rather than showing an empty label', async () => {
    const f = await build({
      ranked: [],
      config: { paper_url: 'https://zenodo.org/doi/10.5281/zenodo.2', paper_title: '' },
    });
    expect(trigger(f).textContent).toContain('Read the paper');
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
 *
 * This part of the old page stays: it is a statement about the measured table, not a control over
 * the models. The pool it names is ordered on the other surface.
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

  const bar = (f: ComponentFixture<HostRanking>) =>
    f.nativeElement.querySelector('.lane-authority') as HTMLElement;

  it('names the model that answers, the admin default, and the tie-break state', async () => {
    const f = await build({ ranked: [card()], authority: AUTH() });
    const text = bar(f).textContent?.replace(/\s+/g, ' ') ?? '';
    expect(text).toContain('deployed/primary');
    expect(text, 'the admin row has to be labelled as the admin row').toContain('Deployed default');
    expect(text).toContain('tie-break lane off');
  });

  it('lists the auto-switch order the user pool gives him, in his order', async () => {
    const f = await build({
      ranked: [card()],
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
      ranked: [card()],
      authority: AUTH({ next: { model: 'm/one', provider: 'nvidia', source: 'user-pool' } }),
    });
    expect(bar(f).textContent).toContain('nothing behind it');
  });

  it('marks a pinned model as one run only and releases it from this page', async () => {
    const f = await build({ ranked: [card()], authority: AUTH(), picked: 'pinned/model-x' });
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
      ranked: [card()],
      authority: AUTH({ deployed_default: null, next: null }),
    });
    const text = bar(f).textContent?.replace(/\s+/g, ' ') ?? '';
    expect(text).toContain('No primary model is enabled');
    expect(text).not.toContain('deployed/primary');
  });

  it('renders no claim at all when the backend reports no authority', async () => {
    const f = await build({ ranked: [card()], authority: undefined });
    expect(bar(f)).toBe(null);
  });

  it('links the printable PDF next to the report, not only inside it', async () => {
    const f = await build({ ranked: [card()], authority: AUTH() });
    const pdf = f.nativeElement.querySelector('a.pdf-link') as HTMLAnchorElement;
    expect(pdf, 'the research PDF has to be reachable from the ranking in one click').toBeTruthy();
    expect(pdf.getAttribute('href')).toBe('/research/report.pdf');
    expect(pdf.getAttribute('target')).toBe('_blank');
  });

  it('chips the card that answers his runs and the card that is the admin default', async () => {
    const f = await build({
      ranked: [card()],
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
      ranked: [card()],
      authority: AUTH({
        deployed_default: { model: 'other/model', provider: 'nvidia' },
        next: { model: 'other/model', provider: 'nvidia', source: 'deployed-default' },
      }),
    });
    f.detectChanges();
    const labels = Array.from(f.nativeElement.querySelectorAll('.rank-card .chip')).map(
      (c) => (c as HTMLElement).textContent?.trim(),
    );
    expect(labels).not.toContain('used for your runs');
    expect(labels).not.toContain('deployed default');
  });

  /** Measured live on the shipped page, 2026-10-21: the deployed primary carried `free tier`,
      `deployed · primary`, `shared key`, `used for your runs` AND `deployed default`, and that last
      pair is one fact read from two sources — the row's own `lane` role and the authority's
      `deployed_default`. The row said it twice while its neighbours said it once, and the extra line
      made the card the tallest on the page. When the lane chip is already on the card it carries the
      claim; `deployed default` stays for the case the chip cannot cover — a row the deployment
      answers with that has no `lane` role of its own (the env default, which has no marked row). */
  it('says "deployed" once when the lane row is also the deployed default', async () => {
    const f = await build({
      ranked: [card({ lane: 'primary' })],
      authority: AUTH({
        deployed_default: { model: 'z-ai/glm-5.3-flash', provider: 'nvidia' },
        next: { model: 'z-ai/glm-5.3-flash', provider: 'nvidia', source: 'deployed-default' },
      }),
    });
    const labels = chipLabels(cards(f)[0]);
    expect(labels).toContain('deployed · primary');
    expect(labels).not.toContain('deployed default');
  });

  /** The frame is a single channel and the pool owns it — that is the yellow neon the user asked
      for. A second fact used to be painted on the same border (`deployed` in --good), and the two
      cannot coexist: adding the deployed default to the pool swapped green for gold and the deployed
      marker vanished with no warning. One fact, one channel; the deployment's claim lives in the
      chip and in the authority bar above the grid, both of which survive a pool. */
  it('leaves the card frame to the pool alone', async () => {
    const f = await build({
      ranked: [card({ lane: 'primary', in_pool: true })],
      authority: AUTH({
        deployed_default: { model: 'z-ai/glm-5.3-flash', provider: 'nvidia' },
        next: { model: 'z-ai/glm-5.3-flash', provider: 'nvidia', source: 'user-pool' },
      }),
    });
    const el = cards(f)[0];
    expect(el.classList.contains('in-pool'), 'the pool row keeps its frame').toBe(true);
    expect(
      el.classList.contains('deployed'),
      'a second frame would fight the pool frame for the same border',
    ).toBe(false);
  });
});
