// Reine Logik der Einstellungsansicht (ohne DOM), in Node testbar.
import type { SettingsField, SettingValue } from './protocol.ts';

export type SettingsGroup = { id: string; title: string; fields: SettingsField[] };

const TITLES: Record<string, string> = {
  defaults: 'Defaults',
  sessions: 'Sessions',
  vscode: 'VS Code',
  ui: 'Interface',
  run: 'Run',
  workflow: 'Ultracode',
  general: 'General',
};

export function groupId(key: string): string {
  const i = key.indexOf('.');
  return i > 0 ? key.slice(0, i) : 'general';
}

export function groupTitle(id: string): string {
  if (TITLES[id]) return TITLES[id];
  const t = id.replace(/[_-]+/g, ' ');
  return t.charAt(0).toUpperCase() + t.slice(1);
}

/** Gruppen nach Schluesselpraefix, in der Reihenfolge des ersten Auftretens. */
export function groupFields(fields: SettingsField[]): SettingsGroup[] {
  const out: SettingsGroup[] = [];
  for (const f of fields) {
    const id = groupId(f.key);
    let g = out.find((x) => x.id === id);
    if (!g) {
      g = { id, title: groupTitle(id), fields: [] };
      out.push(g);
    }
    g.fields.push(f);
  }
  return out;
}

export function fieldTitle(f: SettingsField): string {
  if (f.label) return f.label;
  const last = f.key.slice(f.key.lastIndexOf('.') + 1).replace(/[_-]+/g, ' ');
  return last.charAt(0).toUpperCase() + last.slice(1);
}

export function showValue(v: SettingValue | null): string {
  if (v === null) return 'not set';
  if (v === true) return 'yes';
  if (v === false) return 'no';
  return String(v);
}

export function choiceIndex(f: SettingsField): number {
  return f.choices.findIndex((c) => c === f.value);
}
