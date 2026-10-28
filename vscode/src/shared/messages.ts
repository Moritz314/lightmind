// Nachrichten Webview -> Erweiterung. Jede Nachricht wird gegen dieses Schema geprueft;
// unbekannte Typen und Felder werden verworfen, Strings und Zahlen sind begrenzt.

export type WebviewMessage =
  | { type: 'ready' }
  | { type: 'send'; text: string }
  | { type: 'cancel' }
  | { type: 'subCancel'; id: string }
  | { type: 'answer'; question: string; choice: string }
  | { type: 'newSession' }
  | { type: 'pickSession' }
  | { type: 'settings' }
  | { type: 'sessionsGet' }
  | { type: 'archiveGet' }
  | { type: 'sessionRestore'; id: string }
  | { type: 'openSession'; id: string }
  | { type: 'notesGet'; id: string }
  | { type: 'notesName'; id: string; field: string; text: string }
  | { type: 'sessionEnd'; id: string }
  | { type: 'choicesGet' }
  | { type: 'sessionSet'; field: string; value: string }
  | { type: 'settingsGet' }
  | { type: 'settingsPick'; key: string; index: number }
  | { type: 'settingsText'; key: string; text: string }
  | { type: 'restart' }
  | { type: 'toggleFollow' }
  | { type: 'openFile'; call: string; index: number }
  | { type: 'openDiff'; call: string; index: number }
  | { type: 'openLink'; url: string }
  | { type: 'copy'; text: string };

type FieldSpec = { kind: 'str'; max: number; min?: number } | { kind: 'int'; max: number };

const SCHEMA: Record<string, Record<string, FieldSpec>> = {
  ready: {},
  send: { text: { kind: 'str', min: 1, max: 100_000 } },
  cancel: {},
  subCancel: { id: { kind: 'str', min: 1, max: 200 } },
  answer: { question: { kind: 'str', min: 1, max: 200 }, choice: { kind: 'str', min: 1, max: 20_000 } },
  newSession: {},
  pickSession: {},
  settings: {},
  sessionsGet: {},
  archiveGet: {},
  sessionRestore: { id: { kind: 'str', min: 1, max: 500 } },
  openSession: { id: { kind: 'str', min: 1, max: 500 } },
  notesGet: { id: { kind: 'str', min: 1, max: 500 } },
  notesName: { id: { kind: 'str', min: 1, max: 500 }, field: { kind: 'str', min: 1, max: 20 }, text: { kind: 'str', max: 400 } },
  sessionEnd: { id: { kind: 'str', min: 1, max: 500 } },
  choicesGet: {},
  sessionSet: { field: { kind: 'str', min: 1, max: 20 }, value: { kind: 'str', min: 1, max: 80 } },
  settingsGet: {},
  settingsPick: { key: { kind: 'str', min: 1, max: 200 }, index: { kind: 'int', max: 199 } },
  settingsText: { key: { kind: 'str', min: 1, max: 200 }, text: { kind: 'str', max: 2000 } },
  restart: {},
  toggleFollow: {},
  openFile: { call: { kind: 'str', min: 1, max: 200 }, index: { kind: 'int', max: 49 } },
  openDiff: { call: { kind: 'str', min: 1, max: 200 }, index: { kind: 'int', max: 49 } },
  openLink: { url: { kind: 'str', min: 1, max: 2000 } },
  copy: { text: { kind: 'str', min: 1, max: 200_000 } },
};

export function parseWebviewMessage(raw: unknown): WebviewMessage | null {
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) return null;
  const r = raw as Record<string, unknown>;
  const type = r.type;
  if (typeof type !== 'string' || !Object.prototype.hasOwnProperty.call(SCHEMA, type)) return null;
  const spec = SCHEMA[type];
  const out: Record<string, unknown> = { type };
  for (const [key, f] of Object.entries(spec)) {
    const v = r[key];
    if (f.kind === 'str') {
      if (typeof v !== 'string' || v.length < (f.min ?? 0) || v.length > f.max) return null;
      out[key] = v;
    } else {
      if (typeof v !== 'number' || !Number.isInteger(v) || v < 0 || v > f.max) return null;
      out[key] = v;
    }
  }
  return out as WebviewMessage;
}
