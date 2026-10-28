// Protokoll v1 (PROTOCOL.md). Eingehende Daten werden hier geprueft und normalisiert.

export type SessionInfo = {
  id: string;
  name: string;
  agent: string;
  project: string | null;
  status: string;
  last_active: string | null;
  model: string | null;
  origin: string | null;
  /** true: the chat is open in another process, read only here. */
  readonly: boolean;
  /** true: the chat is in the archive (`session.list` with `archived`); opening it restores it. */
  archived?: boolean;
  /** Run id of the session that started this one (`lm run` from a chat); null = a person did. */
  parent: string | null;
  /** Name of the parent when it is not in the list itself; null otherwise. */
  parentName: string | null;
};

export type StatusInfo = {
  model: string | null;
  effort: string | null;
  mode: string | null;
  project: string | null;
  account: string | null;
};

export type Hello = {
  version: number;
  lm: string;
  codename: string;
  tree: string;
  workdir: string;
  project: string | null;
};

export type BackendEvent =
  | { type: 'turn.start'; session: string; turn: string }
  | { type: 'text'; session: string; turn: string; text: string; delta: boolean; parent?: string }
  | { type: 'tool.start'; session: string; turn: string; call: string; name: string; summary: string; files: string[]; parent?: string }
  | { type: 'tool.end'; session: string; turn: string; call: string; ok: boolean; summary: string; files: string[]; parent?: string }
  | { type: 'subagent.start'; session: string; turn: string; id: string; name: string; task: string; workflow?: string; phase?: string }
  // Ein Workflow (viele Unteragenten nach einem Skript): Start mit Plan, Phasenwechsel, Logzeile, Ende.
  | { type: 'workflow.start'; session: string; turn: string; id: string; name: string; description: string; phases: string[] }
  | { type: 'workflow.phase'; session: string; turn: string; id: string; title: string }
  | { type: 'workflow.log'; session: string; turn: string; id: string; text: string }
  | { type: 'workflow.end'; session: string; turn: string; id: string; ok: boolean; summary: string }
  | { type: 'subagent.end'; session: string; turn: string; id: string; ok: boolean; summary: string }
  // Ergebnis eines Hintergrund-Unteragenten, das als Mitteilung in den Chat kommt (nie als Text des Nutzers).
  | { type: 'subagent.message'; session: string; turn: string; name: string; ok: boolean; summary: string; file: string }
  | {
      type: 'question';
      session: string;
      question: string;
      kind: 'permission' | 'ask';
      text: string;
      detail: string;
      choices: string[];
    }
  | { type: 'question.closed'; session: string; question: string; choice: string }
  | { type: 'turn.end'; session: string; turn: string; status: 'done' | 'cancelled' | 'error'; cost_usd: number; context_used: number | null }
  | { type: 'status'; session: string; model: string | null; effort: string | null; mode: string | null; project: string | null; account: string | null }
  | { type: 'notice'; session: string; level: 'info' | 'warn'; text: string }
  | { type: 'error'; session: string; text: string }
  // Ein anderes Fenster hat dieses Gespraech im Resume-Menue beendet: die Sitzung ist zu.
  | { type: 'session.ended'; session: string; text: string }
  | { type: 'interject'; session: string; id: string; state: InterjectState; text: string; parent?: string; agent?: string }
  // Entsteht aus text mit role "user" (Verlauf); ein eigenes user-Ereignis wird weiter toleriert.
  | { type: 'user'; session: string; text: string };

export type InterjectState = 'pending' | 'delivered' | 'queued' | 'sent' | 'dropped';
const INTERJECT_STATES: readonly string[] = ['pending', 'delivered', 'queued', 'sent', 'dropped'];

export const MAX_TEXT = 1_000_000;

function isObj(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}
function str(v: unknown, max = MAX_TEXT): string {
  if (typeof v === 'string') return v.length > max ? v.slice(0, max) : v;
  if (typeof v === 'number' && Number.isFinite(v)) return String(v);
  return '';
}
function optStr(v: unknown, max = 2000): string | null {
  if (typeof v === 'string' && v !== '') return v.slice(0, max);
  return null;
}
function num(v: unknown, fallback = 0): number {
  return typeof v === 'number' && Number.isFinite(v) ? v : fallback;
}
function strList(v: unknown, maxItems: number, maxLen: number): string[] {
  if (!Array.isArray(v)) return [];
  const out: string[] = [];
  for (const x of v) {
    if (typeof x === 'string' && out.length < maxItems) out.push(x.slice(0, maxLen));
  }
  return out;
}

export function normalizeSession(raw: unknown): SessionInfo | null {
  if (!isObj(raw)) return null;
  const id = str(raw.id, 500);
  if (!id) return null;
  return {
    id,
    name: str(raw.name, 500) || id,
    agent: str(raw.agent, 200),
    project: optStr(raw.project, 500),
    status: str(raw.status, 40) || 'done',
    last_active: raw.last_active == null ? null : str(raw.last_active, 100) || null,
    model: optStr(raw.model, 200),
    origin: optStr(raw.origin, 40),
    readonly: raw.readonly === true,
    archived: raw.archived === true,
    parent: optStr(raw.parent, 500) || null,
    parentName: optStr(raw.parent_name, 500) || null,
  };
}

/** Notes of a chat (Ctrl+N in the terminal): name, description and the to-do tree of its run folder. */
export type NoteRemark = { by: string; text: string };
export type NoteLeaf =
  | { type: 'task'; id: string; title: string; state: 'open' | 'doing' | 'done'; info: string; remarks: NoteRemark[] }
  | { type: 'note'; id: string; title: string; text: string };
export type NoteNode = NoteLeaf | { type: 'folder'; id: string; title: string; items: NoteLeaf[] };
export type NotesInfo = { session: string; title: string; description: string; items: NoteNode[] };

function noteLeaf(raw: unknown): NoteLeaf | null {
  if (!isObj(raw)) return null;
  const id = str(raw.id, 100);
  const title = str(raw.title, 400);
  if (raw.type === 'note') return { type: 'note', id, title, text: str(raw.text, 8000) };
  if (raw.type !== 'task') return null;
  const st = raw.state === 'doing' || raw.state === 'done' ? raw.state : 'open';
  const remarks: NoteRemark[] = [];
  if (Array.isArray(raw.remarks)) for (const r of raw.remarks.slice(0, 50)) if (isObj(r)) remarks.push({ by: str(r.by, 40) || 'user', text: str(r.text, 4000) });
  return { type: 'task', id, title, state: st, info: str(raw.info, 8000), remarks };
}

export function normalizeNotes(raw: unknown): NotesInfo | null {
  if (!isObj(raw)) return null;
  const session = str(raw.session, 500);
  if (!session) return null;
  const items: NoteNode[] = [];
  if (Array.isArray(raw.items)) {
    for (const x of raw.items.slice(0, 500)) {
      if (isObj(x) && x.type === 'folder') {
        const sub: NoteLeaf[] = [];
        if (Array.isArray(x.items)) for (const y of x.items.slice(0, 500)) {
          const l = noteLeaf(y);
          if (l) sub.push(l);
        }
        items.push({ type: 'folder', id: str(x.id, 100), title: str(x.title, 400), items: sub });
      } else {
        const l = noteLeaf(x);
        if (l) items.push(l);
      }
    }
  }
  return { session, title: str(raw.title, 500), description: str(raw.description, 500), items };
}

export const AGENT_FIELDS = ['project', 'provider', 'model', 'effort', 'mode'] as const;
export type AgentField = (typeof AGENT_FIELDS)[number];
/** An agent of agents/: `own` is what its file holds (null = uses the default), `eff` what applies. */
export type AgentInfo = { name: string; own: Record<AgentField, string | null>; eff: Record<AgentField, string | null>; error: string | null };

export function normalizeAgents(raw: unknown): AgentInfo[] {
  if (!isObj(raw) || !Array.isArray(raw.agents)) return [];
  const out: AgentInfo[] = [];
  for (const a of raw.agents.slice(0, 200)) {
    if (!isObj(a)) continue;
    const name = str(a.name, 200);
    if (!name) continue;
    const eff = isObj(a.effective) ? a.effective : {};
    const own = {} as Record<AgentField, string | null>;
    const ef = {} as Record<AgentField, string | null>;
    for (const f of AGENT_FIELDS) {
      own[f] = optStr(a[f], 200);
      ef[f] = optStr(eff[f], 200);
    }
    out.push({ name, own, eff: ef, error: optStr(a.error, 500) });
  }
  return out;
}

export function normalizeHello(raw: unknown): Hello | null {
  if (!isObj(raw) || raw.type !== 'hello') return null;
  return {
    version: num(raw.version, 0),
    lm: str(raw.lm, 100),
    codename: str(raw.codename, 100),
    tree: str(raw.tree, 4000),
    workdir: str(raw.workdir, 4000),
    project: optStr(raw.project, 500),
  };
}

function par(raw: Record<string, unknown>): { parent?: string } {
  const v = str(raw.parent, 200);
  return v ? { parent: v } : {};
}

export function normalizeEvent(raw: unknown): BackendEvent | null {
  if (!isObj(raw) || typeof raw.type !== 'string') return null;
  const session = str(raw.session, 500);
  switch (raw.type) {
    case 'turn.start':
      return { type: 'turn.start', session, turn: str(raw.turn, 200) };
    case 'text':
      // Protokoll: Nutzertexte im Verlauf sind text mit role "user".
      if (raw.role === 'user') return { type: 'user', session, text: str(raw.text) };
      return { type: 'text', session, turn: str(raw.turn, 200), text: str(raw.text), delta: raw.delta === true, ...par(raw) };
    case 'tool.start':
      return {
        type: 'tool.start',
        session,
        turn: str(raw.turn, 200),
        call: str(raw.call, 200),
        name: str(raw.name, 200),
        summary: str(raw.summary, 20_000),
        files: strList(raw.files, 50, 4000),
        ...par(raw),
      };
    case 'tool.end':
      return {
        type: 'tool.end',
        session,
        turn: str(raw.turn, 200),
        call: str(raw.call, 200),
        ok: raw.ok !== false,
        summary: str(raw.summary, 20_000),
        files: strList(raw.files, 50, 4000),
        ...par(raw),
      };
    case 'subagent.start': {
      const id = str(raw.id, 200);
      if (!id) return null;
      const flow = str(raw.workflow, 200);
      return {
        type: 'subagent.start', session, turn: str(raw.turn, 200), id, name: str(raw.name, 200) || 'agent', task: str(raw.task, 2000),
        ...(flow ? { workflow: flow, phase: str(raw.phase, 200) } : {}),
      };
    }
    case 'workflow.start': {
      const id = str(raw.id, 200);
      if (!id) return null;
      return { type: 'workflow.start', session, turn: str(raw.turn, 200), id, name: str(raw.name, 200) || 'workflow', description: str(raw.description, 2000), phases: strList(raw.phases, 50, 200) };
    }
    case 'workflow.phase': {
      const id = str(raw.id, 200);
      return id ? { type: 'workflow.phase', session, turn: str(raw.turn, 200), id, title: str(raw.title, 200) } : null;
    }
    case 'workflow.log': {
      const id = str(raw.id, 200);
      return id ? { type: 'workflow.log', session, turn: str(raw.turn, 200), id, text: str(raw.text, 2000) } : null;
    }
    case 'workflow.end': {
      const id = str(raw.id, 200);
      return id ? { type: 'workflow.end', session, turn: str(raw.turn, 200), id, ok: raw.ok !== false, summary: str(raw.summary, 2000) } : null;
    }
    case 'subagent.end': {
      const id = str(raw.id, 200);
      if (!id) return null;
      return { type: 'subagent.end', session, turn: str(raw.turn, 200), id, ok: raw.ok !== false, summary: str(raw.summary, 2000) };
    }
    case 'subagent.message':
      return {
        type: 'subagent.message',
        session,
        turn: str(raw.turn, 200),
        name: str(raw.name, 200) || 'agent',
        ok: raw.ok !== false,
        summary: str(raw.summary, 2000),
        file: str(raw.file, 1000),
      };
    case 'question': {
      const id = str(raw.question, 200);
      if (!id) return null;
      return {
        type: 'question',
        session,
        question: id,
        kind: raw.kind === 'ask' ? 'ask' : 'permission',
        text: str(raw.text, 20_000),
        detail: str(raw.detail, 50_000),
        choices: strList(raw.choices, 20, 500),
      };
    }
    case 'question.closed':
      return { type: 'question.closed', session, question: str(raw.question, 200), choice: str(raw.choice, 5000) };
    case 'turn.end': {
      const status = raw.status === 'cancelled' || raw.status === 'error' ? raw.status : 'done';
      const ctx = typeof raw.context_used === 'number' && Number.isFinite(raw.context_used) ? Math.min(1, Math.max(0, raw.context_used)) : null;
      return { type: 'turn.end', session, turn: str(raw.turn, 200), status, cost_usd: Math.max(0, num(raw.cost_usd)), context_used: ctx };
    }
    case 'status':
      return {
        type: 'status',
        session,
        model: optStr(raw.model, 200),
        effort: optStr(raw.effort, 100),
        mode: optStr(raw.mode, 100),
        project: optStr(raw.project, 500),
        account: optStr(raw.account, 500),
      };
    case 'notice':
      return { type: 'notice', session, level: raw.level === 'warn' ? 'warn' : 'info', text: str(raw.text, 20_000) };
    case 'error':
      return { type: 'error', session, text: str(raw.text, 20_000) };
    case 'session.ended':
      return { type: 'session.ended', session, text: str(raw.text, 500) };
    case 'user':
      return { type: 'user', session, text: str(raw.text) };
    case 'interject': {
      if (typeof raw.state !== 'string' || !INTERJECT_STATES.includes(raw.state)) return null;
      const agent = str(raw.agent, 200);
      return { type: 'interject', session, id: str(raw.id, 40), state: raw.state as InterjectState, text: str(raw.text), ...par(raw), ...(agent ? { agent } : {}) };
    }
    default:
      return null;
  }
}

export type SettingValue = string | number | boolean;
export type SettingsField = { key: string; label: string; help: string; value: SettingValue | null; choices: SettingValue[]; scope: string };

function settingValue(v: unknown, max = 200): SettingValue | null {
  if (typeof v === 'string') return v.slice(0, max);
  if (typeof v === 'number' && Number.isFinite(v)) return v;
  if (typeof v === 'boolean') return v;
  return null;
}

export function normalizeFields(raw: unknown): SettingsField[] {
  if (!isObj(raw) || !Array.isArray(raw.fields)) return [];
  const out: SettingsField[] = [];
  for (const f of raw.fields.slice(0, 200)) {
    if (!isObj(f) || typeof f.key !== 'string' || !f.key || f.key.length > 200) continue;
    const choices: SettingValue[] = [];
    if (Array.isArray(f.choices)) for (const c of f.choices) {
      const v = settingValue(c);
      if (v !== null && choices.length < 200) choices.push(v);
    }
    out.push({ key: f.key, label: str(f.label, 200), help: str(f.help, 600), value: settingValue(f.value, 2000), choices, scope: str(f.scope, 20) });
  }
  return out;
}

/** Nur http(s) mit begrenzter Laenge gilt als sicherer Link. */
export function safeHref(url: unknown): string | null {
  if (typeof url !== 'string' || url.length === 0 || url.length > 2000) return null;
  let u: URL;
  try {
    u = new URL(url);
  } catch {
    return null;
  }
  if (u.protocol !== 'http:' && u.protocol !== 'https:') return null;
  return u.href;
}
