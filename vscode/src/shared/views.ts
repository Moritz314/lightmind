// Reine Logik der Sitzungsliste, der Auswahlchips und der Fusszeile der Einstellungen (ohne DOM, in Node testbar).
import type { SessionInfo, SettingsField, SettingValue } from './protocol.ts';

/** The raw command of a permission request, folded away (the plain sentence is the question). Opened it is shown whole
 *  (the body scrolls), so the part of a long line that runs is never hidden; only beyond ASK_DETAIL_MAX characters it is
 *  shortened in the middle, start and end kept. */
export const ASK_DETAIL_MAX = 100_000;
export function askPreview(detail: string): { text: string; cut: boolean; chars: number } {
  let text = detail.replace(/[\u0000-\u0008\u000b-\u001f\u007f-\u009f\u200b-\u200f\u2028-\u202e\u2060-\u2069\ufeff]/g, ' ');
  const cut = text.length > ASK_DETAIL_MAX;
  if (cut) {
    const tail = Math.floor(ASK_DETAIL_MAX / 4);
    text = text.slice(0, ASK_DETAIL_MAX - tail) + ' … ' + text.slice(-tail);
  }
  return { text, cut, chars: detail.length };
}

export type ChipField = 'model' | 'effort' | 'mode';
export const CHIP_FIELDS: readonly ChipField[] = ['model', 'effort', 'mode'];
export const CHIP_TITLE: Record<ChipField, string> = { model: 'Model', effort: 'Effort', mode: 'Mode' };
export type Choices = { model: string[]; effort: string[]; mode: string[]; current: Record<ChipField, string | null>; error?: string; modeError?: string };

export function emptyChoices(): Choices {
  return { model: [], effort: [], mode: [], current: { model: null, effort: null, mode: null } };
}

/** Schluessel der Einstellungen, aus denen Liste und Vorgabe eines Chips kommen (erster Treffer mit Wert gilt). */
export const CHIP_KEYS: Record<ChipField, string[]> = {
  model: ['vscode.model', 'defaults.model'],
  effort: ['vscode.effort', 'defaults.effort'],
  mode: ['vscode.mode', 'defaults.mode'],
};

function strings(v: SettingValue[]): string[] {
  return v.filter((x): x is string => typeof x === 'string');
}

export function choicesFromFields(fields: SettingsField[], pending: Partial<Record<ChipField, string>>): Choices {
  const out = emptyChoices();
  for (const f of CHIP_FIELDS) {
    const found = CHIP_KEYS[f].map((k) => fields.find((x) => x.key === k)).filter((x): x is SettingsField => !!x);
    out[f] = strings(found.find((x) => x.choices.length)?.choices ?? []);
    const v = found.find((x) => typeof x.value === 'string' && x.value !== '')?.value;
    out.current[f] = pending[f] ?? (typeof v === 'string' ? v : null);
  }
  return out;
}

/** Ist `choice` der Wert, der gerade gilt? Ein Modell darf als aufgeloester Name kommen ("claude-sonnet-4" fuer "sonnet"). */
export function isCurrent(field: ChipField, choice: string, current: string | null): boolean {
  if (!current) return false;
  return current === choice || (field === 'model' && current.toLowerCase().includes(choice.toLowerCase()));
}

export function chipValue(field: ChipField, status: Record<ChipField, string | null>, sessionModel: string | null, choices: Choices): string | null {
  return status[field] ?? (field === 'model' ? sessionModel : null) ?? choices.current[field];
}

// ---- Sitzungsliste ----
export function parseWhen(s: string | null): number | null {
  if (!s) return null;
  const t = Date.parse(/^\d{4}-\d\d-\d\d \d/.test(s) ? s.replace(' ', 'T') : s);
  return Number.isFinite(t) ? t : null;
}

export function ago(s: string | null, now: number): string {
  const t = parseWhen(s);
  if (t === null) return s ?? '';
  const sec = Math.max(0, Math.round((now - t) / 1000));
  if (sec < 60) return 'just now';
  const min = Math.round(sec / 60);
  if (min < 60) return `${min} min ago`;
  const h = Math.round(min / 60);
  if (h < 24) return `${h} h ago`;
  const d = Math.round(h / 24);
  return d < 14 ? `${d} d ago` : (s ?? '').slice(0, 10);
}

export function originOf(s: SessionInfo): string {
  return s.origin || 'tui';
}

export function sessionMeta(s: SessionInfo, now: number): string {
  return [s.agent, s.project ? `project ${s.project}` : '', `from ${originOf(s)}`, ago(s.last_active, now)].filter(Boolean).join(' · ');
}

export function filterSessions(list: SessionInfo[], query: string): SessionInfo[] {
  const words = query.toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return list;
  return list.filter((s) => {
    const hay = [s.name, s.agent, s.project ?? '', originOf(s), s.status, s.model ?? '', s.readonly ? 'open elsewhere' : ''].join(' ').toLowerCase();
    return words.every((w) => hay.includes(w));
  });
}

/** One line of the grouped list: `session` is null for a header of a parent that is not in the list. */
export type SessionLine = { id: string; session: SessionInfo | null; label: string; depth: number; kids: number };

/**
 * Sessions as a tree: one that has a `parent` sits under it, one whose parent is missing under a header with the parent's name.
 * A group shows only its head unless its id is in `open`. Groups are ordered by the newest activity inside them.
 */
export function groupSessions(list: SessionInfo[], open: ReadonlySet<string>): SessionLine[] {
  const byId = new Map(list.map((s) => [s.id, s] as const));
  const kids = new Map<string, string[]>();
  const top: string[] = [];
  const parentNames = new Map<string, string>();
  for (const s of list) {
    const p = s.parent && s.parent !== s.id ? s.parent : '';
    if (!p) {
      top.push(s.id);
      continue;
    }
    if (!byId.has(p)) {
      if (!kids.has(p)) top.push(p);
      if (s.parentName) parentNames.set(p, s.parentName);
    }
    kids.set(p, [...(kids.get(p) ?? []), s.id]);
  }
  const reached = new Set<string>(); // a parent loop (a -> b -> a) has no top level: its first member becomes one
  const reach = (id: string): void => {
    if (reached.has(id)) return;
    reached.add(id);
    for (const k of kids.get(id) ?? []) reach(k);
  };
  top.forEach(reach);
  for (const s of list) {
    if (!reached.has(s.id)) {
      top.push(s.id);
      reach(s.id);
    }
  }
  const stamp = (id: string, stack: string[] = []): number => {
    const t = Date.parse(byId.get(id)?.last_active ?? '');
    return Math.max(Number.isNaN(t) ? 0 : t, ...(kids.get(id) ?? []).filter((k) => !stack.includes(k) && k !== id).map((k) => stamp(k, [...stack, id])));
  };
  const count = (id: string, stack: string[] = []): number =>
    (kids.get(id) ?? []).filter((k) => !stack.includes(k) && k !== id).reduce((n, k) => n + 1 + count(k, [...stack, id]), 0);
  const out: SessionLine[] = [];
  const walk = (id: string, depth: number, stack: string[]): void => {
    out.push({ id, session: byId.get(id) ?? null, label: byId.has(id) ? '' : parentNames.get(id) || id, depth, kids: count(id, stack) });
    if (!open.has(id) || stack.includes(id)) return;
    for (const k of [...(kids.get(id) ?? [])].sort((a, b) => stamp(b) - stamp(a))) if (!stack.includes(k) && k !== id) walk(k, depth + 1, [...stack, id]);
  };
  for (const t of [...top].sort((a, b) => stamp(b) - stamp(a))) walk(t, 0, []);
  return out;
}

// ---- Fusszeile der Einstellungen: "New chat starts with ..." ----
export const SUMMARY_KEYS = ['defaults.provider', 'defaults.model', 'defaults.effort', 'defaults.context', 'defaults.mode'] as const;

export function summaryParts(fields: SettingsField[]): { text: string; mode: boolean }[] {
  const out: { text: string; mode: boolean }[] = [];
  for (const k of SUMMARY_KEYS) {
    const f = fields.find((x) => x.key === k);
    if (f && f.value !== null && f.value !== '') out.push({ text: String(f.value), mode: k === 'defaults.mode' });
  }
  return out;
}

// ---- Tool blocks: one line for a run of consecutive calls (as in the TUI) ----
const TOOL_CATS: Record<string, { names: string[]; one: string; many: string; doing: string }> = {
  read: { names: ['read', 'read_file', 'notebookread', 'view'], one: 'file read', many: 'files read', doing: 'reading a file' },
  search: { names: ['glob', 'grep', 'toolsearch', 'search', 'find', 'ls', 'list_files'], one: 'search', many: 'searches', doing: 'searching' },
  command: { names: ['bash', 'bashoutput', 'killshell', 'killbash', 'monitor', 'shell', 'run_command'], one: 'command', many: 'commands', doing: 'running a command' },
  edit: { names: ['edit', 'multiedit', 'write', 'notebookedit', 'write_file', 'edit_file'], one: 'edit', many: 'edits', doing: 'editing a file' },
  web: { names: ['webfetch', 'websearch'], one: 'web lookup', many: 'web lookups', doing: 'looking something up on the web' },
  agent: { names: ['task', 'agent'], one: 'sub-agent', many: 'sub-agents', doing: 'a sub-agent is working' },
  todo: { names: ['todowrite', 'todoread'], one: 'to-do update', many: 'to-do updates', doing: 'updating the to-dos' },
  other: { names: [], one: 'other call', many: 'other calls', doing: 'using a tool' },
};
export function toolCategory(name: string): string {
  const n = name.trim().toLowerCase();
  if (n.startsWith('\u25c6')) return 'agent';
  return Object.keys(TOOL_CATS).find((k) => TOOL_CATS[k]!.names.includes(n)) ?? 'other';
}
/** "5 tool calls: 3 files read, 1 command, 1 edit": counted by kind in the order the kinds first appear; never the raw command. */
export function toolSummary(names: string[]): string {
  const counts = new Map<string, number>();
  for (const n of names) counts.set(toolCategory(n), (counts.get(toolCategory(n)) ?? 0) + 1);
  const parts = [...counts].map(([c, k]) => `${k} ${k === 1 ? TOOL_CATS[c]!.one : TOOL_CATS[c]!.many}`);
  return `${names.length} tool call${names.length === 1 ? '' : 's'}: ${parts.join(', ')}`;
}
export function toolDoing(name: string): string {
  return TOOL_CATS[toolCategory(name)]!.doing;
}
/** The mode after `current` in the order the backend lists them (auto, plan, bypass); an unknown current starts at the first. */
export function nextMode(modes: string[], current: string | null): string | null {
  if (!modes.length) return null;
  const i = current === null ? -1 : modes.indexOf(current);
  return modes[(i + 1) % modes.length] ?? null;
}
