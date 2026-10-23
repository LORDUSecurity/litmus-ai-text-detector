import { Component, ElementRef, OnDestroy, OnInit, afterRenderEffect, computed, effect, inject, input, signal, viewChild } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { BackendService } from '../../core/backend.service';
import { AuthService } from '../../core/auth.service';
import { composeBaseUrl, decomposeBaseUrl } from '../../core/providers';
import { AdminKeys } from '../admin-keys/admin-keys';
import { ProviderMark } from '../provider-mark/provider-mark';
import { TieBreakSwitch } from '../tie-break-switch/tie-break-switch';
import type {
  ConfigProvider,
  DiscoveryJob,
  PoolEntry,
  PoolOption,
  PublicConfig,
  UserKeyView,
} from '../../core/backend.types';

/** One endpoint's block of the add-models menu: its mark, whether a key of this caller's can pay
    for it, and every model row that belongs to it. `payable` is the whole reason the group owns the
    key button instead of each card: twenty models behind one absent key is one question, not twenty. */
interface PickGroup {
  provider: string;
  label: string;
  providerId: number | null;
  payable: boolean;
  items: PoolOption[];
}

/** Controls the endpoints and the models this account spends: BYOK keys, the guides for getting
    one, the using-pool detection walks down, and discovery of the free models a key can reach.
    Split out of the Ranking page, which keeps only the measurement and the one button that points
    a run at a model. The split is the point: a card used to carry a pick button, a pool button and
    a key form several screens away from it, so "score with this" and "pay for this" — two different
    decisions — were one scroll apart and looked like the same kind of control.

    Users never touch thresholds, sampling or bands here either. Those are the deployed engine's
    parameters and stay on the admin surface (called "Advanced parameters" on a single-owner
    install), because a user editing a calibrated band breaks the measurement the ranking cites. */
@Component({
  selector: 'app-providers-view',
  standalone: true,
  imports: [ProviderMark, TieBreakSwitch, AdminKeys],
  host: { '(document:keydown.escape)': 'onEscape()' },
  styles: `
    /* Two selects that choose a model. They take no corner treatment of their own: the cut belongs to
       two boxes in the whole app (the primary action, the brand mark) and a select is neither, so this
       is a square-edged control bounded by --edge like every other one.
       (No backticks in this comment: it lives inside a template literal.) */
    .pool-tools,
    .discover-actions {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--s-2);
    }
    .pool-tools {
      margin: var(--s-2) 0 0;
    }
    /* Only the smaller voice is overridden here; the global field rule already owns the ground, the
       findable edge and the padding, and re-typing them is how a control drifts off the ladder. */
    .pool-row select.pool-change-target {
      min-width: 9.375rem;
      max-width: 15rem;
      font-size: var(--fs-meta);
    }

    /* The add-key surface: the endpoints as stickers, pressed one at a time. The chosen one takes
       the same outlined frame a held model card does, so "this is the one you picked" is one visual
       language across the page rather than a second convention. */
    .key-stickers {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--s-2);
      margin: 0 0 var(--s-3);
    }
    .sticker {
      display: inline-flex;
      align-items: center;
      padding: var(--s-2) var(--s-3);
      color: var(--ink);
      background-color: var(--field-bg);
      border: 1px solid var(--edge);
    }
    .sticker[aria-pressed='true'] {
      border-color: var(--accent);
      box-shadow: inset 0 0 0 1px var(--accent);
    }
    .key-for,
    .endpoint-preview {
      flex: 1 1 100%;
      margin: 0;
    }
    /* The address of a gateway the caller runs himself, on its own row: four fields that mean one
       thing, so they group rather than interleave with the key and its name. */
    .endpoint-fields {
      display: flex;
      flex-wrap: wrap;
      align-items: flex-end;
      gap: var(--s-2);
      flex: 1 1 100%;
    }
    .endpoint-fields input[name=port] {
      min-width: 5.25rem;
    }
    .endpoint-fields input[name=path] {
      min-width: 7rem;
    }

    /* The add-models menu. Two columns on a wide screen, one on a phone: the cards are read as a
       list of states, not as a table, so they wrap instead of scrolling sideways. */
    .pool-picker {
      margin-top: var(--s-4);
    }
    .pick-group {
      margin-top: var(--s-3);
    }
    .pick-ghead {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--s-2);
      padding-bottom: var(--s-1);
      border-bottom: 1px solid var(--edge);
    }
    .pick-ghead .get-key {
      margin-left: auto;
    }
    .pick-list {
      list-style: none;
      display: grid;
      gap: var(--s-2);
      grid-template-columns: 1fr;
      margin: var(--s-2) 0 0;
      padding: 0;
    }
    @media (min-width: 55rem) {
      .pick-list {
        grid-template-columns: repeat(2, minmax(0, 1fr));
      }
    }
    .pick-card {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: var(--s-2);
      /* --s-4 on the right: this card is cut by the corner language, and the notch takes 14px out of
         the padding box. The ink is the frame's own weight — a card that draws --edge and cuts --line
         has a corner lighter than the edge it is supposed to continue. */
      --cut-ink: var(--edge);
      padding: var(--s-2) var(--s-4);
      background-color: var(--panel);
      border: 1px solid var(--edge);
    }
    /* The frame IS the state. Held rows take the accent outline — the colour this app already
       spends on "this is the answer" — because an outline is the only way to say "yours already"
       without inventing a sixth palette colour. A row he cannot pay for yet recedes the same way
       it was always meant to: by dropping to the grouping edge, never by fading the whole row.
       Opacity multiplies into the text it covers, and at the strength that reads as quiet the
       reason line falls under 4.5:1 on both grounds — and "needs a key for NVIDIA" is precisely
       the sentence he has to be able to read. So the frame goes soft and the words stay whole. */
    .pick-card.in-pool {
      border-color: var(--accent);
      --cut-ink: var(--accent);
      box-shadow: inset 0 0 0 1px var(--accent);
    }
    .pick-card.needs-key {
      border-color: var(--line);
      --cut-ink: var(--line);
    }
    /* The frame a ranking card sent the user to. Same accent, doubled, because two facts are on this
       row at once — he came for THIS one, and the pool may already hold it — and a held state must not
       look identical to the answer he asked for. It is lit FROM INSIDE: this card is cut, and a
       clip-path cuts the element's own box-shadow along with it, so an outer ring or a spread would
       arrive with two corners missing. The gold ground wash plus a doubled inset ring is the loud
       version that survives the notch, and the corner rules follow the frame because --cut-ink does. */
    .pick-card.neon {
      border-color: var(--accent);
      --cut-ink: var(--accent);
      background-color: color-mix(in oklab, var(--accent) 12%, var(--panel));
      box-shadow:
        inset 0 0 0 1px var(--accent),
        inset 0 0 0 3px color-mix(in oklab, var(--accent) 30%, transparent);
    }
    .picker-focus-note {
      margin: var(--s-2) 0 0;
      font-size: var(--fs-meta);
      color: var(--ink-dim);
      line-height: var(--lh-mono);
    }
    /* The same container the pool row uses, and it needs the same rule. Angular drops the
       whitespace-only text between two elements when the source breaks the line, so a name and the
       note after it used to be painted as one word: "deepseek-ai/deepseek-v4.1-flashnever measured".
       The gap has to be layout, not a character in the template, or the next reformat of this file
       takes it back away — and the pool row already proves the pattern above. */
    .pick-card .grow {
      display: flex;
      align-items: baseline;
      flex-wrap: wrap;
      gap: 0 var(--s-2);
      min-width: 0;
    }

    /* The offers reuse .pool-row, so they inherit its ground and its control edge from the global
       sheet; this only stacks the reason line under the model instead of beside it. */
    .offer-list {
      list-style: none;
      display: flex;
      flex-direction: column;
      gap: var(--s-2);
      margin: 0 0 var(--s-3);
      padding: 0;
    }
    .offer-why {
      flex: 1 1 100%;
      margin: 0;
      font-size: var(--fs-meta);
      color: var(--ink-dim);
    }
    .offer-actions {
      display: flex;
      gap: var(--s-2);
    }
    /* Both said in tokens instead of a style="" attribute: the key list keeps one step of air before
       the add-key block, and the privacy note closes the panel rather than opening a paragraph. */
    .byok {
      margin-bottom: var(--s-4);
    }
    .byok-note {
      margin: var(--s-3) 0 0;
    }
  `,
  template: `
    @if (error(); as e) {
      <!-- A failed read of config or ranking leaves every picker below empty for a reason the
           user cannot see, so the page names the break instead of rendering a blank "Your API
           keys" panel over a backend that answered nothing. -->
      <p class="auth-error" role="alert">{{ e }}</p>
    }

    <section class="panel">
      <div class="panel-head">
        <h2>Your API keys</h2>
        <span class="muted">Bring your own key for an allowed endpoint — it is auto-picked when verified.</span>
      </div>

      @if (keys().length) {
        <div class="byok">
          @for (k of keys(); track k.id) {
            <div class="key-row">
              <div class="grow">
                <strong>{{ k.provider_name || ('provider #' + k.provider_id) }}</strong>
                <span class="hint"> · {{ k.key_hint }} · {{ k.label }}</span>
              </div>
              @if (k.verified) {
                <span class="chip good">verified</span>
              } @else {
                <span class="chip">not verified</span>
              }
              <button type="button" class="ghost danger"
                      [attr.aria-label]="'Remove the ' + (k.provider_name || ('provider #' + k.provider_id)) + ' key'"
                      (click)="removeKey(k.id)">Remove</button>
            </div>
          }
        </div>
      }

      <!-- The endpoints first, side by side, at sticker size: "which one is this key for" is the
           question this form answers, and a mark the user already recognises answers it without him
           opening a select and reading a list. Lit means a key of his can already pay for it. The
           fields stay shut until he says he is adding one. -->
      <div class="key-stickers">
        @for (p of providers(); track p.id) {
          <button type="button" class="sticker" data-act="pick-endpoint"
                  [attr.data-provider]="p.name"
                  [attr.aria-pressed]="providerId() === p.id"
                  [disabled]="saving()" (click)="chooseProvider(p.id)">
            <app-provider-mark [big]="true" [provider]="p.name" [label]="p.label || p.name"
                               [lit]="providerId() === p.id || payableProviders().has(p.name)" />
          </button>
        }
        <button type="button" class="primary" data-act="add-key"
                [attr.aria-expanded]="keyFormOpen()" (click)="toggleKeyForm()">
          {{ keyFormOpen() ? 'Close' : 'Add new key' }}
        </button>
      <!-- The shared half of the same subject: which endpoints exist at all, and which key this
           install dials when a detection has none of its own. It used to sit at the bottom of
           Advanced parameters, several screens from the logos it decides — a brand you added there
           only appeared here after a reload, and a key you stored there lit no sticker until you
           found this page again. One window, opened from where the marks are drawn, both halves of
           "what can this install pay for". The model chooser (per-model bands, which lane a row
           serves) stays on Advanced, because those are the calibrated parameters a ranking cites.
           Admin-only on purpose: /admin/providers is an admin route, so a plain user's click would
           only be refused. -->
      @if (canManageKeys()) {
        <button type="button" class="ghost" data-act="manage-keys"
                aria-haspopup="dialog" (click)="manageOpen.set(true)">
          Manage keys and endpoints
        </button>
      }
      </div>

      @if (keyFormOpen()) {
        <form class="inline-form" (submit)="$event.preventDefault(); saveKey()">
          <p class="hint key-for">
            Key for <strong>{{ selectedProvider()?.label || selectedProvider()?.name }}</strong>
            <span class="muted"> · press another sticker to change that</span>
          </p>
          <label>
            <span>API key</span>
            <input #keyField type="password" name="key" [value]="newKey()" (input)="onKeyInput($event)"
                   placeholder="paste your key" autocomplete="off" [disabled]="saving()" />
          </label>
          <label>
            <span>Name this key (optional)</span>
            <input name="label" [value]="keyName()" (input)="onKeyNameInput($event)"
                   placeholder="e.g. laptop" autocomplete="off" [disabled]="saving()" />
          </label>
          <!-- Only a self-hosted gateway gets to be asked. NVIDIA, OpenRouter and Gemini each live
               at one URL the deployment already has, and POST /my/keys refuses an address for a
               provider that is not flagged self-hosted — so offering the field there would be
               offering a rejection. -->
          @if (selfHosted()) {
            <div class="endpoint-fields">
              <label>
                <span>Scheme</span>
                <select name="scheme" (change)="onSchemeChange($event)" [disabled]="saving()">
                  <option value="https" [selected]="epScheme() === 'https'">https</option>
                  <option value="http" [selected]="epScheme() === 'http'">http</option>
                </select>
              </label>
              <label>
                <span>Endpoint</span>
                <input name="host" [value]="epHost()" (input)="onHostInput($event)"
                       placeholder="127.0.0.1 or router.example.com" autocomplete="off"
                       [disabled]="saving()" />
              </label>
              <label>
                <span>Port</span>
                <input name="port" [value]="epPort()" (input)="onPortInput($event)"
                       placeholder="20128" inputmode="numeric" autocomplete="off" [disabled]="saving()" />
              </label>
              <label>
                <span>Path</span>
                <input name="path" [value]="epPath()" (input)="onPathInput($event)"
                       placeholder="/v1" autocomplete="off" [disabled]="saving()" />
              </label>
            </div>
            <p class="hint endpoint-preview">{{ endpointPreview() }}</p>
          }
          <label>
            <span>Model to verify with (optional)</span>
            <input name="model" [value]="verifyModel()" (input)="onModelInput($event)"
                   [placeholder]="verifyPlaceholder()" [disabled]="saving()" />
          </label>
          <button class="primary" type="submit" [disabled]="saving() || !canSave()">
            {{ saving() ? 'Verifying…' : (verifyModel().trim() ? 'Save and verify' : 'Save key') }}
          </button>
        </form>
      }

      @if (keyMsg(); as m) {
        <p class="doc-status" [class.bad]="m.bad" aria-live="polite">{{ m.text }}</p>
      }
      <p class="muted byok-note">
        The plaintext is encrypted at rest and never returned to the page — only the hint above.
        When a verified key exists for an endpoint, detection uses it automatically.
      </p>
    </section>

    <!-- The other control that came from Advanced: the switch, not the chooser. It writes
         models.enabled on the row that owns the tie-break lane — one fact about which model
         answers a run, which is this page's subject. The lane ASSIGNMENT and the bands stay where
         they were, next to every other per-model parameter. (No backticks in this comment: it lives
         inside a template literal.) -->
    @if (canManageKeys()) {
      <section class="panel">
        <app-tie-break-switch />
      </section>
    }

    @if (guides().length) {
      <section class="panel">
        <div class="panel-head">
          <h2>How to get an API key</h2>
          <span class="muted">Short guides and direct links.</span>
        </div>
        <div class="guides">
          @for (g of guides(); track g.provider + g.url) {
            <div class="guide">
              <h4>{{ g.title || g.provider }}</h4>
              @if (g.steps) { <p>{{ g.steps }}</p> }
              <a [href]="g.url" target="_blank" rel="noopener noreferrer">
                Open {{ g.provider }} →
              </a>
            </div>
          }
        </div>
      </section>
    }

    @if (signedIn()) {
      <section class="panel pool-panel">
        <div class="panel-head">
          <h2>Your using pool</h2>
          <span class="muted">
            The order detection follows. Drag to rank — a model that stops answering is served
            last until it answers again, and your rank comes back untouched.
          </span>
        </div>

        <!-- The way a model enters the menu is the same call the menu's cards make, so an order the
             page wrote by hand is not a second answer to the question the Ranking page already
             answered. It ranks what has a measurement and leaves the rest at the back in the user's
             own order: no capture is not a verdict, and a model that only timed out is still his. -->
        <div class="pool-tools">
          <button type="button" class="ghost" data-act="auto-rank"
                  [disabled]="poolBusy() || pool().length === 0"
                  aria-label="Reorder the pool by the measurement, best first"
                  (click)="autoRank()">Auto rank</button>
          <span class="hint">best first, then what has no capture to judge it on</span>
        </div>

        @if (poolBusy()) {
          <p class="empty">Saving the order…</p>
        }
        @if (poolMsg(); as pm) {
          <p class="doc-status" [class.bad]="pm.bad" aria-live="polite">{{ pm.text }}</p>
        }

        <!-- A model that only answered the screen after it timed out is the one entry on this page
             that is waiting on an answer from the user, not from a server. It is already pickable
             (the backend wrote its pool row), so the choice below is about the pool, not about
             whether the model exists. When the window runs out the card simply disappears: silence
             is not a veto, and it is never a pin either — the next discovery run asks it again. -->
        @if (offers().length) {
          <ul class="offer-list">
            @for (f of offers(); track f.key) {
              <li class="pool-row offer-row">
                <span class="grow">
                  <app-provider-mark [provider]="f.o.provider" [label]="f.o.provider_name || f.o.provider" />
                  <strong>{{ f.o.model }}</strong>
                  <span class="chip warn" role="timer" aria-live="polite">
                    {{ countdown(f.left) }} left to decide
                  </span>
                </span>
                <p class="offer-why">
                  {{ f.o.probe_note || 'It answered the screen, but only after a retry.' }}
                  @if (f.o.answer_ms !== null) { · answered in {{ seconds(f.o.answer_ms) }} s }
                  @if (f.o.in_pool) { · already in your pool at #{{ f.o.your_rank }} }
                </p>
                <span class="offer-actions">
                  <button type="button" class="ghost" data-act="accept-offer"
                          [attr.aria-label]="'Add ' + f.o.model + ' to my using pool'"
                          [disabled]="deciding() !== null" (click)="decide(f.o, 'accept')">
                    Accept
                  </button>
                  <button type="button" class="ghost danger" data-act="refuse-offer"
                          [attr.aria-label]="'Keep ' + f.o.model + ' out of my pool, without removing it'"
                          [disabled]="deciding() !== null" (click)="decide(f.o, 'refuse')">
                    Refuse
                  </button>
                </span>
              </li>
            }
          </ul>
        }

        @if (pool().length === 0) {
          <p class="empty">
            Nothing in your pool yet, so detection runs on the deployed lane. Put a model in below
            — or let discovery find the free ones your key can reach.
          </p>
        } @else {
          <ol class="pool-list">
            @for (p of pool(); track p.provider + '/' + p.model; let i = $index) {
              <li class="pool-row" [class.demoted]="p.demoted" [class.dragging]="dragIndex() === i"
                  [class.drop-before]="overIndex() === i"
                  draggable="true"
                  (dragstart)="onDragStart(i, $event)"
                  (dragover)="onDragOver(i, $event)"
                  (drop)="onDrop(i, $event)"
                  (dragend)="onDragEnd()">
                <span class="grip" aria-hidden="true">⋮⋮</span>
                <span class="pool-pos">{{ i + 1 }}</span>
                <span class="grow">
                  <app-provider-mark [provider]="p.provider" [label]="providerLabel(p.provider)"
                                     [lit]="payableProviders().has(p.provider)" />
                  <strong>{{ p.model }}</strong>
                  @if (p.source === 'auto') { <span class="chip">discovered</span> }
                  @if (p.health !== 'ok' && p.health !== 'unknown') {
                    <span class="chip" [class.warn]="p.health === 'degraded'"
                          [class.bad]="p.health === 'down'"
                          [title]="p.last_error">
                      {{ p.health }} · {{ p.consecutive_failures }} in a row
                    </span>
                  }
                  @if (p.demoted) {
                    <span class="hint demote-note">
                      you ranked it #{{ p.your_rank }} — served last because it stopped answering
                    </span>
                  }
                </span>
                <!-- Keyboard path for the drag: the order is the whole feature, so it cannot
                     depend on a pointer being available. -->
                <button type="button" class="ghost icon-only" [disabled]="i === 0 || poolBusy()"
                        [attr.aria-label]="'Move ' + p.model + ' up'" (click)="move(i, i - 1)">↑</button>
                <button type="button" class="ghost icon-only"
                        [disabled]="i === pool().length - 1 || poolBusy()"
                        [attr.aria-label]="'Move ' + p.model + ' down'" (click)="move(i, i + 1)">↓</button>
                <!-- One atomic slot change: "remove, add, re-drag" leaves the slot empty between
                     the calls, and a half-finished sequence ends with two rows or none. The choices
                     are limited to models a key can actually pay for and not already in the pool —
                     the backend refuses the other two, so offering them would only be a delay. -->
                @if (editing() === p.provider + '/' + p.model) {
                  <select class="pool-change-target" [value]="changeTarget()"
                          (change)="onChangeTarget($event)"
                          [attr.aria-label]="'Model to put in the slot holding ' + p.model">
                    <option value="">choose a model…</option>
                    @for (c of poolChangeOptions(); track c.provider + '|' + c.model) {
                      <option [value]="c.provider + '|' + c.model">
                        {{ c.model }} · {{ c.provider_name || c.provider }}
                      </option>
                    }
                  </select>
                  <button type="button" class="ghost icon-only" data-act="save-change"
                          aria-label="Save the new model in this slot"
                          [disabled]="!changeTarget() || poolBusy()" (click)="saveChange(p)">✓</button>
                  <button type="button" class="ghost icon-only" data-act="cancel-change"
                          aria-label="Cancel changing this slot" (click)="cancelChange()">×</button>
                } @else {
                  <button type="button" class="ghost icon-only" data-act="change"
                          [attr.aria-label]="'Change the model in this slot'"
                          [disabled]="poolBusy() || poolChangeOptions().length === 0"
                          (click)="openChange(p)">✎</button>
                }
                <button type="button" class="ghost danger" [disabled]="poolBusy()"
                        [attr.aria-label]="'Remove ' + p.model + ' from your using pool'"
                        (click)="removeFromPool(p.provider, p.model)">Remove</button>
              </li>
            }
          </ol>
        }

        <!-- The way a model enters the pool. It used to be a button on each ranking card, which is
             where the split left it behind: the ranking's card decides WHO SCORES this run, this
             list decides WHAT IS TRIED WHEN A MODEL IS DEAD. Same permission either way — only
             models with a key behind them can be added, because the backend refuses the rest.

             It used to be one dropdown, and a dropdown can only hold the models that are
             addable. So the two states a user most needs to see — "you already have this one" and
             "this one needs a key" — were expressed by their absence, which is indistinguishable
             from "this model is gone": exactly the reading the timeout rule exists to prevent.
             Every row is now a card, held ones get the outlined frame with his rank in it, and
             unpayable ones carry the endpoint's key button. Nothing on this list disappears
             because of a state he did not choose. -->
        <div class="pool-picker">
          <div class="panel-head">
            <h3>Add a model to the pool</h3>
            <span class="muted">
              Everything this app has a row for. An outlined card is one you already hold; a dim one
              is a model no key of yours can pay for yet.
            </span>
          </div>
          @if (focusMiss(); as missing) {
            <!-- A ranking card can name a model this menu has no row for: the ranking measures
                 whatever answered, and a card is only written for a model with a models row. So the
                 jump lands here with nothing lit — which a user reads as the app being broken, the
                 one inference this page refuses to invite about a model. Say what is missing and
                 where it came from instead of leaving a silent jump. -->
            <p class="picker-focus-note" role="status">
              <b>{{ missing }}</b> has no row here yet, so there is no card to light up. A model
              joins this list once it has been screened or has answered a run — until then it can
              still be scored with from the Ranking page.
            </p>
          }
          @for (g of pickGroups(); track g.provider) {
            <section class="pick-group" [attr.data-provider]="g.provider">
              <header class="pick-ghead">
                <app-provider-mark [provider]="g.provider" [label]="g.label" [lit]="g.payable" />
                <span class="hint">
                  {{ g.items.length }} {{ g.items.length === 1 ? 'model' : 'models' }}
                  @if (g.payable) { · a key can pay for these }
                </span>
                @if (!g.payable) {
                  <button type="button" class="primary get-key" data-act="get-key"
                          [attr.aria-label]="'Get an API key for ' + g.label"
                          (click)="askKey(g)">Get a key for {{ g.label }}</button>
                }
              </header>
              <ul class="pick-list">
                @for (o of g.items; track o.provider + '|' + o.model) {
                  <li class="pick-card" [class.in-pool]="o.in_pool" [class.needs-key]="o.needs_key"
                      [class.neon]="isFocused(o)"
                      [attr.data-pair]="o.provider + '|' + o.model">
                    <span class="grow">
                      <strong>{{ o.model }}</strong>
                      @if (!o.measured) { <span class="hint">never measured</span> }
                      @if (o.probe_status === 'slow' || o.probe_status === 'busy') {
                        <span class="hint">— {{ o.probe_status }} last time, gets asked again</span>
                      }
                      @if (o.probe_status === 'pending') {
                        <span class="hint">— answered, you have not decided yet</span>
                      }
                      @if (o.probe_status === 'declined') {
                        <span class="hint">— you refused this once</span>
                      }
                    </span>
                    @if (o.in_pool) {
                      <span class="chip good">in your pool · #{{ o.your_rank }}</span>
                    } @else if (o.needs_key) {
                      <span class="hint">needs a key for {{ g.label }}</span>
                    } @else {
                      <button type="button" class="ghost" data-act="add-pool"
                              [attr.aria-label]="'Add ' + o.model + ' to your using pool'"
                              [disabled]="poolBusy()" (click)="addToPool(o.provider, o.model)">
                        Add
                      </button>
                    }
                  </li>
                }
              </ul>
            </section>
          } @empty {
            <p class="empty">
              Nothing to add yet: a model only joins this list once it has a row, and rows are
              written by a detection or by screening an endpoint above. Do that and the card shows
              up here, measured or not.
            </p>
          }
        </div>

        <div class="discover">
          <div class="panel-head">
            <h3>Find free models on your key</h3>
            <span class="muted">
              Lists what the endpoint offers, then screens the untested ones on
              {{ screenTexts() }} real texts through the same engine path a detection uses.
            </span>
          </div>
          @if (discoverable().length === 0) {
            <p class="empty">
              Add and verify a key above first — discovery spends your own budget, so it never
              runs on the shared key.
            </p>
          } @else {
            <div class="discover-actions">
              @for (p of discoverable(); track p.id) {
                <button type="button" class="ghost" [disabled]="discovering() === p.id"
                        (click)="discover(p.id)">
                  {{ discovering() === p.id ? 'Screening…' : 'Discover ' + (p.label || p.name) }}
                </button>
              }
            </div>
          }
          @if (jobs().length) {
            <ul class="job-list">
              @for (j of jobs(); track j.id) {
                <li class="job-row" [class.bad]="j.status === 'failed'">
                  <span class="chip" [class.good]="j.status === 'done'"
                        [class.warn]="j.status === 'running' || j.status === 'queued'">
                    {{ j.status }}
                  </span>
                  <strong>{{ j.provider }}</strong>
                  <span class="hint">
                    {{ j.n_listed }} listed · {{ j.n_screened }} screened · {{ j.n_usable }} usable
                    @if (j.stage) { · {{ j.stage }} }
                    @if (j.error) { · {{ j.error }} }
                  </span>
                </li>
              }
            </ul>
            <p class="hint">{{ discoveryNote() }}</p>
          }
        </div>
      </section>
    }

    @if (manageOpen()) {
      <div class="sheet-scrim" (click)="manageOpen.set(false)"></div>
      <section class="sheet" role="dialog" aria-modal="true" aria-label="Shared keys and endpoints"
               tabindex="-1" #managePanel>
        <header class="sheet-head">
          <h3>Shared keys and endpoints</h3>
          <button type="button" class="ghost sheet-close" (click)="manageOpen.set(false)">Close</button>
        </header>
        <app-admin-keys (changed)="refreshAfterKeyAdmin()" />
      </section>
    }
  `,
})
export class ProvidersView implements OnInit, OnDestroy {
  private readonly backend = inject(BackendService);
  readonly auth = inject(AuthService);
  private readonly host = inject<ElementRef<HTMLElement>>(ElementRef);

  /** Which endpoint a ranking card sent the user here for. Read as a signal input, because the
      shell holds it (`view` is the shell's state too) and a plain field on a zoneless app would
      never re-check itself. Null means "nothing asked" — the form then opens on the first
      endpoint, exactly as it did before the split. */
  readonly focusProviderId = input<number | null>(null);
  /** Which MODEL a ranking card sent the user here for, as the picker's own address:
      `provider|model` with the provider SLUG, because that is what `data-pair` is built from on
      both sides of the wire. The frame is lit and the row is scrolled to whatever the answer is —
      held, unpayable or not — since "this is the one" is the same fact either way and the card
      below still says what to do about it. */
  readonly focusPair = input<string | null>(null);

  readonly error = signal<string | null>(null);
  readonly config = signal<PublicConfig | null>(null);
  readonly keys = signal<UserKeyView[]>([]);
  /** What this caller may pick, measured or not, each row carrying the last probe's own verdict.
      The picker reads this and NOT `/ranking`: the ranking is a measurement, so a model whose lane
      ran out of time is absent from it, and an absent card is exactly the "that model is gone"
      inference this page must not invite. A `models` row is what makes a model pickable. */
  readonly options = signal<PoolOption[]>([]);

  readonly guides = computed(() => this.config()?.api_key_guides ?? []);
  readonly providers = computed<ConfigProvider[]>(() => this.config()?.providers ?? []);
  readonly signedIn = computed(() => this.auth.user() !== null);

  // add-key form. These are signals, not plain fields, on purpose: the app runs zoneless
  // (no zone.js dependency), so a `computed` over plain fields is cached after its first
  // evaluation and nothing schedules a re-check when the user types. Live symptom on 2026-10-16:
  // typing a real key left the DOM input non-empty while "Save key" stayed disabled forever, so
  // no user could add a key at all. Signal producers make `canSave` invalidate correctly, and the
  // template-bound (input)/(change) listeners schedule change detection.
  readonly providerId = signal<number | null>(null);
  readonly newKey = signal('');
  readonly keyName = signal('');
  readonly verifyModel = signal('');
  readonly saving = signal(false);
  readonly keyMsg = signal<{ text: string; bad: boolean } | null>(null);
  /** The form is shut until the user says he is adding a key: the page opens on its endpoints, not
      on three empty fields he has to scroll past to find the keys he already has. */
  readonly keyFormOpen = signal(false);

  /** The window that holds the shared keys and the endpoints they belong to — admin-only, because
      `/admin/providers` is an admin route and a plain user's click would only be refused. A single
      owner is his own admin even when the session payload carries no role, hence `|| local()`. */
  readonly manageOpen = signal(false);
  readonly canManageKeys = computed(() => this.auth.isAdmin() || this.auth.local());
  private readonly managePanel = viewChild<ElementRef<HTMLElement>>('managePanel');

  /** The panel is created by the `@if` above, so focusing it is an effect on it existing — a call
      in the click handler would read a viewChild that is still undefined. */
  private readonly focusManagePanel = effect(() => {
    if (!this.manageOpen()) return;
    this.managePanel()?.nativeElement?.focus();
  });

  onEscape(): void {
    this.manageOpen.set(false);
  }

  /** Adding a brand or storing a shared key changes what this page draws: the stickers come from
      `/config` and the lit marks from the option rows, so both are re-read. Nothing about the key
      form is touched — a user mid-paste keeps his paste. */
  refreshAfterKeyAdmin(): void {
    this.loadConfig();
    this.refreshOptions();
  }

  // A gateway the caller runs himself has no address the deployment can guess — same provider row,
  // different machine for everyone who has one — so its form carries scheme, host, port and path
  // and composes them into the per-key endpoint POST /my/keys stores.
  readonly epScheme = signal<'http' | 'https'>('https');
  readonly epHost = signal('');
  readonly epPort = signal('');
  readonly epPath = signal('/v1');

  readonly selectedProvider = computed<ConfigProvider | undefined>(
    () => this.providers().find((p) => p.id === this.providerId()),
  );

  /** Off means the form must NOT show an address field: that endpoint is one URL the admin already
      has, and `POST /my/keys` refuses an override for a provider not flagged this way. */
  readonly selfHosted = computed(() => this.selectedProvider()?.self_hosted === true);

  readonly canSave = computed(() => this.providerId() !== null && this.newKey().trim().length > 0);

  /** The address that will be stored with the key, or '' for "the provider's own". An override that
      merely repeats the row is not sent: a stored copy would freeze this key to yesterday's seed
      address the day the admin moves the provider. Trailing slashes are not a different endpoint,
      so they are compared away. */
  readonly endpointOverride = computed(() => {
    const p = this.selectedProvider();
    if (!p || !this.selfHosted() || !this.epHost().trim()) return '';
    const url = composeBaseUrl({
      scheme: this.epScheme(),
      host: this.epHost(),
      port: this.epPort(),
      path: this.epPath(),
    });
    const trimmed = url.replace(/\/+$/, '');
    if (!trimmed || trimmed === (p.base_url || '').replace(/\/+$/, '')) return '';
    return url;
  });

  readonly endpointPreview = computed(() => {
    const p = this.selectedProvider();
    const over = this.endpointOverride();
    if (over) return `This key will be dialed at ${over}`;
    return `This key will use the endpoint ${p?.name ?? 'above'} already has: ${p?.base_url ?? ''}`;
  });

  /** The endpoint's own first listed model, so the optional verify field can name something real
      instead of asking the user to remember an id. */
  verifyPlaceholder(): string {
    return this.selectedProvider()?.models?.[0]?.model_id || 'the model id to probe';
  }

  /** Put the address fields on the endpoint the sticker chose, so a user whose gateway runs on the
      seeded port only has to change the one number that differs about his box. */
  private seedEndpoint(): void {
    const parts = decomposeBaseUrl(this.selectedProvider()?.base_url || '');
    this.epScheme.set(parts?.scheme ?? 'https');
    this.epHost.set(parts?.host ?? '');
    this.epPort.set(parts?.port ?? '');
    this.epPath.set(parts?.path ?? '/v1');
  }

  toggleKeyForm(): void {
    if (this.keyFormOpen()) {
      this.keyFormOpen.set(false);
      return;
    }
    this.seedEndpoint();
    this.keyFormOpen.set(true);
  }

  /** The field is not in the DOM until the form opens, and on a zoneless app the click handler runs
      before the pass that inserts it — so focus is an effect on the field existing, not a call in
      the handler that would read a viewChild that is still undefined. */
  private readonly focusKeyField = effect(() => {
    if (!this.keyFormOpen()) return;
    this.keyField()?.nativeElement?.focus();
  });

  chooseProvider(id: number): void {
    if (this.providerId() === id) return;
    this.providerId.set(id);
    if (this.keyFormOpen()) this.seedEndpoint();
  }

  onKeyInput(e: Event): void {
    this.newKey.set((e.target as HTMLInputElement).value);
  }

  onKeyNameInput(e: Event): void {
    this.keyName.set((e.target as HTMLInputElement).value);
  }

  onModelInput(e: Event): void {
    this.verifyModel.set((e.target as HTMLInputElement).value);
  }

  onSchemeChange(e: Event): void {
    this.epScheme.set((e.target as HTMLSelectElement).value === 'http' ? 'http' : 'https');
  }

  onHostInput(e: Event): void {
    this.epHost.set((e.target as HTMLInputElement).value);
  }

  onPortInput(e: Event): void {
    this.epPort.set((e.target as HTMLInputElement).value);
  }

  onPathInput(e: Event): void {
    this.epPath.set((e.target as HTMLInputElement).value);
  }

  ngOnInit(): void {
    this.loadConfig();
    this.loadKeys();
    this.loadPool();
    this.loadOptions();
    this.loadJobs();
  }

  /** Re-readable, because the window that adds an endpoint writes the same rows this page draws.
      The arrival logic runs once: a refresh after a key change must not re-open a form the user
      closed, and must not move the pressed sticker he chose himself. */
  private loadConfig(): void {
    this.backend.config().subscribe({
      next: (cfg) => {
        const firstRead = this.config() === null;
        this.config.set(cfg);
        if (!firstRead) return;
        // The card the user came from wins over "the first endpoint in the list": arriving here
        // from a locked NVIDIA card and finding OpenRouter pressed would send him back to the
        // ranking to read which endpoint he was on. And a card that sent him here to add a key gets
        // the open form, not a page where he has to work out what he was sent here to do.
        const wanted = this.focusProviderId();
        if (wanted !== null && cfg.providers.some((p) => p.id === wanted)) {
          this.providerId.set(wanted);
          this.keyFormOpen.set(true);
          this.seedEndpoint();
        } else if (this.providerId() === null && cfg.providers.length) {
          this.providerId.set(cfg.providers[0].id);
        }
      },
      error: (e) => this.error.set(this.describe(e)),
    });
  }

  /** The picker and the offers both come from one read, because they are the same rows seen from
      two questions: "what may I add" and "what is waiting on my answer". */
  private loadOptions(): void {
    if (!this.signedIn()) return;
    this.backend.poolOptions().subscribe({
      next: (r) => {
        this.optionsAt.set(Date.now());
        this.options.set(r.options ?? []);
        this.startTicking();
      },
      // A picker that could not be read says so through its own empty-state hint; it is not the
      // page-level failure that config or keys is, so it does not overwrite `error`.
      error: () => this.options.set([]),
    });
  }

  private loadKeys(): void {
    this.backend.myKeys().subscribe({
      next: (k) => this.keys.set(k),
      error: () => this.keys.set([]),
    });
  }

  saveKey(): void {
    if (!this.canSave()) return;
    this.saving.set(true);
    this.keyMsg.set(null);
    const model = this.verifyModel().trim();
    const name = this.keyName().trim();
    const body: Parameters<BackendService['addMyKey']>[0] = {
      provider_id: this.providerId() ?? undefined,
      api_key: this.newKey().trim(),
      label: name || undefined,
      model_id: model || undefined,
    };
    // Sent only when it actually differs from the provider's own address: `base_url: ''` would be
    // read as a blank override rather than as "no override".
    const endpoint = this.endpointOverride();
    if (endpoint) body.base_url = endpoint;
    this.backend.addMyKey(body).subscribe({
      next: (k) => {
        this.saving.set(false);
        this.newKey.set('');
        this.keyName.set('');
        this.verifyModel.set('');
        // The key is on the list below now, so the form has done its job and gets out of the way.
        this.keyFormOpen.set(false);
        // A verified key also queues discovery on the server, so say so here rather than
        // letting models appear in the ranking later with no stated reason.
        const disc = k.discovery?.queued
          ? ' Discovery is now screening that endpoint\'s free models — see "Find free models" below.'
          : '';
        this.keyMsg.set({
          text: (k.verified
            ? 'Key saved and verified working — detection will use it automatically.'
            : 'Key saved. It was not verified (no model supplied or the check failed); detection can still use it.') + disc,
          bad: false,
        });
        this.loadKeys();
        this.loadJobs();
        // Re-read what a key can now pay for: the picker lists exactly the models a key behind
        // them can answer, and a first verified key is what turns a `needs_key` row into one.
        this.refreshOptions();
      },
      error: (e) => {
        this.saving.set(false);
        // Nothing was written, so the form stays open with his paste still in it: closing it here
        // would throw away what he typed and blame him for the backend's answer.
        this.keyMsg.set({ text: this.describe(e), bad: true });
      },
    });
  }

  removeKey(id: number): void {
    this.backend.deleteMyKey(id).subscribe({
      next: () => {
        this.loadKeys();
        this.refreshOptions();
      },
      error: (e) => this.keyMsg.set({ text: this.describe(e), bad: true }),
    });
  }

  // ---- the using pool: the user's order, and what health did to it ---------
  //
  // Two different orders are on screen and the panel has to keep them apart. `your_rank` is
  // where the user put a model; the list itself is rendered in *served* order, which pushes a
  // model that stopped answering to the back. The backend derives that at read time and never
  // rewrites `position`, so a recovery restores the user's ranking with nothing to undo.

  readonly pool = signal<PoolEntry[]>([]);
  readonly poolBusy = signal(false);
  readonly poolMsg = signal<{ text: string; bad: boolean } | null>(null);
  readonly dragIndex = signal<number | null>(null);
  readonly overIndex = signal<number | null>(null);

  private loadPool(): void {
    if (!this.signedIn()) return;
    this.backend.myPool().subscribe({
      next: (r) => this.pool.set(r.pool ?? []),
      error: () => this.pool.set([]),
    });
  }

  addToPool(provider: string, model: string): void {
    this.poolBusy.set(true);
    this.poolMsg.set(null);
    this.backend.addToPool(provider, model).subscribe({
      next: (r) => {
        this.pool.set(r.pool ?? []);
        this.poolBusy.set(false);
        this.poolMsg.set({
          text: `${model} added at the end of your pool. Drag it up to make it the first tried.`,
          bad: false,
        });
        this.refreshOptions();
      },
      error: (e) => {
        this.poolBusy.set(false);
        this.poolMsg.set({ text: this.describe(e), bad: true });
      },
    });
  }

  removeFromPool(provider: string, model: string): void {
    this.poolBusy.set(true);
    this.backend.removeFromPool(provider, model).subscribe({
      next: (r) => {
        this.pool.set(r.pool ?? []);
        this.poolBusy.set(false);
        this.poolMsg.set(null);
        this.refreshOptions();
      },
      error: (e) => {
        this.poolBusy.set(false);
        this.poolMsg.set({ text: this.describe(e), bad: true });
      },
    });
  }

  /** The endpoint's display name, for a row that only knows the provider id (`pool` entries carry
      the id, not the label). Falls back to the id itself: a row from a provider the config does
      not list is still a row the user can see and remove. */
  providerLabel(provider: string): string {
    const p = this.providers().find((x) => x.name === provider);
    return p?.label || p?.name || provider;
  }

  /** Which endpoints this caller can spend right now. Read off the option rows rather than the
      stored keys, because "can pay" is what those rows already answer — `needs_key` is the backend
      saying no verified key and no admin key exists for that endpoint — and the mark must light for
      the same reason a card is addable, not for a second, slightly different rule. */
  readonly payableProviders = computed(
    () => new Set(this.options().filter((o) => !o.needs_key).map((o) => o.provider)),
  );

  /** Does this row carry the card the ranking sent him to? The address is the picker's own
      `provider|model` slug pair — the same string `data-pair` is built from — so two endpoints
      offering the same model id light one frame, not two. */
  isFocused(o: PoolOption): boolean {
    const pair = this.focusPair();
    return pair !== null && `${o.provider}|${o.model}` === pair;
  }

  /** The model he was sent for when this menu has no row for it — which is a normal state, not a
      failure: `/ranking` measures whatever answered and a card exists only for a `models` row. The
      note names the model rather than leaving a jump that lit nothing, because "the app is broken"
      is exactly what a silent arrival reads as. */
  readonly focusMiss = computed<string | null>(() => {
    const pair = this.focusPair();
    if (pair === null) return null;
    if (this.options().some((o) => `${o.provider}|${o.model}` === pair)) return null;
    const bar = pair.indexOf('|');
    return bar > 0 ? pair.slice(bar + 1) : pair;
  });

  /** Which pair has already been scrolled to, so the route centres the card once instead of
      yanking the page back to it after every later refresh of the same list. */
  private readonly scrolledPair = signal<string | null>(null);

  /** Bring the found card into view. A render effect, not one in the click handler: the row is
      created by the `@for` that the option read fills, and on a zoneless app that read can still be
      in the air when this view is first drawn — so it depends on `options()` too, and lands on the
      card as soon as it exists. The list is scanned rather than selected by an attribute value
      because a model id is whatever the provider's catalogue said it was. */
  private readonly scrollToFocus = afterRenderEffect({
    mixedReadWrite: () => {
      const pair = this.focusPair();
      this.options();
      if (pair === null || this.scrolledPair() === pair) return;
      const cards = Array.from(
        this.host.nativeElement.querySelectorAll<HTMLElement>('[data-pair]'),
      );
      const found = cards.find((c) => c.getAttribute('data-pair') === pair);
      if (!found) return;
      this.scrolledPair.set(pair);
      found.scrollIntoView({ block: 'center' });
    },
  });

  /** Every model with a `models` row, grouped by endpoint, payable endpoints first.
      Deliberately NOT filtered by addability: the menu's whole job is to say which of these he has,
      which he can get, and which he cannot pay for yet. `poolChangeOptions()` below stays the
      filtered list, because that one is a slot to fill and an unfillable choice there is a delay,
      not an answer. */
  readonly pickGroups = computed<PickGroup[]>(() => {
    const byProvider = new Map<string, PickGroup>();
    for (const o of this.options()) {
      let g = byProvider.get(o.provider);
      if (!g) {
        g = {
          provider: o.provider,
          // The endpoint's own name, joined through /config — `provider_name` on an option row is
          // the slug (the backend fills it from the same column as `provider`), and a menu that
          // says "openrouter" next to a keys page that says "OpenRouter" makes the user check
          // whether they are the same thing. Live symptom on the packaged app, 2026-10-20.
          label: this.providerLabel(o.provider),
          providerId: o.provider_id ?? null,
          payable: false,
          items: [],
        };
        byProvider.set(o.provider, g);
      }
      if (!o.needs_key) g.payable = true;
      g.items.push(o);
    }
    const groups = [...byProvider.values()];
    for (const g of groups) {
      g.items.sort((a, b) => Number(b.in_pool) - Number(a.in_pool)
        || a.model.localeCompare(b.model));
    }
    return groups.sort((a, b) => Number(b.payable) - Number(a.payable)
      || a.label.localeCompare(b.label));
  });

  /** The key form's field, so a locked card can hand the user a focused input rather than a note
      telling him to scroll and find it himself. */
  private readonly keyField = viewChild<ElementRef<HTMLInputElement>>('keyField');

  /** Send the user to the credential the card is missing: choose that endpoint in the form above,
      say what happened, and put the cursor in the field. This writes nothing — it is the same
      handoff a locked ranking card already does, one level down. */
  askKey(group: PickGroup): void {
    if (group.providerId !== null && this.providerId() !== group.providerId) {
      this.providerId.set(group.providerId);
    }
    if (!this.keyFormOpen()) {
      this.seedEndpoint();
      this.keyFormOpen.set(true);
    }
    this.keyMsg.set({
      text: `The key form is open on ${group.label} now — paste a key for it and those cards become addable.`,
      bad: false,
    });
    this.keyField()?.nativeElement?.focus();
  }

  /** Order the pool by the Ranking page's own measurement. The backend does the ranking and this
      only applies the answer: an order computed twice is how a page ends up promising a model that
      never answers, which is the defect #142 was. */
  autoRank(): void {
    this.poolBusy.set(true);
    this.poolMsg.set(null);
    this.backend.autoRankPool().subscribe({
      next: (r) => {
        this.pool.set(r.pool ?? []);
        this.poolBusy.set(false);
        // Not an error when a model could not be ranked: it is in the pool, it is just unmeasured.
        this.poolMsg.set({
          text: r.note
            || `Ordered by the measurement: ${r.ranked} ranked best first, ${r.unranked} left at the back.`,
          bad: false,
        });
        // The frames on the menu rows are about these same entries, so re-read them.
        this.refreshOptions();
      },
      error: (e) => {
        this.poolBusy.set(false);
        this.poolMsg.set({ text: this.describe(e), bad: true });
      },
    });
  }

  // ---- changing which model holds one slot --------------------------------
  //
  // "Edit the model he put in the using pool" is a replace, not a remove-plus-add: the two-call
  // version leaves the slot empty while it is in flight, and if the second call fails the user has
  // lost an entry he never meant to lose. One call keeps the rank his too, because the backend
  // rebuilds the stored order with exactly one entry swapped in place of the old one.

  /** Which row shows its editor, by "provider/model". One at a time: two open selects would make
      `changeTarget` ambiguous — and in a zoneless app a plain field could not re-check itself. */
  readonly editing = signal<string | null>(null);
  /** The chosen replacement, as "provider|model" — the pair shape `POST /my/pool/replace` takes. */
  readonly changeTarget = signal('');

  private readonly poolKeys = computed(
    () => new Set(this.pool().map((p) => `${p.provider}/${p.model}`)),
  );

  /** What a slot may be filled with, and what the pool may be grown by: a model a key can actually
      pay for that is not already in the pool. Those are exactly the two states the backend refuses,
      so listing anything else would only turn one click into one error message.
      `slow`, `busy` and `declined` stay on this list on purpose. A probe that ran out of time says
      something about the pool at that minute, not about the model, and a refusal today is not a
      deletion — the same card can be added later. The only status missing from the picker is
      `dead`, and it is missing because the backend never wrote that model's `models` row, not
      because this list filtered anything out. */
  readonly poolChangeOptions = computed<PoolOption[]>(() => {
    const held = this.poolKeys();
    const out: PoolOption[] = [];
    for (const o of this.options()) {
      if (o.needs_key || held.has(`${o.provider}/${o.model}`)) continue;
      out.push(o);
    }
    return out;
  });

  openChange(p: PoolEntry): void {
    this.editing.set(`${p.provider}/${p.model}`);
    this.changeTarget.set('');
    this.poolMsg.set(null);
  }

  cancelChange(): void {
    this.editing.set(null);
    this.changeTarget.set('');
  }

  onChangeTarget(e: Event): void {
    this.changeTarget.set((e.target as HTMLSelectElement).value);
  }

  saveChange(p: PoolEntry): void {
    const target = this.changeTarget();
    const bar = target.indexOf('|');
    if (bar <= 0 || bar === target.length - 1) return;
    const newProvider = target.slice(0, bar);
    const newModel = target.slice(bar + 1);
    this.poolBusy.set(true);
    this.poolMsg.set(null);
    this.backend.replaceInPool(p.provider, p.model, newProvider, newModel).subscribe({
      next: (r) => {
        this.pool.set(r.pool ?? []);
        this.poolBusy.set(false);
        this.cancelChange();
        // The row's health badge and the picker's list are both about this entry now.
        this.refreshOptions();
      },
      error: (e) => {
        this.poolBusy.set(false);
        // Nothing was written, so nothing moved: the row stays exactly as the user left it, and
        // the editor stays open so he can pick a different model instead of starting over.
        this.poolMsg.set({ text: this.describe(e), bad: true });
      },
    });
  }

  /** Write a new order. The list is moved locally first so the drag feels immediate, then
      confirmed from the server's answer — the response is the authority, not the guess.
      The payload is expressed in the USER's rank space: the rows are drawn in served order,
      which pushes a model that stopped answering to the back, and splicing that list would
      rewrite a demoted model's stored position — silently making the user's #1 their #3 while
      the panel promises their rank comes back untouched. With no demotion the two spaces are the
      same list, so this changes nothing a healthy pool could notice. */
  move(from: number, to: number): void {
    const rows = this.pool();
    if (from === to || from < 0 || to < 0 || from >= rows.length || to >= rows.length) return;
    const order = this.userOrderFor(from, to);
    if (!order) return;
    const next = [...rows];
    const [row] = next.splice(from, 1);
    next.splice(to, 0, row);
    this.pool.set(next);
    this.poolBusy.set(true);
    this.poolMsg.set(null);
    this.backend
      .setPoolOrder(order.map((p) => ({ provider: p.provider, model: p.model })))
      .subscribe({
        next: (r) => {
          this.pool.set(r.pool ?? []);
          this.poolBusy.set(false);
        },
        error: (e) => {
          this.poolBusy.set(false);
          this.poolMsg.set({ text: this.describe(e), bad: true });
          this.loadPool();   // the server kept the old order; show that, not the local guess
        },
      });
  }

  /** The whole pool ordered by `your_rank`, with the served-space move applied to it. */
  private userOrderFor(from: number, to: number): PoolEntry[] | null {
    const rows = this.pool();
    const key = (r: PoolEntry) => `${r.provider}/${r.model}`;
    const user = [...rows].sort((a, b) => a.your_rank - b.your_rank || a.position - b.position);
    const mi = user.findIndex((r) => key(r) === key(rows[from]));
    const ti = user.findIndex((r) => key(r) === key(rows[to]));
    if (mi < 0 || ti < 0 || mi === ti) return null;
    const [moved] = user.splice(mi, 1);
    user.splice(ti, 0, moved);
    return user;
  }

  onDragStart(i: number, e: DragEvent): void {
    this.dragIndex.set(i);
    if (e.dataTransfer) {
      e.dataTransfer.effectAllowed = 'move';
      e.dataTransfer.setData('text/plain', String(i));
    }
  }

  onDragOver(i: number, e: DragEvent): void {
    e.preventDefault();               // without this the browser refuses the drop
    if (e.dataTransfer) e.dataTransfer.dropEffect = 'move';
    this.overIndex.set(i);
  }

  onDrop(i: number, e: DragEvent): void {
    e.preventDefault();
    const from = this.dragIndex();
    this.onDragEnd();
    if (from !== null) this.move(from, i);
  }

  onDragEnd(): void {
    this.dragIndex.set(null);
    this.overIndex.set(null);
  }

  // ---- the accept-or-refuse window on a model that only answered after a retry ----------
  //
  // The screening job can state the evidence — "the first ask timed out, the second answered in
  // 3 s" — but it cannot decide whether that model belongs in the lane that scores this user's
  // texts. So it writes the row as `pending` with a deadline instead of spending his call budget
  // on a pin nobody agreed to, and this section holds the window.
  //
  // The countdown is arithmetic on the seconds the backend said were left when the list was read,
  // so a ticking clock costs no requests. Silence at the end of the window is deliberately worth
  // nothing: the offer card goes away, the model stays in the picker, and the next discovery run
  // asks it again. Neither an auto-pin nor a veto can be inferred from a page nobody looked at.

  /** Which offer is in flight, as "provider|model". Both buttons are off while any decision is
      outstanding, because two writes for one row would leave the catalogue answering twice. */
  readonly deciding = signal<string | null>(null);
  /** Bumped once a second while an offer is live. A zoneless app cannot re-check a wall clock by
      itself, so the clock is read inside the computed below and this signal is what invalidates it. */
  readonly tick = signal(0);
  /** When the current options were read — a signal for the same reason as `tick`. */
  private readonly optionsAt = signal(0);
  private tickHandle: ReturnType<typeof setTimeout> | null = null;

  readonly offers = computed(() => {
    this.tick();
    const age = Math.floor((Date.now() - this.optionsAt()) / 1000);
    const out: Array<{ key: string; o: PoolOption; left: number }> = [];
    for (const o of this.options()) {
      if (!o.offered || o.offer_seconds_left === null) continue;
      const left = o.offer_seconds_left - age;
      if (left > 0) out.push({ key: `${o.provider}|${o.model}`, o, left });
    }
    return out;
  });

  /** One timer for the whole page, and only while something is counting down. */
  private startTicking(): void {
    if (this.tickHandle !== null || this.offers().length === 0) return;
    this.tickHandle = setTimeout(() => {
      this.tickHandle = null;
      this.tick.update((t) => t + 1);
      if (this.offers().length) this.startTicking();
      else this.loadOptions();   // the window closed: show what the catalogue says now
    }, 1000);
  }

  /** Minutes and seconds: a window measured in bare seconds next to a "600" would be read as
      milliseconds, which is the wrong unit and the wrong urgency. */
  countdown(left: number): string {
    const m = Math.floor(left / 60);
    const s = left % 60;
    return m > 0 ? `${m}:${String(s).padStart(2, '0')}` : `${s} s`;
  }

  seconds(ms: number): string {
    return (ms / 1000).toFixed(1);
  }

  decide(o: PoolOption, decision: 'accept' | 'refuse'): void {
    this.deciding.set(`${o.provider}|${o.model}`);
    this.poolMsg.set(null);
    this.backend.catalogDecision(o.provider, o.model, decision).subscribe({
      next: (r) => {
        this.deciding.set(null);
        this.pool.set(r.pool ?? []);
        this.poolMsg.set({
          text: decision === 'accept'
            ? `${o.model} is in your pool at the end — drag it up to make it the first tried.`
            : `${o.model} is not in your pool. It stays pickable below, so this is a not-now.`,
          bad: false,
        });
        this.loadOptions();
      },
      error: (e) => {
        this.deciding.set(null);
        this.poolMsg.set({ text: this.describe(e), bad: true });
      },
    });
  }

  /** Re-read the pickable list after anything that changes what may be offered. It deliberately
      does NOT re-read the pool: every mutation already sets `pool` from the server's own answer,
      and a second read behind that only invites a stale list to overwrite the confirmation. */
  private refreshOptions(): void {
    this.loadOptions();
  }

  // ---- discovery: bring the free models this key can reach ----------------

  readonly jobs = signal<DiscoveryJob[]>([]);
  readonly discoveryNote = signal('');
  readonly screenTexts = signal('2');
  readonly discovering = signal<number | null>(null);

  private pollHandle: ReturnType<typeof setTimeout> | null = null;
  private readonly finishedSeen = new Set<number>();

  /** Endpoints the caller has a key for. Discovery is offered only on those: it runs on the
      caller's credential, so a provider without one can only answer 409. */
  readonly discoverable = computed(() => {
    const owned = new Set(this.keys().map((k) => k.provider_id));
    return this.providers().filter((p) => owned.has(p.id));
  });

  discover(providerId: number): void {
    this.discovering.set(providerId);
    this.poolMsg.set(null);
    this.backend.discover(providerId).subscribe({
      next: (r) => {
        this.discovering.set(null);
        // `queued: false` is not a failure to report as one, and not a success to hide: the
        // backend refused to write a job no worker would pick up, and says why.
        this.poolMsg.set({
          text: r.queued
            ? 'Screening started. Each model is run on 2 real texts through the detection engine, so this takes a few minutes.'
            : `Not queued: ${r.reason || r.error || r.status}`,
          bad: !r.queued,
        });
        this.loadJobs();
      },
      error: (e) => {
        this.discovering.set(null);
        this.poolMsg.set({ text: this.describe(e), bad: true });
      },
    });
  }

  private loadJobs(): void {
    if (!this.signedIn()) return;
    this.backend.discoveryStatus().subscribe({
      next: (s) => {
        this.jobs.set(s.jobs ?? []);
        this.discoveryNote.set(s.note || '');
        if (s.screen_texts) this.screenTexts.set(s.screen_texts);
        // A finished job may have added a pool row and put an offer on this page: re-read both
        // once per job, not on every poll. This is the one place a pool re-read belongs after a
        // change, because here the job — not this user's click — is what mutated it.
        let newlyDone = false;
        for (const j of s.jobs ?? []) {
          if ((j.status === 'done' || j.status === 'failed') && !this.finishedSeen.has(j.id)) {
            this.finishedSeen.add(j.id);
            if (j.status === 'done') newlyDone = true;
          }
        }
        if (newlyDone) {
          this.loadPool();
          this.refreshOptions();
        }
        this.schedulePoll(s.jobs ?? []);
      },
      error: () => this.schedulePoll([]),
    });
  }

  /** Poll only while something can still change. An idle page that keeps asking every four
      seconds is a request budget spent on nothing. */
  private schedulePoll(jobs: DiscoveryJob[]): void {
    if (this.pollHandle !== null) {
      clearTimeout(this.pollHandle);
      this.pollHandle = null;
    }
    const pending = jobs.some((j) => j.status === 'queued' || j.status === 'running');
    if (!pending) return;
    this.pollHandle = setTimeout(() => this.loadJobs(), 4_000);
  }

  ngOnDestroy(): void {
    if (this.pollHandle !== null) {
      clearTimeout(this.pollHandle);
      this.pollHandle = null;
    }
    if (this.tickHandle !== null) {
      clearTimeout(this.tickHandle);
      this.tickHandle = null;
    }
  }

  private describe(err: unknown): string {
    if (err instanceof HttpErrorResponse) {
      const body = err.error as { error?: string; message?: string } | null;
      const text = body && typeof body === 'object' ? (body.error ?? body.message) : '';
      if (err.status === 0) return 'Cannot reach the backend on /api.';
      return text || `HTTP ${err.status}`;
    }
    return String(err);
  }
}
