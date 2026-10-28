// Einstellungsansicht nach Mockup N1 "Orbit List": Kopf mit Orbit-Zeichen, Abschnitte mit Quelle, Listenzeilen mit Wert und
// Pfeil, Unterseite mit Auswahlliste oder Textfeld, Fusszeile "New chat starts with ...".
// Rendert nur per DOM-API (textContent), nie per innerHTML. Daten kommen ueber settings.get / settings.set des Backends.
import type { AgentInfo, SettingsField } from '../shared/protocol.ts';
import { choiceIndex, fieldTitle, groupFields, showValue } from '../shared/settings-view.ts';
import { summaryParts } from '../shared/views.ts';
import { createFx } from './settings-fx.ts';
import type { Fx } from './settings-fx.ts';

type Post = (m: Record<string, unknown>) => void;

function h<K extends keyof HTMLElementTagNameMap>(tag: K, cls?: string, text?: string): HTMLElementTagNameMap[K] {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

const MODE_TEXT: Record<string, string> = {
  auto: 'works on its own, asks only at risky steps',
  plan: 'reads and plans only, waits for your go',
  bypass: 'works without asking',
};
const AGENT_LABEL: Record<string, string> = { project: 'Project', provider: 'Account', model: 'Model', effort: 'Effort', mode: 'Mode' };
const isModeKey = (key: string): boolean => /(^|\.)mode$/.test(key);

export type SettingsView = {
  open(): void;
  close(): void;
  isOpen(): boolean;
  onFields(fields: SettingsField[], error: string | undefined, agents?: AgentInfo[]): void;
  onResult(key: string, ok: boolean, error: string | undefined): void;
};

export function initSettings(post: Post, onVisibility: (open: boolean) => void): SettingsView {
  const $ = (id: string): HTMLElement => document.getElementById(id) as HTMLElement;
  const root = $('sview');
  const listPg = $('s-list');
  const subPg = $('s-sub');
  const listSc = $('s-listsc');
  const groupsEl = $('s-groups');
  const subSc = $('s-subsc');
  const titleEl = $('s-title');
  const backL = $('s-backl');
  const toast = $('s-toast');
  const stateEl = $('s-state');
  const barEl = $('s-bar');
  const sumEl = $('s-sum');
  let fields: SettingsField[] = [];
  let agents: AgentInfo[] = [];
  let error = '';
  let loaded = false;
  let sub: string | null = null; // Schluessel der Unterseite, oder "agent/<name>"
  let subError = '';
  let pending: string | null = null;
  let toastTimer = 0;
  let transUntil = 0;
  let wasSub = false;

  const fx: Fx = createFx({
    view: root,
    grid: $('s-grid') as HTMLCanvasElement,
    sign: $('s-sign') as HTMLCanvasElement,
    home: listPg,
    sc: listSc,
    slot0: $('s-slot0'),
    slot1: $('s-slot1'),
    homeVisible: () => listPg.dataset.s === 'cur' || performance.now() < transUntil,
  });

  function say(text: string): void {
    toast.textContent = text;
    toast.dataset.show = '1';
    clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => {
      delete toast.dataset.show;
    }, 2200);
  }

  function setState(text: string, dirty: boolean): void {
    stateEl.textContent = text;
    barEl.toggleAttribute('data-dirty', dirty);
  }

  function valueNode(f: SettingsField): HTMLElement {
    const v = h('span', 'val');
    if (f.value === null) v.appendChild(h('span', 'inh', showValue(null)));
    else if (isModeKey(f.key) && typeof f.value === 'string') v.appendChild(h('span', `m-${f.value}`, f.value));
    else v.textContent = showValue(f.value);
    return v;
  }

  function group(title: string, code: string): { sec: HTMLElement; card: HTMLElement } {
    const sec = h('section', 'grp');
    const gh = h('div', 'gh');
    const g = h('span', 'g', '◉');
    g.setAttribute('aria-hidden', 'true');
    gh.appendChild(g);
    gh.appendChild(h('h2', undefined, title));
    gh.appendChild(h('code', undefined, code));
    sec.appendChild(gh);
    const card = h('div', 'card');
    sec.appendChild(card);
    return { sec, card };
  }

  function agentSummary(a: AgentInfo): HTMLElement {
    const v = h('span', 'val');
    (['model', 'effort', 'mode'] as const).forEach((f, i) => {
      if (i) v.appendChild(h('span', 'sep', ' · '));
      const own = a.own[f];
      const val = own ?? a.eff[f] ?? 'default';
      const cls = own === null ? 'inh' : f === 'mode' ? `m-${val}` : '';
      v.appendChild(cls ? h('span', cls, val) : document.createTextNode(val));
    });
    return v;
  }

  function renderSummary(): void {
    sumEl.textContent = '';
    for (const p of summaryParts(fields)) {
      sumEl.appendChild(h('span', p.mode ? `md-${p.text}` : undefined, p.text));
      sumEl.appendChild(document.createTextNode(' '));
    }
  }

  function renderList(): void {
    groupsEl.textContent = '';
    if (error) {
      const e = h('div', 'callout bad', error);
      e.setAttribute('role', 'alert');
      groupsEl.appendChild(e);
    }
    if (!loaded && !error) groupsEl.appendChild(h('p', 'none', 'Loading...'));
    if (loaded && !fields.length && !error) groupsEl.appendChild(h('p', 'none', 'The backend lists no settings.'));
    const groups = groupFields(fields);
    const agentGroup = (): void => {
      if (!loaded) return;
      const { sec, card } = group('Agents', 'agents/');
      for (const a of agents) {
        const row = h('button', 'row ag');
        row.type = 'button';
        row.dataset.agent = a.name;
        const lab = h('span', 'lab');
        lab.appendChild(h('span', 'name', a.name));
        lab.appendChild(h('span', 'key', `project ${a.own.project ?? 'none'}`));
        row.appendChild(lab);
        row.appendChild(agentSummary(a));
        row.appendChild(h('i', 'chev'));
        row.addEventListener('click', () => openSub(`agent/${a.name}`));
        card.appendChild(row);
      }
      if (!agents.length) card.appendChild(h('p', 'none', 'No agents in agents/ yet.'));
      sec.appendChild(h('p', 'gf', 'Empty fields use the default from Defaults.'));
      groupsEl.appendChild(sec);
    };
    const place = groups.findIndex((g) => g.id === 'defaults');
    groups.forEach((g, gi) => {
      const { sec, card } = group(g.title, 'config.md');
      for (const f of g.fields) {
        const row = h('button', 'row');
        row.type = 'button';
        row.dataset.key = f.key;
        const lab = h('span', 'lab');
        lab.appendChild(h('span', 'name', fieldTitle(f)));
        lab.appendChild(h('span', 'key', f.key));
        row.appendChild(lab);
        row.appendChild(valueNode(f));
        row.appendChild(h('i', 'chev'));
        row.addEventListener('click', () => openSub(f.key));
        card.appendChild(row);
      }
      groupsEl.appendChild(sec);
      if (gi === (place >= 0 ? place : groups.length - 1)) agentGroup();
    });
    if (!groups.length) agentGroup();
    renderSummary();
    fx.place();
  }

  function callout(on: boolean): HTMLElement {
    const c = h('p', 'callout');
    c.appendChild(h('b', undefined, 'Caution'));
    c.appendChild(document.createTextNode('bypass is a permission switch. Prompts are skipped: the agent may edit and run without asking.'));
    if (on) c.dataset.on = '1';
    return c;
  }

  function renderAgentSub(name: string): void {
    const a = agents.find((x) => x.name === name);
    if (!a) return;
    titleEl.textContent = a.name;
    subSc.appendChild(h('p', 'hint', `agents/${a.name}.md. Empty fields use the default and show it dimmed.`));
    if (a.error) {
      const e = h('div', 'callout bad', a.error);
      e.setAttribute('role', 'alert');
      subSc.appendChild(e);
    }
    const card = h('div', 'card');
    for (const f of ['project', 'provider', 'model', 'effort', 'mode'] as const) {
      const own = a.own[f];
      const row = h('div', 'row info');
      const lab = h('span', 'lab');
      lab.appendChild(h('span', 'name', AGENT_LABEL[f]));
      lab.appendChild(h('span', 'key', own === null ? (f === 'project' ? 'none set' : 'uses default') : 'own value'));
      row.appendChild(lab);
      const v = h('span', 'val');
      const val = own ?? (f === 'project' ? 'none' : (a.eff[f] ?? 'default'));
      v.appendChild(h('span', own === null ? 'inh' : f === 'mode' ? `m-${val}` : '', val));
      row.appendChild(v);
      card.appendChild(row);
    }
    subSc.appendChild(card);
    subSc.appendChild(h('p', 'gf', 'To change an agent, edit its file.'));
  }

  function renderSub(): void {
    subSc.textContent = '';
    if (sub === null) return;
    if (sub.startsWith('agent/')) {
      renderAgentSub(sub.slice(6));
      return;
    }
    const f = fields.find((x) => x.key === sub);
    if (!f) return;
    titleEl.textContent = fieldTitle(f);
    subSc.appendChild(h('p', 'hint', f.help || f.key));
    if (subError) {
      const e = h('div', 'callout bad', subError);
      e.setAttribute('role', 'alert');
      subSc.appendChild(e);
    }
    if (f.choices.length) {
      const modes = isModeKey(f.key);
      const card = h('div', 'card');
      card.setAttribute('role', 'radiogroup');
      card.setAttribute('aria-label', fieldTitle(f));
      const cur = choiceIndex(f);
      f.choices.forEach((c, i) => {
        const v = showValue(c);
        const b = h('button', modes ? `opt m-${v}` : 'opt');
        b.type = 'button';
        b.setAttribute('role', 'radio');
        b.setAttribute('aria-checked', String(i === cur));
        b.dataset.i = String(i);
        if (modes) b.appendChild(h('i', 'ring'));
        const lab = h('span', 'lab');
        lab.appendChild(h('span', 'name', v));
        if (modes && MODE_TEXT[v]) lab.appendChild(h('span', 'sub', MODE_TEXT[v]));
        b.appendChild(lab);
        if (modes && v === 'bypass') b.appendChild(h('em', 'tag warn', 'Caution'));
        b.addEventListener('click', () => {
          if (i === cur || pending) return;
          pending = f.key;
          subError = '';
          setState('Saving...', true);
          post({ type: 'settingsPick', key: f.key, index: i });
        });
        card.appendChild(b);
      });
      subSc.appendChild(card);
      if (modes) subSc.appendChild(callout(f.value === 'bypass'));
    } else {
      const form = h('div', 'tf');
      const inp = h('input');
      inp.type = 'text';
      inp.value = f.value === null ? '' : String(f.value);
      inp.setAttribute('aria-label', fieldTitle(f));
      inp.spellcheck = false;
      const save = h('button', 'btn pri', 'Save');
      save.type = 'button';
      const go = (): void => {
        if (pending) return;
        pending = f.key;
        subError = '';
        setState('Saving...', true);
        post({ type: 'settingsText', key: f.key, text: inp.value });
      };
      save.addEventListener('click', go);
      inp.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') {
          e.preventDefault();
          go();
        }
      });
      form.appendChild(inp);
      form.appendChild(save);
      subSc.appendChild(form);
    }
    subSc.appendChild(h('p', 'foot', f.key));
  }

  function setPages(): void {
    const subOpen = sub !== null;
    transUntil = performance.now() + 500;
    root.classList.toggle('nt', matchMedia('(prefers-reduced-motion: reduce)').matches);
    listPg.dataset.s = subOpen ? 'under' : 'cur';
    subPg.dataset.s = subOpen ? 'cur' : 'right';
    listPg.setAttribute('aria-hidden', String(subOpen));
    subPg.setAttribute('aria-hidden', String(!subOpen));
    (listPg as HTMLElement & { inert: boolean }).inert = subOpen;
    (subPg as HTMLElement & { inert: boolean }).inert = !subOpen;
    backL.textContent = 'Settings';
    if (!subOpen) {
      titleEl.textContent = '';
      if (wasSub && !root.hidden) fx.start();
    }
    wasSub = subOpen;
  }

  function openSub(key: string): void {
    sub = key;
    subError = '';
    subSc.scrollTop = 0;
    renderSub();
    setPages();
    titleEl.focus({ preventScroll: true });
  }

  function close(): void {
    root.hidden = true;
    sub = null;
    fx.stop();
    onVisibility(false);
  }

  function back(): void {
    if (sub !== null) {
      const key = sub;
      sub = null;
      setPages();
      const row = Array.from(listSc.querySelectorAll<HTMLElement>('.row')).find((r) => r.dataset.key === key || (r.dataset.agent !== undefined && `agent/${r.dataset.agent}` === key));
      row?.focus({ preventScroll: true });
    } else close();
  }

  function open(): void {
    root.hidden = false;
    onVisibility(true);
    sub = null;
    subError = '';
    error = '';
    setPages();
    setState('saved', false);
    renderList();
    fx.start();
    post({ type: 'settingsGet' });
    $('s-wm').focus({ preventScroll: true });
  }

  function metrics(): void {
    root.style.setProperty('--barh', `${barEl.offsetHeight}px`);
  }
  if (window.ResizeObserver) new ResizeObserver(metrics).observe(barEl);

  $('s-back').addEventListener('click', back);
  $('s-close').addEventListener('click', close);
  $('s-open').addEventListener('click', () => post({ type: 'settings' }));
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !root.hidden) {
      e.preventDefault();
      back();
    }
  });

  return {
    open,
    close,
    isOpen: () => !root.hidden,
    onFields(fs, err, ags) {
      fields = fs;
      if (ags) agents = ags;
      error = err ?? '';
      loaded = true;
      if (sub !== null && !(fields.some((f) => f.key === sub) || agents.some((a) => `agent/${a.name}` === sub))) sub = null;
      renderList();
      if (sub !== null) renderSub();
      setPages();
      metrics();
    },
    onResult(key, ok, err) {
      if (pending !== key) return;
      pending = null;
      if (ok) {
        setState('saved', false);
        say('Saved');
      } else {
        setState('not saved', true);
        subError = err || 'The setting could not be saved.';
        if (sub === key) renderSub();
      }
    },
  };
}
