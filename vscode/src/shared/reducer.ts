// Reiner Reduzierer (state, action) -> state. Wird in der Erweiterung UND in der Webview benutzt.
import type { BackendEvent, InterjectState, SessionInfo, StatusInfo } from './protocol.ts';

/** A message written while a turn runs: id is null until the backend event binds it. */
export type UserItem = { k: 'user'; id: number; text: string; ij?: { id: string | null; state: InterjectState } };
export type AssistantItem = { k: 'assistant'; id: number; turn: string; text: string; open: boolean };
export type ToolItem = {
  k: 'tool';
  id: number;
  call: string;
  name: string;
  summary: string;
  status: 'run' | 'done' | 'fail' | 'stop';
  files: string[];
  /** Das Backend meldet geaenderte Dateien (nur bei erfolgreichem write/edit): nur dann gibt es einen Diff. */
  changed: boolean;
};
export type QuestionItem = {
  k: 'question';
  id: number;
  question: string;
  kind: 'permission' | 'ask';
  text: string;
  detail: string;
  choices: string[];
  answered: string | null;
};
export type SubMsgItem = { k: 'submsg'; id: number; name: string; ok: boolean; summary: string; file: string };
export type NoticeItem = { k: 'notice'; id: number; level: 'info' | 'warn'; text: string };
export type ErrorItem = { k: 'error'; id: number; text: string };
export type TurnEndItem = { k: 'turnend'; id: number; status: 'done' | 'cancelled' | 'error'; cost: number; context: number | null };
export type Item = UserItem | AssistantItem | ToolItem | QuestionItem | SubMsgItem | NoticeItem | ErrorItem | TurnEndItem;

export type ConnStatus = 'starting' | 'ready' | 'down' | 'restarting';

export type SubInfo = { id: string; name: string; task: string; done: boolean; ok: boolean; summary: string; log: string[]; workflow?: string; phase?: string };
/** Ein Workflow-Lauf: Plan, aktuelle Phase, letzte Logzeile, Ende. Seine Unteragenten tragen seine id in `workflow`. */
export type FlowInfo = { id: string; name: string; phases: string[]; phase: string; log: string; done: boolean; ok: boolean; summary: string };

export type State = {
  subs: SubInfo[];
  flows: FlowInfo[];
  conn: { status: ConnStatus; detail: string };
  session: SessionInfo | null;
  items: Item[];
  busy: boolean;
  cancelling: boolean;
  follow: boolean;
  status: StatusInfo;
  context: number | null;
  cost: number;
  seq: number;
};

export type Action =
  | { kind: 'event'; event: BackendEvent }
  | { kind: 'conn'; status: ConnStatus; detail?: string }
  | { kind: 'open'; session: SessionInfo; history: BackendEvent[] }
  | { kind: 'user'; text: string; interject?: boolean }
  | { kind: 'answered'; question: string; choice: string }
  | { kind: 'cancelling' }
  | { kind: 'follow'; on: boolean }
  | { kind: 'localerror'; text: string }
  | { kind: 'ijfailed'; text: string }
  | { kind: 'ijclear' }
  | { kind: 'localnotice'; level: 'info' | 'warn'; text: string };

export const MAX_ITEMS = 1500;

export function initialState(): State {
  return {
    conn: { status: 'starting', detail: '' },
    session: null,
    subs: [],
    flows: [],
    items: [],
    busy: false,
    cancelling: false,
    follow: true,
    status: { model: null, effort: null, mode: null, project: null, account: null },
    context: null,
    cost: 0,
    seq: 0,
  };
}

function push(s: State, make: (id: number) => Item): State {
  const id = s.seq + 1;
  let items = s.items.concat([make(id)]);
  if (items.length > MAX_ITEMS) items = items.slice(items.length - MAX_ITEMS);
  return { ...s, items, seq: id };
}

function replaceAt(s: State, idx: number, item: Item): State {
  const items = s.items.slice();
  items[idx] = item;
  return { ...s, items };
}

/** Open permission requests and questions, in arrival order; the UI pins them below all messages. */
export function openQuestions(s: State): QuestionItem[] {
  return s.items.filter((it): it is QuestionItem => it.k === 'question' && it.answered === null);
}

/** Beendet offene Streams, laufende Werkzeuge und offene Fragen. */
function settle(s: State): State {
  let changed = false;
  const items = s.items.map((it) => {
    if (it.k === 'tool' && it.status === 'run') {
      changed = true;
      return { ...it, status: 'stop' as const };
    }
    if (it.k === 'question' && it.answered === null) {
      changed = true;
      return { ...it, answered: '(closed)' };
    }
    if (it.k === 'assistant' && it.open) {
      changed = true;
      return { ...it, open: false };
    }
    return it;
  });
  return changed ? { ...s, items } : s;
}

function lastIndex(items: Item[], pred: (it: Item) => boolean): number {
  for (let i = items.length - 1; i >= 0; i--) if (pred(items[i])) return i;
  return -1;
}

function withSub(s: State, id: string, f: (b: SubInfo) => SubInfo): State {
  const i = s.subs.findIndex((b) => b.id === id);
  if (i < 0) return s;
  const subs = s.subs.slice();
  subs[i] = f(subs[i]);
  return { ...s, subs };
}

function withFlow(s: State, id: string, f: (w: FlowInfo) => FlowInfo): State {
  const i = s.flows.findIndex((w) => w.id === id);
  if (i < 0) return s;
  const flows = s.flows.slice();
  flows[i] = f(flows[i]);
  return { ...s, flows };
}

function applyEvent(s: State, ev: BackendEvent): State {
  // Fremdes Ereignis einer frueheren Sitzung.
  if (s.session && ev.session && ev.session !== s.session.id) return s;
  // Ereignisse eines Unteragenten gehoeren in seinen Verlauf, nicht in den Chat.
  if ((ev.type === 'text' || ev.type === 'tool.start' || ev.type === 'tool.end') && ev.parent) {
    const line = ev.type === 'text' ? ev.text : ev.type === 'tool.start' ? `${ev.name} ${ev.summary}` : `${ev.ok ? 'ok' : 'failed'}: ${ev.summary}`;
    return withSub(s, ev.parent, (b) => {
      const t = line.replace(/\s+/g, ' ').trim().slice(0, 300);
      if (!t) return b;
      const last = b.log[b.log.length - 1];
      const joined = ev.type === 'text' && ev.delta && last !== undefined && b.log.length > 0 ? [...b.log.slice(0, -1), (last + ev.text).replace(/\s+/g, ' ').slice(0, 300)] : [...b.log, t];
      return { ...b, log: joined.slice(-60) };
    });
  }
  switch (ev.type) {
    case 'subagent.start': {
      const rest = s.subs.filter((b) => b.id !== ev.id);
      const sub: SubInfo = { id: ev.id, name: ev.name, task: ev.task, done: false, ok: true, summary: '', log: [], ...(ev.workflow ? { workflow: ev.workflow, phase: ev.phase ?? '' } : {}) };
      // Agenten eines Workflows zaehlen nur in seiner Fortschrittszeile; einzeln zeigt die Webview die letzten 20 anderen.
      return { ...s, subs: [...rest, sub].slice(-400) };
    }
    case 'workflow.start': {
      const flow: FlowInfo = { id: ev.id, name: ev.name, phases: ev.phases, phase: '', log: '', done: false, ok: true, summary: '' };
      const next = { ...s, flows: [...s.flows.filter((w) => w.id !== ev.id), flow].slice(-5) };
      const plan = ev.phases.length ? ` · phases: ${ev.phases.join(', ')}` : '';
      return push(next, (id) => ({ k: 'notice', id, level: 'info', text: `Workflow ${ev.name}: ${ev.description}${plan}` }));
    }
    case 'workflow.phase':
      return withFlow(s, ev.id, (w) => ({ ...w, phase: ev.title }));
    case 'workflow.log':
      return withFlow(s, ev.id, (w) => ({ ...w, log: ev.text }));
    case 'workflow.end': {
      const name = s.flows.find((w) => w.id === ev.id)?.name ?? ev.id;
      const next = withFlow(s, ev.id, (w) => ({ ...w, done: true, ok: ev.ok, summary: ev.summary }));
      return push(next, (id) => ({ k: 'notice', id, level: ev.ok ? 'info' : 'warn', text: `Workflow ${name} ${ev.ok ? 'ended' : 'failed'}: ${ev.summary}` }));
    }
    case 'subagent.message': {
      const last = s.items[s.items.length - 1];
      const closed = last && last.k === 'assistant' && last.open ? replaceAt(s, s.items.length - 1, { ...last, open: false }) : s;
      return push(closed, (id) => ({ k: 'submsg', id, name: ev.name, ok: ev.ok, summary: ev.summary, file: ev.file }));
    }
    case 'subagent.end':
      return withSub(s, ev.id, (b) => ({ ...b, done: true, ok: ev.ok, summary: ev.summary }));
    case 'user':
      return push(s, (id) => ({ k: 'user', id, text: ev.text }));
    case 'interject': {
      const idxOf = (pred: (u: UserItem) => boolean): number => s.items.findIndex((it) => it.k === 'user' && pred(it));
      const setIj = (idx: number, st: InterjectState, id: string | null): State => replaceAt(s, idx, { ...(s.items[idx] as UserItem), ij: { id, state: st } });
      if (ev.state === 'pending' || ev.state === 'queued') {
        if (ev.parent) return s;
        const own = idxOf((u) => u.ij?.id === ev.id);
        if (own >= 0) return setIj(own, ev.state, ev.id);
        const idx = idxOf((u) => !!u.ij && u.ij.id === null);
        if (idx >= 0) return setIj(idx, ev.state, ev.id);
        return push(s, (id) => ({ k: 'user', id, text: ev.text, ij: { id: ev.id, state: ev.state as InterjectState } }));
      }
      if (ev.parent) {
        if (ev.state !== 'delivered') return s;
        return push(s, (id) => ({ k: 'notice', id, level: 'info', text: `${ev.agent || 'Sub-agent'} read the message passed on to it` }));
      }
      const idx = idxOf((u) => !!u.ij && u.ij.id === ev.id);
      if (idx < 0) return s;
      return setIj(idx, ev.state, ev.id);
    }
    case 'turn.start':
      return { ...s, busy: true, cancelling: false, subs: s.subs.filter((b) => !b.done), flows: s.flows.filter((w) => !w.done) };
    case 'text': {
      if (ev.text === '') return s;
      const last = s.items[s.items.length - 1];
      if (ev.delta) {
        if (last && last.k === 'assistant' && last.open && last.turn === ev.turn) {
          return replaceAt(s, s.items.length - 1, { ...last, text: last.text + ev.text });
        }
        const closed = last && last.k === 'assistant' && last.open ? replaceAt(s, s.items.length - 1, { ...last, open: false }) : s;
        return push(closed, (id) => ({ k: 'assistant', id, turn: ev.turn, text: ev.text, open: true }));
      }
      const closed = last && last.k === 'assistant' && last.open ? replaceAt(s, s.items.length - 1, { ...last, open: false }) : s;
      return push(closed, (id) => ({ k: 'assistant', id, turn: ev.turn, text: ev.text, open: false }));
    }
    case 'tool.start': {
      const last = s.items[s.items.length - 1];
      const closed = last && last.k === 'assistant' && last.open ? replaceAt(s, s.items.length - 1, { ...last, open: false }) : s;
      return push(closed, (id) => ({ k: 'tool', id, call: ev.call, name: ev.name, summary: ev.summary, status: 'run', files: ev.files, changed: false }));
    }
    case 'tool.end': {
      const idx = lastIndex(s.items, (it) => it.k === 'tool' && it.call === ev.call);
      const status = ev.ok ? ('done' as const) : ('fail' as const);
      if (idx >= 0) {
        const it = s.items[idx] as ToolItem;
        return replaceAt(s, idx, {
          ...it,
          status,
          summary: ev.summary || it.summary,
          files: ev.files.length ? ev.files : it.files,
          changed: ev.ok && ev.files.length > 0,
        });
      }
      return push(s, (id) => ({ k: 'tool', id, call: ev.call, name: 'tool', summary: ev.summary, status, files: ev.files, changed: ev.ok && ev.files.length > 0 }));
    }
    case 'question': {
      if (s.items.some((it) => it.k === 'question' && it.question === ev.question)) return s;
      const last = s.items[s.items.length - 1];
      const closed = last && last.k === 'assistant' && last.open ? replaceAt(s, s.items.length - 1, { ...last, open: false }) : s;
      return push(closed, (id) => ({
        k: 'question',
        id,
        question: ev.question,
        kind: ev.kind,
        text: ev.text,
        detail: ev.detail,
        choices: ev.choices,
        answered: null,
      }));
    }
    case 'question.closed': {
      const idx = lastIndex(s.items, (it) => it.k === 'question' && it.question === ev.question);
      if (idx < 0) return s;
      const it = s.items[idx] as QuestionItem;
      return replaceAt(s, idx, { ...it, answered: ev.choice || it.answered || '(closed)' });
    }
    case 'turn.end': {
      const settled = settle(s);
      const done = push(settled, (id) => ({ k: 'turnend', id, status: ev.status, cost: ev.cost_usd, context: ev.context_used }));
      return {
        ...done,
        busy: false,
        cancelling: false,
        cost: s.cost + ev.cost_usd,
        context: ev.context_used !== null ? ev.context_used : s.context,
      };
    }
    case 'status':
      return { ...s, status: { model: ev.model, effort: ev.effort, mode: ev.mode, project: ev.project, account: ev.account } };
    case 'notice':
      return push(s, (id) => ({ k: 'notice', id, level: ev.level, text: ev.text }));
    case 'error':
      return { ...push(s, (id) => ({ k: 'error', id, text: ev.text })), cancelling: false };
    case 'session.ended':
      // The chat was ended elsewhere: no session is open any more, the next message starts a new one.
      return { ...push(s, (id) => ({ k: 'notice', id, level: 'warn', text: ev.text })), session: null, busy: false, cancelling: false, subs: [], flows: [] };
  }
}

export function reduce(s: State, a: Action): State {
  switch (a.kind) {
    case 'event':
      return applyEvent(s, a.event);
    case 'conn': {
      const next = { ...s, conn: { status: a.status, detail: a.detail ?? '' } };
      if (a.status !== 'ready') return { ...settle(next), busy: false, cancelling: false, subs: [], flows: [] };
      return next;
    }
    case 'open': {
      let n: State = {
        ...s,
        session: a.session,
        items: [],
        subs: [],
        flows: [],
        busy: false,
        cancelling: false,
        context: null,
        cost: 0,
      };
      for (const ev of a.history) n = applyEvent(n, { ...ev, session: ev.session || a.session.id });
      n = settle(n);
      return { ...n, busy: false, cancelling: false, subs: [], flows: [] };
    }
    case 'user':
      if (a.interject) return { ...push(s, (id) => ({ k: 'user', id, text: a.text, ij: { id: null, state: 'pending' } })), busy: true };
      return { ...push(s, (id) => ({ k: 'user', id, text: a.text })), busy: true, cancelling: false };
    case 'answered': {
      const idx = lastIndex(s.items, (it) => it.k === 'question' && it.question === a.question && it.answered === null);
      if (idx < 0) return s;
      return replaceAt(s, idx, { ...(s.items[idx] as QuestionItem), answered: a.choice });
    }
    case 'cancelling':
      return s.busy ? { ...s, cancelling: true } : s;
    case 'follow':
      return { ...s, follow: a.on };
    case 'localerror':
      return { ...push(s, (id) => ({ k: 'error', id, text: a.text })), busy: false, cancelling: false };
    case 'ijfailed': {
      // The interjection was refused: mark the newest unbound one dropped (keeps its text); the turn itself keeps running.
      let idx = -1;
      for (let i = s.items.length - 1; i >= 0; i--) {
        const it = s.items[i];
        if (it.k === 'user' && it.ij && it.ij.id === null && it.ij.state === 'pending') {
          idx = i;
          break;
        }
      }
      const marked = idx >= 0 ? replaceAt(s, idx, { ...(s.items[idx] as UserItem), ij: { id: null, state: 'dropped' } }) : s;
      return push(marked, (id) => ({ k: 'error', id, text: a.text }));
    }
    case 'ijclear': {
      // The backend took the message as a normal one (no turn was running any more): drop the interjection mark of the newest unbound item.
      for (let i = s.items.length - 1; i >= 0; i--) {
        const it = s.items[i];
        if (it.k === 'user' && it.ij && it.ij.id === null) return replaceAt(s, i, { k: 'user', id: it.id, text: it.text });
      }
      return s;
    }
    case 'localnotice':
      return push(s, (id) => ({ k: 'notice', id, level: a.level, text: a.text }));
  }
}
