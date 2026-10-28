// Webview: haelt den Zustand per Reduzierer, rendert per DOM-API. Kein Framework, kein innerHTML.
import { initialState, openQuestions, reduce } from '../shared/reducer.ts';
import type { Action, FlowInfo, Item, QuestionItem, State, SubInfo, ToolItem } from '../shared/reducer.ts';
import { renderMarkdown } from './render-markdown.ts';
import { startSign } from './sign.ts';
import { initSettings } from './settings.ts';
import { initSessions } from './sessions.ts';
import { normalizeAgents, normalizeFields, normalizeNotes, normalizeSession } from '../shared/protocol.ts';
import type { SessionInfo } from '../shared/protocol.ts';
import { CHIP_FIELDS, CHIP_TITLE, askPreview, chipValue, emptyChoices, isCurrent, nextMode, toolDoing, toolSummary } from '../shared/views.ts';
import type { ChipField, Choices } from '../shared/views.ts';

type Api = { postMessage(m: unknown): void; getState(): unknown; setState(s: unknown): void };
declare function acquireVsCodeApi(): Api;

const api = acquireVsCodeApi();
let state: State = initialState();

const $ = (id: string): HTMLElement => document.getElementById(id) as HTMLElement;
const scrollEl = $('scroll');
const logEl = $('log');
const asksEl = $('asks');
const bannerEl = $('banner');
const noteEl = $('note');
const inputEl = $('input') as HTMLTextAreaElement;
const sendEl = $('send');
const metaEl = $('meta');
const slEl = $('statusline');
const subsEl = $('subs');
const openSubs = new Set<string>();
const sigEl = $('sig');
const snameEl = $('sname');
const followEl = $('b-follow');
const sendIcon: Node[] = Array.from(sendEl.childNodes).map((n) => n.cloneNode(true));

function h<K extends keyof HTMLElementTagNameMap>(tag: K, cls?: string, text?: string): HTMLElementTagNameMap[K] {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function post(m: Record<string, unknown>): void {
  api.postMessage(m);
}

const appEl = $('app');
const sign = startSign($('sign') as HTMLCanvasElement);
const settings = initSettings(post, (open) => {
  appEl.dataset.view = open ? 'settings' : 'chat';
  if (!open) schedule();
});
const sessionsView = initSessions(
  post,
  () => state.session?.id ?? null,
  (open) => {
    appEl.dataset.view = open ? 'sessions' : 'chat';
    if (!open) schedule();
  },
);
function showChat(): void {
  if (settings.isOpen()) settings.close();
  if (sessionsView.isOpen()) sessionsView.close();
  inputEl.focus({ preventScroll: true });
}

// ---- Entwurf merken ----
try {
  const saved = api.getState() as { draft?: string } | undefined;
  if (saved && typeof saved.draft === 'string') inputEl.value = saved.draft;
} catch {
  // ohne gespeicherten Entwurf weiter
}
function saveDraft(): void {
  try {
    api.setState({ draft: inputEl.value });
  } catch {
    // egal
  }
}

// ---- Rendering ----
const expanded = new Set<string>();
const cache = new Map<number, { item: Item; node: HTMLElement }>();
// A run of tool calls is one block, closed unless opened: by its header, or all at once with Alt+T.
const groupState = new Map<number, boolean>();
let toolsAllOpen = false;
const groupCache = new Map<number, { items: ToolItem[]; open: boolean; node: HTMLElement }>();

function money(x: number): string {
  return x.toFixed(x < 0.1 ? 4 : 2);
}

function fileName(p: string): string {
  const parts = p.split(/[\\/]/);
  return parts[parts.length - 1] || p;
}

const IJ_LABEL: Record<'pending' | 'delivered' | 'queued' | 'sent' | 'dropped', string> = {
  pending: 'waiting for the next step',
  delivered: 'read by the agent',
  queued: 'queued',
  sent: 'sent',
  dropped: 'not read: stopped',
};

function renderUser(it: Extract<Item, { k: 'user' }>): HTMLElement {
  const d = h('div', 'um');
  d.appendChild(h('div', 'wh', 'You'));
  d.appendChild(h('div', 'tx', it.text));
  if (it.ij) d.appendChild(h('div', `ij ij-${it.ij.state}`, IJ_LABEL[it.ij.state]));
  return d;
}

function renderAssistant(it: Extract<Item, { k: 'assistant' }>): HTMLElement {
  const d = h('div', it.open ? 'am live-text' : 'am');
  d.appendChild(h('div', 'wh', 'Lightmind'));
  d.appendChild(renderMarkdown(it.text));
  return d;
}

const TOOL_LABEL: Record<ToolItem['status'], string> = { run: 'running', done: 'done', fail: 'failed', stop: 'stopped' };

function renderTool(it: ToolItem): HTMLElement {
  const card = h('div', 'tc');
  card.dataset.s = it.status === 'fail' ? 'deny' : it.status;
  card.dataset.call = it.call;
  const row = h('div', 'tcr');
  const head = h('button', 'tch');
  head.type = 'button';
  head.dataset.act = 'toggle';
  head.dataset.call = it.call;
  const open = expanded.has(it.call);
  head.setAttribute('aria-expanded', String(open));
  head.appendChild(h('span', 'ic'));
  head.appendChild(h('b', undefined, it.name));
  head.appendChild(h('span', 'arg', it.summary));
  head.appendChild(h('span', 'mt', TOOL_LABEL[it.status]));
  head.appendChild(h('span', 'cv'));
  row.appendChild(head);
  if (it.status === 'done' && it.changed && it.files.length) {
    const od = h('button', 'od', 'Open diff');
    od.type = 'button';
    od.dataset.act = 'diff';
    od.dataset.call = it.call;
    od.dataset.i = '0';
    row.appendChild(od);
  }
  card.appendChild(row);
  const body = h('div', 'tcb');
  body.hidden = !open;
  body.appendChild(h('div', 'tsum', it.summary));
  it.files.forEach((f, i) => {
    const b = h('button', 'fl', fileName(f));
    b.type = 'button';
    b.title = f;
    b.dataset.act = 'file';
    b.dataset.call = it.call;
    b.dataset.i = String(i);
    body.appendChild(b);
  });
  card.appendChild(body);
  return card;
}

function renderToolGroup(items: ToolItem[], open: boolean): HTMLElement {
  const g = h('div', 'tg');
  const head = h('button', 'tgh');
  head.type = 'button';
  head.dataset.act = 'group';
  head.dataset.g = String(items[0]!.id);
  head.setAttribute('aria-expanded', String(open));
  head.appendChild(h('span', 'cv'));
  head.appendChild(h('span', 'tgs', toolSummary(items.map((t) => t.name))));
  const failed = items.filter((t) => t.status === 'fail').length;
  if (failed) head.appendChild(h('span', 'tgx', `${failed} failed`));
  const now = items.filter((t) => t.status === 'run').pop();
  if (now) head.appendChild(h('span', 'tgn', toolDoing(now.name)));
  g.appendChild(head);
  // Collapsed, a failed or denied call stays in view; everything else waits in the closed body.
  const shown = open ? [] : items.filter((t) => t.status === 'fail');
  const body = h('div', 'tgb');
  body.hidden = !open;
  for (const t of open ? items : items.filter((x) => !shown.includes(x))) body.appendChild(renderTool(t));
  g.appendChild(body);
  if (shown.length) {
    const f = h('div', 'tgf');
    for (const t of shown) f.appendChild(renderTool(t));
    g.appendChild(f);
  }
  return g;
}

function renderQuestion(it: QuestionItem): HTMLElement {
  if (it.answered !== null) {
    const a = h('div', 'ans');
    a.dataset.a = /^(no|deny|\(closed\))$/i.test(it.answered) ? 'no' : 'yes';
    a.appendChild(h('span', undefined, `${it.text}  `));
    a.appendChild(h('b', undefined, it.answered));
    return a;
  }
  const c = h('div', 'ask');
  c.setAttribute('role', 'group');
  c.setAttribute('aria-label', it.kind === 'permission' ? 'Permission request' : 'Question');
  c.dataset.q = it.question;
  c.dataset.t = String(Date.now());
  c.appendChild(h('div', 'ah', it.kind === 'permission' ? 'Permission needed' : 'Lightmind asks'));
  // The text scrolls inside .abody (max-height, see main.css); the buttons below stay in view however long it is.
  const body = h('div', 'abody');
  body.tabIndex = 0;
  body.setAttribute('aria-label', 'Request details');
  // The first line is the plain sentence, the lines after it say why it is asked; the raw command is folded away.
  const [first, ...rest] = it.text.split('\n');
  body.appendChild(h('div', 'aq', first));
  if (rest.length) body.appendChild(h('div', 'areason', rest.join('\n')));
  if (it.detail) {
    const pv = askPreview(it.detail);
    const det = h('details', 'adet');
    det.appendChild(h('summary', undefined, `Show the command (${pv.chars} characters)`));
    det.appendChild(h('pre', 'acmd', pv.text + (pv.cut ? `\n… shortened in the middle, ${pv.chars} characters in all` : '')));
    body.appendChild(det);
  }
  c.appendChild(body);
  const bt = h('div', 'abt');
  const perm = it.kind === 'permission';
  const labels: Record<string, string> = { yes: 'Yes', always: 'Always', no: 'No' };
  for (const ch of it.choices) {
    const b = h('button', perm ? `btn ${ch === 'no' ? 'btn-sec' : ch === 'always' ? 'btn-alt' : 'btn-pri'}` : 'btn btn-sec', perm ? labels[ch] || ch : ch);
    b.type = 'button';
    b.dataset.act = 'answer';
    b.dataset.q = it.question;
    b.dataset.c = ch;
    bt.appendChild(b);
  }
  c.appendChild(bt);
  if (!perm) {
    const f = h('div', 'free');
    const inp = h('input');
    inp.type = 'text';
    inp.placeholder = it.choices.length ? 'Or type your own answer' : 'Type your answer';
    inp.setAttribute('aria-label', 'Your answer');
    inp.dataset.q = it.question;
    const go = h('button', 'btn btn-pri', 'Answer');
    go.type = 'button';
    go.dataset.act = 'free';
    go.dataset.q = it.question;
    f.appendChild(inp);
    f.appendChild(go);
    c.appendChild(f);
  }
  return c;
}

function renderTurnEnd(it: Extract<Item, { k: 'turnend' }>): HTMLElement {
  const parts: string[] = [it.status === 'done' ? 'Done' : it.status === 'cancelled' ? 'Stopped' : 'Failed'];
  if (it.cost > 0) parts.push(`$${money(it.cost)}`);
  if (it.context !== null) parts.push(`context ${Math.round(it.context * 100)}%`);
  const d = h('div', 'tend', parts.join('  ·  '));
  d.dataset.s = it.status;
  return d;
}

function build(it: Item): HTMLElement {
  switch (it.k) {
    case 'user':
      return renderUser(it);
    case 'assistant':
      return renderAssistant(it);
    case 'tool':
      return renderTool(it);
    case 'question':
      return renderQuestion(it);
    case 'turnend':
      return renderTurnEnd(it);
    case 'notice': {
      const d = h('div', it.level === 'warn' ? 'sysl warn' : 'sysl', it.text);
      return d;
    }
    case 'submsg': {
      // Ergebnis eines Hintergrund-Unteragenten: eigene Zeile, aufklappbar, nie als "You".
      const d = document.createElement('details');
      d.className = it.ok ? 'submsg' : 'submsg fail';
      const head = document.createElement('summary');
      head.appendChild(h('span', 'dm', '◆'));
      head.appendChild(h('b', undefined, it.name));
      head.appendChild(h('span', 'st', it.ok ? 'finished' : 'failed'));
      head.appendChild(h('span', 'sn', it.summary.replace(/\s+/g, ' ').slice(0, 90)));
      d.appendChild(head);
      const body = h('div', 'smb');
      body.appendChild(h('div', 'sml', it.summary));
      if (it.file) body.appendChild(h('div', 'sml sf', 'Full answer: ' + it.file));
      d.appendChild(body);
      return d;
    }
    case 'error': {
      const d = h('div', 'errl');
      d.setAttribute('role', 'alert');
      d.textContent = it.text;
      return d;
    }
  }
}

function isOpenAsk(it: Item): it is QuestionItem {
  return it.k === 'question' && it.answered === null;
}

/** Open requests stay pinned above the input, below all messages, until answered. */
function renderAsks(): void {
  const open = openQuestions(state);
  const nodes: HTMLElement[] = [];
  for (const it of open) {
    let c = cache.get(it.id);
    if (!c || c.item !== it) {
      c = { item: it, node: build(it) };
      cache.set(it.id, c);
    }
    nodes.push(c.node);
  }
  let i = 0;
  for (const n of nodes) {
    if (asksEl.children[i] !== n) asksEl.insertBefore(n, asksEl.children[i] || null);
    i++;
  }
  while (asksEl.children.length > nodes.length) asksEl.removeChild(asksEl.lastChild as Node);
  asksEl.hidden = nodes.length === 0;
}

function groupNode(items: ToolItem[]): HTMLElement {
  const id = items[0]!.id;
  const open = groupState.get(id) ?? toolsAllOpen;
  let c = groupCache.get(id);
  if (!c || c.open !== open || c.items.length !== items.length || c.items.some((t, i) => t !== items[i])) {
    c = { items, open, node: renderToolGroup(items, open) };
    groupCache.set(id, c);
  }
  return c.node;
}

function renderLog(): void {
  const focusedGroup = document.activeElement instanceof HTMLElement && document.activeElement.classList.contains('tgh') ? document.activeElement.dataset.g : undefined;
  const nodes: HTMLElement[] = [];
  const live = new Set<number>();
  let run: ToolItem[] = [];
  const flush = (): void => {
    if (run.length) nodes.push(groupNode(run));
    run = [];
  };
  for (const it of state.items) {
    live.add(it.id);
    if (it.k === 'tool') {
      run.push(it);
      continue;
    }
    if (isOpenAsk(it)) continue; // open requests live in the pinned area, see renderAsks
    flush();
    let c = cache.get(it.id);
    if (!c || c.item !== it) {
      c = { item: it, node: build(it) };
      cache.set(it.id, c);
    }
    nodes.push(c.node);
  }
  flush();
  for (const id of cache.keys()) if (!live.has(id)) cache.delete(id);
  for (const id of groupCache.keys()) if (!live.has(id)) groupCache.delete(id);
  // Reihenfolge angleichen, unveraenderte Knoten bleiben stehen
  let i = 0;
  for (const n of nodes) {
    if (logEl.children[i] !== n) logEl.insertBefore(n, logEl.children[i] || null);
    i++;
  }
  while (logEl.children.length > nodes.length) logEl.removeChild(logEl.lastChild as Node);
  // A header that had the focus was rebuilt with its block: a keyboard user keeps their place.
  if (focusedGroup !== undefined && !(document.activeElement instanceof HTMLElement && document.activeElement.classList.contains('tgh'))) {
    logEl.querySelector<HTMLElement>(`.tgh[data-g="${focusedGroup}"]`)?.focus({ preventScroll: true });
  }
}

function renderBanner(): void {
  bannerEl.textContent = '';
  const c = state.conn;
  if (c.status === 'ready') {
    if (!state.session && state.items.length === 0) {
      const e = h('div', 'empty');
      e.appendChild(h('b', undefined, 'No session open'));
      e.appendChild(h('div', undefined, 'Type a message to start a new session, or pick one.'));
      const row = h('div', 'abt center');
      const n = h('button', 'btn btn-pri', 'New session');
      n.type = 'button';
      n.dataset.act = 'new';
      const l = h('button', 'btn btn-sec', 'Open session');
      l.type = 'button';
      l.dataset.act = 'list';
      row.appendChild(n);
      row.appendChild(l);
      e.appendChild(row);
      bannerEl.appendChild(e);
    }
    return;
  }
  const e = h('div', c.status === 'down' ? 'empty bad' : 'empty');
  e.setAttribute('role', 'status');
  const title = c.status === 'starting' ? 'Starting Lightmind...' : c.status === 'restarting' ? 'Reconnecting...' : 'Lightmind is not running';
  e.appendChild(h('b', undefined, title));
  if (c.detail) e.appendChild(h('div', undefined, c.detail));
  if (c.status === 'down') {
    const row = h('div', 'abt center');
    const r = h('button', 'btn btn-pri', 'Retry');
    r.type = 'button';
    r.dataset.act = 'restart';
    row.appendChild(r);
    e.appendChild(row);
  }
  bannerEl.appendChild(e);
}

function renderChrome(): void {
  const s = state.session;
  snameEl.textContent = s ? s.name : 'Lightmind';
  const run = state.busy;
  sigEl.dataset.s = state.conn.status !== 'ready' ? 'pause' : run ? 'run' : 'done';
  metaEl.textContent = '';
  if (s) {
    const bits = [s.agent, s.origin ? `from ${s.origin}` : '', s.project ? `project ${s.project}` : ''].filter(Boolean);
    metaEl.textContent = bits.join('  ·  ');
  }
  sign.setMode(state.status.mode);
  sign.setBusy(state.busy);
  followEl.setAttribute('aria-pressed', String(state.follow));
  followEl.title = state.follow ? 'Following the agent in the editor (click to turn off)' : 'Follow the agent in the editor (click to turn on)';
  followEl.classList.toggle('on', state.follow);

  const readonly = !!s?.readonly;
  noteEl.hidden = !(state.busy || state.cancelling || readonly);
  noteEl.className = readonly && !state.busy ? 'nt ro' : 'nt wk';
  noteEl.textContent = readonly && !state.busy ? 'Open elsewhere: you can read this session here, not write to it.' : state.cancelling ? 'Stopping...' : 'Working... you can write to the agent';

  const ready = state.conn.status === 'ready';
  const has = inputEl.value.trim().length > 0 && !state.session?.readonly;
  sendEl.textContent = '';
  if (state.busy) {
    sendEl.className = 'sendb stop';
    sendEl.title = 'Stop';
    sendEl.setAttribute('aria-label', 'Stop');
    sendEl.setAttribute('aria-disabled', 'false');
    sendEl.dataset.mode = 'stop';
    sendEl.appendChild(h('span', 'sq'));
    sendEl.appendChild(document.createTextNode('Stop'));
  } else {
    sendEl.className = 'sendb';
    sendEl.title = 'Send';
    sendEl.setAttribute('aria-label', 'Send');
    sendEl.setAttribute('aria-disabled', String(!(ready && has)));
    sendEl.dataset.mode = 'send';
    for (const n of sendIcon) sendEl.appendChild(n.cloneNode(true));
  }
  inputEl.disabled = !ready || readonly;
  inputEl.placeholder = readonly ? 'Read only: this session is open elsewhere' : state.busy ? 'Write to the agent while it works (Enter)' : 'Ask Lightmind  (Enter sends)';

  slEl.textContent = '';
  const st = state.status;
  const left = h('span', 'sl-l');
  const add = (label: string, val: string | null, mode?: string): void => {
    if (!val) return;
    const sp = h('span', 'sel');
    if (mode) sp.dataset.val = mode;
    sp.appendChild(h('span', 'lb', label + ' '));
    sp.appendChild(h('span', 'v', val));
    left.appendChild(sp);
  };
  add('project', st.project ?? (s ? s.project : null));
  slEl.appendChild(left);
  const us = h('span', 'us');
  if (state.context !== null) {
    us.appendChild(h('span', 'lb', 'ctx '));
    const gg = h('span', 'gg');
    const bar = h('i');
    bar.style.width = `${Math.round(state.context * 100)}%`;
    gg.appendChild(bar);
    if (state.context > 0.8) gg.dataset.hi = '1';
    gg.setAttribute('role', 'img');
    gg.setAttribute('aria-label', `Context ${Math.round(state.context * 100)} percent used`);
    us.appendChild(gg);
    us.appendChild(h('b', undefined, `${Math.round(state.context * 100)}%`));
  }
  if (state.cost > 0) us.appendChild(h('span', 'cost', `  $${money(state.cost)}`));
  slEl.appendChild(us);
}

// ---- Auswahlchips unter dem Eingabefeld: Modell, Aufwand, Modus ----
const chipsEl = $('chips');
const popEl = $('pop');
const dockEl = document.querySelector('.dock') as HTMLElement;
let choices: Choices = emptyChoices();
let choicesAsked = false;
let openChip: ChipField | null = null;
let chipSig = '';

function chipValues(): Record<ChipField, string | null> {
  const st = state.status;
  const s = state.session;
  const status = { model: st.model, effort: st.effort, mode: st.mode };
  return {
    model: chipValue('model', status, s ? s.model : null, choices),
    effort: chipValue('effort', status, null, choices),
    mode: chipValue('mode', status, null, choices),
  };
}

function chipEl(f: ChipField): HTMLElement | null {
  return chipsEl.querySelector(`.chip[data-field="${f}"]`);
}

function closePop(refocus: boolean): void {
  const f = openChip;
  openChip = null;
  popEl.hidden = true;
  popEl.textContent = '';
  if (f) {
    chipEl(f)?.setAttribute('aria-expanded', 'false');
    if (refocus) chipEl(f)?.focus({ preventScroll: true });
  }
}

function renderPop(): void {
  const f = openChip;
  if (!f) return;
  const chip = chipEl(f);
  if (!chip) {
    closePop(false);
    return;
  }
  const cur = chipValues()[f];
  popEl.textContent = '';
  popEl.setAttribute('aria-label', CHIP_TITLE[f]);
  popEl.appendChild(h('div', 'pt', CHIP_TITLE[f]));
  const list = choices[f];
  const why = f === 'mode' && choices.modeError ? choices.modeError : '';
  if (why) popEl.appendChild(h('div', 'pn', why));
  else if (!list.length) popEl.appendChild(h('div', 'pn', choices.error ? `The choices could not be loaded: ${choices.error}` : 'The backend offers no choices.'));
  for (const v of list) {
    const b = h('button', f === 'mode' ? `po m-${v}` : 'po');
    b.type = 'button';
    b.setAttribute('role', 'option');
    b.setAttribute('aria-selected', String(isCurrent(f, v, cur)));
    b.dataset.field = f;
    b.dataset.value = v;
    if (f === 'mode') b.appendChild(h('i', 'ring'));
    b.appendChild(h('span', 'pv', v));
    popEl.appendChild(b);
  }
  chip.setAttribute('aria-expanded', 'true');
  popEl.hidden = false;
  const dr = dockEl.getBoundingClientRect();
  const cr = chip.getBoundingClientRect();
  popEl.style.left = `${Math.max(0, Math.min(cr.left - dr.left, dr.width - popEl.offsetWidth))}px`;
  popEl.style.bottom = `${Math.round(dr.bottom - cr.top + 4)}px`;
}

function renderChips(): void {
  const vals = chipValues();
  const ready = state.conn.status === 'ready';
  const ro = !!state.session?.readonly;
  const sig = JSON.stringify([vals, ready, ro, openChip]);
  if (sig !== chipSig) {
    chipSig = sig;
    const hadFocus = chipsEl.contains(document.activeElement) ? (document.activeElement as HTMLElement).dataset.field : null;
    chipsEl.textContent = '';
    for (const f of CHIP_FIELDS) {
      const v = vals[f];
      const b = h('button', v ? 'chip' : 'chip unset');
      b.type = 'button';
      b.dataset.field = f;
      if (f === 'mode' && v) b.dataset.val = v;
      b.setAttribute('aria-haspopup', 'listbox');
      b.setAttribute('aria-expanded', String(openChip === f));
      b.setAttribute('aria-label', `${CHIP_TITLE[f]}: ${v ?? 'not set'}`);
      b.title = ro ? 'This session is read only here' : `${CHIP_TITLE[f]}: ${v ?? 'not set'}. Applies to ${state.session ? 'this session' : 'the next new session'}.`;
      b.disabled = !ready || ro;
      b.appendChild(h('span', 'cvl', v ?? f));
      b.appendChild(h('i', 'cch'));
      chipsEl.appendChild(b);
    }
    if (hadFocus) chipEl(hadFocus as ChipField)?.focus({ preventScroll: true });
    if (openChip) renderPop();
  }
  if (ready && !choicesAsked) {
    choicesAsked = true;
    post({ type: 'choicesGet' });
  } else if (!ready) choicesAsked = false;
}

function renderFlow(w: FlowInfo): void {
  // Ein Workflow als eine Zeile: Name, aktuelle Phase, wie viele seiner Agenten laufen, fertig oder gescheitert sind.
  const mine = state.subs.filter((b) => b.workflow === w.id);
  const count = (f: (b: SubInfo) => boolean): number => mine.filter(f).length;
  const parts = [`${count((b) => !b.done)} running`, `${count((b) => b.done && b.ok)} done`];
  const failed = count((b) => b.done && !b.ok);
  if (failed) parts.push(`${failed} failed`);
  const line = h('div', 'subrow');
  const row = h('div', 'sub flow');
  row.dataset.s = w.done ? (w.ok ? 'done' : 'fail') : 'run';
  row.appendChild(h('span', 'ic'));
  row.appendChild(h('b', undefined, w.name));
  row.appendChild(h('span', 'arg', [w.phase, parts.join(' · '), w.done ? w.summary : w.log].filter(Boolean).join(' · ')));
  line.appendChild(row);
  if (!w.done) {
    const stop = h('button', 'substop', 'Stop');
    stop.type = 'button';
    stop.dataset.act = 'subcancel';
    stop.dataset.id = w.id;
    stop.setAttribute('aria-label', 'Stop workflow ' + w.name);
    line.appendChild(stop);
  }
  subsEl.appendChild(line);
}

function renderSubs(): void {
  subsEl.textContent = '';
  for (const w of state.flows) renderFlow(w);
  for (const b of state.subs.filter((x) => !x.workflow).slice(-20)) {
    const open = openSubs.has(b.id);
    const line = h('div', 'subrow');
    const row = h('button', 'sub');
    row.type = 'button';
    row.dataset.s = b.done ? (b.ok ? 'done' : 'fail') : 'run';
    row.dataset.act = 'sub';
    row.dataset.id = b.id;
    row.setAttribute('aria-expanded', String(open));
    row.appendChild(h('span', 'ic'));
    row.appendChild(h('b', undefined, b.name));
    row.appendChild(h('span', 'arg', b.task));
    row.appendChild(h('span', 'cv'));
    line.appendChild(row);
    if (!b.done) {
      const stop = h('button', 'substop', 'Stop');
      stop.type = 'button';
      stop.dataset.act = 'subcancel';
      stop.dataset.id = b.id;
      stop.setAttribute('aria-label', 'Stop sub-agent ' + b.name);
      line.appendChild(stop);
    }
    subsEl.appendChild(line);
    if (open) {
      const body = h('div', 'subb');
      for (const l of b.log) body.appendChild(h('div', 'subl', l));
      if (b.done && b.summary) body.appendChild(h('div', 'subl sum', b.summary));
      if (!body.childNodes.length) body.appendChild(h('div', 'subl', 'No steps yet.'));
      subsEl.appendChild(body);
    }
  }
}

let queued = false;
function render(): void {
  queued = false;
  const keep = scrollEl.scrollTop;
  const atBottom = scrollEl.scrollHeight - keep - scrollEl.clientHeight < 48;
  renderBanner();
  renderLog();
  renderAsks();
  renderChrome();
  renderChips();
  renderSubs();
  // A reader who scrolled up keeps the exact position, node swaps must not move it.
  scrollEl.scrollTop = atBottom ? scrollEl.scrollHeight : keep;
}
function schedule(): void {
  if (queued) return;
  queued = true;
  requestAnimationFrame(render);
}
function apply(a: Action): void {
  state = reduce(state, a);
  schedule();
}

// ---- Eingaben ----
function autosize(): void {
  inputEl.style.height = 'auto';
  inputEl.style.height = `${Math.min(168, Math.max(36, inputEl.scrollHeight))}px`;
}

function trySend(): void {
  const t = inputEl.value.trim();
  if (!t || state.session?.readonly || state.conn.status !== 'ready') return;
  post({ type: 'send', text: t });
  inputEl.value = '';
  saveDraft();
  autosize();
  schedule();
}

inputEl.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    trySend();
  }
});
inputEl.addEventListener('input', () => {
  autosize();
  saveDraft();
  schedule();
});
sendEl.addEventListener('click', () => {
  if (sendEl.dataset.mode === 'stop') post({ type: 'cancel' });
  else trySend();
});
$('b-new').addEventListener('click', () => post({ type: 'newSession' }));
$('b-list').addEventListener('click', () => sessionsView.open());
$('b-settings').addEventListener('click', () => settings.open());
followEl.addEventListener('click', () => post({ type: 'toggleFollow' }));

function answerFree(q: string, root: HTMLElement): void {
  const inp = root.querySelector('input');
  const v = inp instanceof HTMLInputElement ? inp.value.trim() : '';
  if (v) post({ type: 'answer', question: q, choice: v });
}

function onAct(t: HTMLElement): void {
  const act = t.dataset.act;
  switch (act) {
    case 'toggle': {
      const call = t.dataset.call || '';
      if (expanded.has(call)) expanded.delete(call);
      else expanded.add(call);
      const open = expanded.has(call);
      t.setAttribute('aria-expanded', String(open));
      const body = t.closest('.tc')?.querySelector('.tcb');
      if (body instanceof HTMLElement) body.hidden = !open;
      break;
    }
    case 'group': {
      const id = Number(t.dataset.g || 0);
      groupState.set(id, !(groupState.get(id) ?? toolsAllOpen));
      schedule();
      break;
    }
    case 'answer':
      post({ type: 'answer', question: t.dataset.q || '', choice: t.dataset.c || '' });
      break;
    case 'free': {
      const root = t.closest('.ask');
      if (root instanceof HTMLElement) answerFree(t.dataset.q || '', root);
      break;
    }
    case 'diff':
      post({ type: 'openDiff', call: t.dataset.call || '', index: Number(t.dataset.i || 0) });
      break;
    case 'file':
      post({ type: 'openFile', call: t.dataset.call || '', index: Number(t.dataset.i || 0) });
      break;
    case 'copy': {
      const code = t.closest('.cb')?.querySelector('code');
      const text = code?.textContent || '';
      if (text) post({ type: 'copy', text });
      break;
    }
    case 'subcancel':
      post({ type: 'subCancel', id: t.dataset.id || '' });
      break;
    case 'sub': {
      const id = t.dataset.id || '';
      if (openSubs.has(id)) openSubs.delete(id);
      else openSubs.add(id);
      schedule();
      break;
    }
    case 'new':
      post({ type: 'newSession' });
      break;
    case 'list':
      sessionsView.open();
      break;
    case 'restart':
      post({ type: 'restart' });
      break;
  }
}

document.addEventListener('click', (e) => {
  const target = e.target instanceof Element ? e.target : null;
  if (!target) return;
  const po = target.closest('.po');
  if (po instanceof HTMLElement && po.dataset.field && po.dataset.value) {
    post({ type: 'sessionSet', field: po.dataset.field, value: po.dataset.value });
    closePop(true);
    return;
  }
  const chip = target.closest('.chip');
  if (chip instanceof HTMLElement && chip.dataset.field) {
    const f = chip.dataset.field as ChipField;
    if (openChip === f) closePop(true);
    else {
      closePop(false);
      openChip = f;
      chipSig = '';
      renderChips();
      renderPop();
      ((popEl.querySelector('[aria-selected=true]') ?? popEl.querySelector('.po')) as HTMLElement | null)?.focus({ preventScroll: true });
    }
    return;
  }
  if (openChip && !target.closest('#pop')) closePop(false);
  const lnk = target.closest('a.lnk');
  if (lnk instanceof HTMLElement && lnk.dataset.url) {
    e.preventDefault();
    post({ type: 'openLink', url: lnk.dataset.url });
    return;
  }
  const t = target.closest('[data-act]');
  if (t instanceof HTMLElement) onAct(t);
});
function cycleMode(): void {
  const next = nextMode(choices.mode, chipValues().mode);
  if (next && state.conn.status === 'ready' && !state.session?.readonly) post({ type: 'sessionSet', field: 'mode', value: next });
}

document.addEventListener('keydown', (e) => {
  const target = e.target instanceof Element ? e.target : null;
  if (!target) return;
  // Alt+T: every run of tool calls open, or every one closed.  Shift+Tab in the message box (Alt+M anywhere): the next mode.
  if (e.altKey && !e.ctrlKey && !e.metaKey && e.code === 'KeyT') {
    e.preventDefault();
    toolsAllOpen = !toolsAllOpen;
    groupState.clear();
    schedule();
    return;
  }
  if ((e.altKey && !e.ctrlKey && !e.metaKey && e.code === 'KeyM') || (e.key === 'Tab' && e.shiftKey && !e.altKey && !e.ctrlKey && target === inputEl)) {
    e.preventDefault();
    cycleMode();
    return;
  }
  // In a permission card y / a / n pick the button (Enter then answers, as in the terminal UI; Tab reaches them too);
  // never while typing, while a key is held, or in the first half second after the card appeared.
  const card = target.closest('.ask');
  if (card instanceof HTMLElement && card.querySelector('.abt [data-c="no"]') && !e.ctrlKey && !e.metaKey && !e.altKey
      && !e.repeat && !e.isComposing && Date.now() - Number(card.dataset.t || 0) > 500
      && !(target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement)) {
    const c = { y: 'yes', a: 'always', n: 'no' }[e.key.toLowerCase() as 'y' | 'a' | 'n'];
    const btn = c ? card.querySelector(`.abt [data-c="${c}"]`) : null;
    if (btn instanceof HTMLElement) {
      e.preventDefault();
      btn.focus();
      return;
    }
  }
  if (openChip) {
    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      closePop(true);
      return;
    }
    if ((e.key === 'ArrowDown' || e.key === 'ArrowUp') && target.closest('#pop')) {
      e.preventDefault();
      const opts = Array.from(popEl.querySelectorAll<HTMLElement>('.po'));
      const i = opts.indexOf(target as HTMLElement);
      opts[(i + (e.key === 'ArrowDown' ? 1 : -1) + opts.length) % opts.length]?.focus({ preventScroll: true });
      return;
    }
  }
  if (e.key === 'Enter' && target instanceof HTMLInputElement && target.dataset.q) {
    e.preventDefault();
    const root = target.closest('.ask');
    if (root instanceof HTMLElement) answerFree(target.dataset.q, root);
  } else if (e.key === 'Enter' && target instanceof HTMLElement && target.classList.contains('lnk') && target.dataset.url) {
    e.preventDefault();
    post({ type: 'openLink', url: target.dataset.url });
  }
});

window.addEventListener('message', (e: MessageEvent) => {
  const d = e.data as { type?: string; state?: State; action?: Action; fields?: unknown; agents?: unknown; sessions?: unknown; archived?: unknown; error?: unknown; key?: unknown; ok?: unknown; name?: unknown; id?: unknown; title?: unknown; description?: unknown; text?: unknown } | null;
  if (!d || typeof d !== 'object') return;
  if (d.type === 'settings') {
    settings.onFields(normalizeFields({ fields: d.fields }), typeof d.error === 'string' ? d.error.slice(0, 500) : undefined, normalizeAgents({ agents: d.agents }));
    return;
  }
  if (d.type === 'choices') {
    const c = d as unknown as Partial<Choices>;
    const list = (v: unknown): string[] => (Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string').slice(0, 200) : []);
    const cur = (c.current ?? {}) as Partial<Record<ChipField, unknown>>;
    const one = (v: unknown): string | null => (typeof v === 'string' && v ? v.slice(0, 200) : null);
    choices = { model: list(c.model), effort: list(c.effort), mode: list(c.mode), current: { model: one(cur.model), effort: one(cur.effort), mode: one(cur.mode) }, ...(typeof d.error === 'string' ? { error: d.error.slice(0, 300) } : {}), ...(typeof (d as { modeError?: unknown }).modeError === 'string' ? { modeError: String((d as { modeError?: string }).modeError).slice(0, 500) } : {}) };
    chipSig = '';
    schedule();
    return;
  }
  if (d.type === 'sessions') {
    const list: SessionInfo[] = [];
    if (Array.isArray(d.sessions)) for (const x of d.sessions.slice(0, 200)) {
      const s = normalizeSession(x);
      if (s) list.push(s);
    }
    sessionsView.onList(list, typeof d.error === 'string' ? d.error.slice(0, 500) : undefined, state.session?.id ?? null, d.archived === true);
    return;
  }
  if (d.type === 'notes') {
    const n = normalizeNotes((d as { notes?: unknown }).notes);
    if (n) sessionsView.onNotes(n);
    return;
  }
  if (d.type === 'notesError') {
    sessionsView.onNotesError(typeof d.id === 'string' ? d.id : '', typeof d.error === 'string' ? d.error.slice(0, 500) : '');
    return;
  }
  if (d.type === 'notesSaved') {
    const t = (v: unknown): string => (typeof v === 'string' ? v.slice(0, 500) : '');
    sessionsView.onNotesSaved(t(d.id), d.ok === true, t(d.title), t(d.description), t(d.error));
    return;
  }
  if (d.type === 'sessionEnded') {
    sessionsView.onEnded(typeof d.id === 'string' ? d.id : '', d.ok === true, typeof d.text === 'string' ? d.text.slice(0, 300) : '');
    return;
  }
  if (d.type === 'sessionMoved') {
    sessionsView.onMoved(typeof d.id === 'string' ? d.id : '', d.ok === true, typeof d.text === 'string' ? d.text.slice(0, 300) : '');
    return;
  }
  if (d.type === 'sessionsError') {
    sessionsView.onError(typeof d.error === 'string' ? d.error.slice(0, 500) : '');
    return;
  }
  if (d.type === 'settingsResult') {
    settings.onResult(typeof d.key === 'string' ? d.key : '', d.ok === true, typeof d.error === 'string' ? d.error.slice(0, 500) : undefined);
    return;
  }
  if (d.type === 'view') {
    if (d.name === 'settings' && !settings.isOpen()) {
      if (sessionsView.isOpen()) sessionsView.close();
      settings.open();
    } else if (d.name === 'sessions' && !sessionsView.isOpen()) {
      if (settings.isOpen()) settings.close();
      sessionsView.open();
    } else if (d.name === 'chat') showChat();
    return;
  }
  if (d.type === 'init' && d.state && typeof d.state === 'object') {
    state = d.state;
    cache.clear();
    logEl.textContent = '';
    schedule();
  } else if (d.type === 'action' && d.action && typeof d.action === 'object') {
    apply(d.action);
  }
});

autosize();
schedule();
post({ type: 'ready' });
