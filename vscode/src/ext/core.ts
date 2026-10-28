// Kern der Erweiterung ohne VS-Code-Import: Zustand, Backend-Ueberwachung, Webview-Nachrichten.
import { normalizeEvent, normalizeFields, normalizeHello, normalizeNotes, normalizeSession, safeHref } from '../shared/protocol.ts';
import type { BackendEvent, Hello, SessionInfo, SettingsField } from '../shared/protocol.ts';
import { CHIP_FIELDS, choicesFromFields } from '../shared/views.ts';
import type { ChipField } from '../shared/views.ts';
import { parseWebviewMessage } from '../shared/messages.ts';
import type { WebviewMessage } from '../shared/messages.ts';
import { initialState, reduce } from '../shared/reducer.ts';
import type { Action, State, ToolItem } from '../shared/reducer.ts';
import { isInside, MarkTracker } from '../shared/marks.ts';
import type { MarkSnapshot } from '../shared/marks.ts';
import type { BackendLike } from './backend.ts';

export interface UiHost {
  pickSession(sessions: SessionInfo[], archived?: boolean): Promise<string | 'new' | 'archive' | undefined>;
  pickAgentModel(agents: string[], models: string[]): Promise<{ agent?: string; model?: string } | undefined>;
  openSettings(tree: string): void;
  openFile(path: string, diff: boolean): void;
  openExternal(url: string): void;
  copy(text: string): void;
  error(text: string): void;
  marks(snap: MarkSnapshot): void;
  followStart(files: string[]): void;
  followEnd(files: string[]): void;
  setFollow(on: boolean): void;
}

export type WebviewPort = { post(msg: unknown): void };

export type CoreOptions = {
  workdir: string | null;
  blocked?: string;
  tree: string; // aus der Einstellung; hello ueberschreibt
  makeBackend: () => BackendLike;
  ui: UiHost;
  log: (s: string) => void;
  follow: boolean;
  restartDelaysMs?: number[];
  shutdownGraceMs?: number;
  /** Frist bis zum ersten `hello` nach dem Start (Standard 20 s). */
  helloTimeoutMs?: number;
  /** Frist beim manuellen Neustart, bis der alte Prozess beendet wird (Standard 1,5 s). */
  restartGraceMs?: number;
};

export class ChatCore {
  state: State = initialState();
  readonly marks = new MarkTracker();
  private o: CoreOptions;
  private port: WebviewPort | null = null;
  private backend: BackendLike | null = null;
  private hello: Hello | null = null;
  private disposed = false;
  private stopping = false;
  private attempts = 0;
  private startedAt = 0;
  private restartTimer: ReturnType<typeof setTimeout> | null = null;
  private helloTimer: ReturnType<typeof setTimeout> | null = null;
  private resumeId: string | null = null;
  private opening: Promise<boolean> | null = null;
  private generation = 0;
  private fields: SettingsField[] = [];
  private agents: unknown[] = []; // roh weitergereicht, die Webview prueft sie
  /** Wahl aus den Chips ohne offene Sitzung: gilt fuer die naechste neue. */
  private pending: Partial<Record<ChipField, string>> = {};

  constructor(o: CoreOptions) {
    this.o = o;
    this.state = { ...this.state, follow: o.follow };
    this.marks.setRoots([o.workdir ?? '', o.tree]);
  }

  get tree(): string {
    return this.hello?.tree || this.o.tree;
  }

  // ---- Webview ----
  attach(port: WebviewPort): void {
    this.port = port;
  }
  detach(): void {
    this.port = null;
  }

  dispatch(a: Action): void {
    this.state = reduce(this.state, a);
    this.port?.post({ type: 'action', action: a });
  }

  // ---- Backend ----
  start(): void {
    if (this.disposed) return;
    if (this.o.blocked) {
      this.dispatch({ kind: 'conn', status: 'down', detail: this.o.blocked });
      return;
    }
    if (!this.o.workdir) {
      this.dispatch({ kind: 'conn', status: 'down', detail: 'Open a folder to use Lightmind.' });
      return;
    }
    this.dispatch({ kind: 'conn', status: this.state.session ? 'restarting' : 'starting' });
    const gen = ++this.generation;
    const b = this.o.makeBackend();
    this.backend = b;
    this.startedAt = Date.now();
    b.onLog = (t) => this.o.log(t);
    b.onHello = (raw) => {
      if (gen !== this.generation) return;
      this.clearHelloTimer();
      void this.onHello(raw);
    };
    b.onEvent = (raw) => {
      if (gen !== this.generation) return;
      const ev = normalizeEvent(raw);
      if (!ev) {
        this.o.log(`ignored event: ${JSON.stringify(raw).slice(0, 200)}`);
        return;
      }
      this.onEvent(ev);
    };
    b.onExit = (info) => {
      if (gen !== this.generation) return;
      this.clearHelloTimer();
      this.onExit(info);
    };
    b.start();
    this.clearHelloTimer();
    const ms = this.o.helloTimeoutMs ?? 20_000;
    this.helloTimer = setTimeout(() => {
      this.helloTimer = null;
      if (gen !== this.generation || this.disposed) return;
      this.generation++; // spaete Meldungen und das Ende des alten Prozesses zaehlen nicht mehr
      this.backend = null;
      this.marks.endAll();
      this.o.ui.marks(this.marks.snapshot());
      if (this.state.session && !this.resumeId) this.resumeId = this.state.session.id;
      this.dispatch({
        kind: 'conn',
        status: 'down',
        detail: `The Lightmind backend did not answer within ${Math.round(ms / 1000)} s. See the "Lightmind" output channel, then press Retry.`,
      });
      void b.shutdown(this.o.shutdownGraceMs ?? 5000);
    }, ms);
  }

  private clearHelloTimer(): void {
    if (this.helloTimer) clearTimeout(this.helloTimer);
    this.helloTimer = null;
  }

  private async onHello(raw: unknown): Promise<void> {
    const h = normalizeHello(raw);
    if (!h) return;
    if (h.version !== 1) {
      this.dispatch({ kind: 'conn', status: 'down', detail: `Unsupported protocol version ${h.version} (expected 1). Update Lightmind or the extension.` });
      this.stopping = true;
      void this.backend?.shutdown(2000);
      return;
    }
    this.hello = h;
    this.marks.setRoots([this.o.workdir ?? '', h.tree]);
    this.dispatch({ kind: 'conn', status: 'ready' });
    const resume = this.resumeId;
    this.resumeId = null;
    if (resume) {
      const ok = await this.openSession(resume, true);
      if (ok) this.dispatch({ kind: 'localnotice', level: 'info', text: 'Backend restarted, session resumed. A running turn was interrupted.' });
    }
  }

  private onEvent(ev: BackendEvent): void {
    this.dispatch({ kind: 'event', event: ev });
    if (ev.type === 'tool.start') {
      const used = this.marks.start(ev.call, ev.files);
      if (used.length) {
        this.o.ui.marks(this.marks.snapshot());
        if (this.state.follow) this.o.ui.followStart(used);
      }
    } else if (ev.type === 'tool.end') {
      const r = this.marks.end(ev.call, ev.files);
      if (r.worked.length || r.changed.length) {
        this.o.ui.marks(this.marks.snapshot());
        if (this.state.follow && r.changed.length) this.o.ui.followEnd(r.changed);
      }
    } else if (ev.type === 'turn.end') {
      this.marks.endAll();
      this.o.ui.marks(this.marks.snapshot());
    }
  }

  private onExit(info: { code: number | null; signal: string | null; spawnError?: string }): void {
    this.backend = null;
    this.marks.endAll();
    this.o.ui.marks(this.marks.snapshot());
    if (this.disposed || this.stopping) return;
    if (this.state.session && !this.resumeId) this.resumeId = this.state.session.id;
    if (info.spawnError) {
      this.dispatch({
        kind: 'conn',
        status: 'down',
        detail: `Cannot start the Lightmind backend (${info.spawnError}). Check the setting "lightmind.home" (${this.o.tree}).`,
      });
      return;
    }
    if (Date.now() - this.startedAt > 30_000) this.attempts = 0;
    const delays = this.o.restartDelaysMs ?? [500, 1500, 4000, 8000, 15000];
    if (this.attempts >= delays.length) {
      this.dispatch({ kind: 'conn', status: 'down', detail: `The Lightmind backend keeps stopping (last exit code ${info.code ?? info.signal}). See the "Lightmind" output channel.` });
      return;
    }
    const delay = delays[this.attempts++];
    this.dispatch({ kind: 'conn', status: 'restarting', detail: `Backend stopped (exit ${info.code ?? info.signal}). Restarting...` });
    this.restartTimer = setTimeout(() => {
      this.restartTimer = null;
      this.start();
    }, delay);
  }

  /** Manueller Neustart (Knopf "Retry"). */
  async restart(): Promise<void> {
    if (this.disposed) return;
    if (this.restartTimer) clearTimeout(this.restartTimer);
    this.restartTimer = null;
    this.clearHelloTimer();
    const old = this.backend;
    this.generation++;
    this.backend = null;
    if (this.state.session) this.resumeId = this.state.session.id;
    this.attempts = 0;
    if (old) await old.shutdown(this.o.restartGraceMs ?? 1500);
    this.start();
  }

  async dispose(): Promise<void> {
    this.disposed = true;
    if (this.restartTimer) clearTimeout(this.restartTimer);
    this.clearHelloTimer();
    this.generation++;
    const b = this.backend;
    this.backend = null;
    this.port = null;
    if (b) await b.shutdown(this.o.shutdownGraceMs ?? 5000);
  }

  // ---- Anfragen ----
  private modesError = '';

  private req(type: string, fields: Record<string, unknown> = {}): Promise<Record<string, unknown>> {
    const b = this.backend;
    if (!b || this.state.conn.status !== 'ready') return Promise.reject(new Error('Lightmind backend is not ready'));
    return b.request(type, fields);
  }

  private fail(e: unknown): void {
    const text = e instanceof Error ? e.message : String(e);
    this.dispatch({ kind: 'localerror', text });
  }

  async newSession(fields: { agent?: string; model?: string } = {}): Promise<boolean> {
    if (this.opening) return this.opening;
    this.opening = (async () => {
      try {
        const data = await this.req('session.new', { ...this.pending, ...fields });
        const s = normalizeSession(data.session);
        if (!s) throw new Error('session.new returned no session');
        this.pending = {};
        this.marks.clear();
        this.o.ui.marks(this.marks.snapshot());
        this.dispatch({ kind: 'open', session: s, history: [] });
        return true;
      } catch (e) {
        this.fail(e);
        return false;
      } finally {
        this.opening = null;
      }
    })();
    return this.opening;
  }

  async openSession(id: string, silent = false): Promise<boolean> {
    try {
      await this.doOpen(id);
      return true;
    } catch (e) {
      if (!silent) this.fail(e);
      else this.dispatch({ kind: 'localnotice', level: 'warn', text: `Could not resume the session: ${e instanceof Error ? e.message : String(e)}` });
      return false;
    }
  }

  private async doOpen(id: string): Promise<void> {
    const data = await this.req('session.open', { session: id });
    const s = normalizeSession(data.session);
    if (!s) throw new Error('session.open returned no session');
    const hist: BackendEvent[] = [];
    if (Array.isArray(data.history)) {
      for (const h of data.history) {
        const ev = normalizeEvent(h);
        if (ev) hist.push(ev);
      }
    }
    this.marks.clear();
    this.o.ui.marks(this.marks.snapshot());
    this.dispatch({ kind: 'open', session: s, history: hist });
  }

  async newSessionFlow(): Promise<void> {
    let fields: SettingsField[] = [];
    try {
      fields = normalizeFields(await this.req('settings.get'));
    } catch {
      // Auswahl ohne Listen: Backend-Standard
    }
    const pick = (re: RegExp): string[] => (fields.find((f) => re.test(f.key) && f.choices.length)?.choices ?? []).filter((x): x is string => typeof x === 'string');
    const choice = await this.o.ui.pickAgentModel(pick(/(^|\.)agents?$/), pick(/(^|\.)model$/));
    if (!choice) return;
    await this.newSession(choice);
  }

  async pickSessionFlow(archived = false): Promise<void> {
    try {
      const data = await this.req('session.list', { limit: 50, ...(archived ? { archived: true } : {}) });
      const list: SessionInfo[] = [];
      if (Array.isArray(data.sessions)) for (const x of data.sessions) {
        const s = normalizeSession(x);
        if (s) list.push(s);
      }
      const id = await this.o.ui.pickSession(list, archived);
      if (!id) return;
      if (id === 'new') await this.newSessionFlow();
      else if (id === 'archive') await this.pickSessionFlow(true);
      else await this.openSession(id);          // an archived chat is restored by the backend when it is opened
    } catch (e) {
      this.fail(e);
    }
  }

  async send(text: string): Promise<void> {
    const t = text.trim();
    if (!t || this.state.conn.status !== 'ready') return;
    if (this.state.busy && (!this.state.session || this.state.session.readonly)) return;
    if (!this.state.session) {
      const ok = await this.newSession({});
      if (!ok) return;
    }
    const interject = this.state.busy;
    this.dispatch({ kind: 'user', text: t, interject });
    try {
      // While a turn runs the result carries {interject, state}; the matching interject events bind the id in the reducer.
      const data = await this.req('send', { text: t });
      // The turn had ended by the time the backend saw it: it is a normal message, not an interjection.
      if (interject && typeof data.interject !== 'string') this.dispatch({ kind: 'ijclear' });
    } catch (e) {
      if (interject) this.dispatch({ kind: 'ijfailed', text: e instanceof Error ? e.message : String(e) });
      else this.fail(e);
    }
  }

  private findTool(call: string): ToolItem | null {
    for (let i = this.state.items.length - 1; i >= 0; i--) {
      const it = this.state.items[i];
      if (it.k === 'tool' && it.call === call) return it;
    }
    return null;
  }

  private openReported(call: string, index: number, diff: boolean): void {
    const t = this.findTool(call);
    const p = t?.files[index];
    if (!p) return;
    if (!isInside(p, [this.o.workdir ?? '', this.tree])) {
      this.dispatch({ kind: 'localnotice', level: 'warn', text: 'Refused to open a file outside the workspace and the Lightmind tree.' });
      return;
    }
    this.o.ui.openFile(p, diff);
  }

  async handleMessage(raw: unknown): Promise<void> {
    const m: WebviewMessage | null = parseWebviewMessage(raw);
    if (!m) {
      this.o.log(`rejected webview message: ${JSON.stringify(raw)?.slice(0, 200)}`);
      return;
    }
    switch (m.type) {
      case 'ready':
        this.port?.post({ type: 'init', state: this.state });
        break;
      case 'send':
        await this.send(m.text);
        break;
      case 'cancel':
        if (!this.state.busy) break;
        this.dispatch({ kind: 'cancelling' });
        try {
          await this.req('cancel');
        } catch (e) {
          this.fail(e);
        }
        break;
      case 'subCancel': {
        // Stoppt einen Hintergrund-Unteragenten; ein vordergruendiger gehoert zum Zug und wird mit Stopp beendet.
        const known = this.state.subs.find((b) => b.id === m.id && !b.done) ?? this.state.flows.find((w) => w.id === m.id && !w.done);
        if (!known) break;
        try {
          await this.req('subagent.cancel', { sub: m.id });
        } catch (e) {
          this.dispatch({ kind: 'localnotice', level: 'warn', text: `Could not stop ${known.name}: ${e instanceof Error ? e.message : String(e)}` });
        }
        break;
      }
      case 'answer': {
        const q = this.state.items.find((it) => it.k === 'question' && it.question === m.question && it.answered === null);
        if (!q) break;
        this.dispatch({ kind: 'answered', question: m.question, choice: m.choice });
        try {
          await this.req('answer', { question: m.question, choice: m.choice });
        } catch (e) {
          this.fail(e);
        }
        break;
      }
      case 'newSession':
        await this.newSessionFlow();
        break;
      case 'pickSession':
        await this.pickSessionFlow();
        break;
      case 'settings':
        this.o.ui.openSettings(this.tree);
        break;
      case 'sessionsGet':
        await this.sendSessions();
        break;
      case 'archiveGet':
        await this.sendSessions(true);
        break;
      case 'sessionRestore':
        try {
          const r = await this.req('session.unarchive', { session: m.id });
          this.port?.post({ type: 'sessionMoved', id: m.id, ok: true, text: typeof r.text === 'string' ? r.text.slice(0, 300) : '' });
        } catch (e) {
          this.port?.post({ type: 'sessionMoved', id: m.id, ok: false, text: e instanceof Error ? e.message : String(e) });
        }
        await this.sendSessions(true);
        break;
      case 'openSession':
        try {
          await this.doOpen(m.id);
          this.port?.post({ type: 'view', name: 'chat' });
        } catch (e) {
          this.port?.post({ type: 'sessionsError', error: e instanceof Error ? e.message : String(e) });
        }
        break;
      case 'notesGet':
        try {
          const notes = normalizeNotes(await this.req('notes.get', { session: m.id }));
          if (!notes) throw new Error('notes.get returned no notes');
          this.port?.post({ type: 'notes', notes });
        } catch (e) {
          this.port?.post({ type: 'notesError', id: m.id, error: e instanceof Error ? e.message : String(e) });
        }
        break;
      case 'notesName':
        try {
          const r = await this.req('notes.name', { session: m.id, field: m.field, text: m.text });
          const t = (v: unknown): string => (typeof v === 'string' ? v.slice(0, 500) : '');
          this.port?.post({ type: 'notesSaved', id: m.id, field: m.field, ok: true, title: t(r.title), description: t(r.description) });
        } catch (e) {
          this.port?.post({ type: 'notesSaved', id: m.id, field: m.field, ok: false, error: e instanceof Error ? e.message : String(e) });
        }
        break;
      case 'sessionEnd':
        try {
          const r = await this.req('session.end', { session: m.id });
          this.port?.post({ type: 'sessionEnded', id: m.id, ok: true, text: typeof r.text === 'string' ? r.text.slice(0, 300) : '' });
        } catch (e) {
          this.port?.post({ type: 'sessionEnded', id: m.id, ok: false, text: e instanceof Error ? e.message : String(e) });
        }
        await this.sendSessions();
        break;
      case 'choicesGet':
        await this.sendChoices(true);
        break;
      case 'sessionSet':
        await this.setChip(m.field, m.value);
        break;
      case 'settingsGet':
        await this.loadSettings();
        break;
      case 'settingsPick': {
        const f = this.fields.find((x) => x.key === m.key);
        const v = f ? f.choices[m.index] : undefined;
        if (v === undefined) this.port?.post({ type: 'settingsResult', key: m.key, ok: false, error: 'That choice is not on the list.' });
        else await this.saveSetting(m.key, v);
        break;
      }
      case 'settingsText': {
        const f = this.fields.find((x) => x.key === m.key);
        if (!f) {
          this.port?.post({ type: 'settingsResult', key: m.key, ok: false, error: 'Unknown setting.' });
          break;
        }
        let v: string | number = m.text.trim();
        if (typeof f.value === 'number') {
          if (!/^-?\d+$/.test(v)) {
            this.port?.post({ type: 'settingsResult', key: m.key, ok: false, error: 'A whole number is needed.' });
            break;
          }
          v = Number(v);
        }
        await this.saveSetting(m.key, v);
        break;
      }
      case 'restart':
        await this.restart();
        break;
      case 'toggleFollow':
        this.setFollow(!this.state.follow);
        break;
      case 'openFile':
        this.openReported(m.call, m.index, false);
        break;
      case 'openDiff':
        this.openReported(m.call, m.index, true);
        break;
      case 'openLink': {
        const href = safeHref(m.url);
        if (href) this.o.ui.openExternal(href);
        break;
      }
      case 'copy':
        this.o.ui.copy(m.text);
        break;
    }
  }

  /** Oeffnet die Sitzungsliste in der Webview (Befehl "Lightmind: Open session..."). */
  showSessions(): void {
    this.port?.post({ type: 'view', name: 'sessions' });
  }

  /** Sends the list of the sessions view: the recent chats, or (archived) the archive; the view ignores a list of the other kind. */
  private async sendSessions(archived = false): Promise<void> {
    try {
      const data = await this.req('session.list', { limit: 100, ...(archived ? { archived: true } : {}) });
      const list: SessionInfo[] = [];
      if (Array.isArray(data.sessions)) for (const x of data.sessions) {
        const s = normalizeSession(x);
        if (s) list.push(s);
      }
      this.port?.post({ type: 'sessions', sessions: list, archived });
    } catch (e) {
      this.port?.post({ type: 'sessions', sessions: [], archived, error: e instanceof Error ? e.message : String(e) });
    }
  }

  /** Listen und Vorgaben der Chips unter dem Eingabefeld (aus settings.get). */
  private async sendChoices(fetch: boolean): Promise<void> {
    let error: string | undefined;
    if (fetch) {
      try {
        const res = await this.req('settings.get');
        this.fields = normalizeFields(res);
        this.modesError = typeof res.modes_error === 'string' ? res.modes_error.slice(0, 500) : '';
      } catch (e) {
        this.modesError = '';
        error = e instanceof Error ? e.message : String(e);
      }
    }
    this.port?.post({ type: 'choices', ...choicesFromFields(this.fields, this.pending), ...(error ? { error } : {}), ...(this.modesError ? { modeError: this.modesError } : {}) });
  }

  /** Modell, Aufwand oder Modus waehlen: gilt fuer die offene Sitzung, sonst fuer die naechste neue. */
  private async setChip(field: string, value: string): Promise<void> {
    if (!(CHIP_FIELDS as readonly string[]).includes(field)) return;
    const f = field as ChipField;
    const s = this.state.session;
    if (s?.readonly) {
      this.dispatch({ kind: 'localnotice', level: 'warn', text: 'This session is open elsewhere and read only here.' });
      return;
    }
    if (!s) {
      this.pending = { ...this.pending, [f]: value };
      await this.sendChoices(false);
      return;
    }
    try {
      await this.req('session.set', { [f]: value });
    } catch (e) {
      this.fail(e);
    }
  }

  /** Oeffnet die Einstellungsansicht in der Webview (Befehl "Lightmind: Settings"). */
  showSettings(): void {
    this.port?.post({ type: 'view', name: 'settings' });
  }

  private async loadSettings(): Promise<void> {
    try {
      const data = await this.req('settings.get');
      this.fields = normalizeFields(data);
      this.modesError = typeof data.modes_error === 'string' ? data.modes_error.slice(0, 500) : '';
      this.agents = Array.isArray(data.agents) ? data.agents.slice(0, 200) : [];
      this.port?.post({ type: 'settings', fields: this.fields, agents: this.agents });
      this.port?.post({ type: 'choices', ...choicesFromFields(this.fields, this.pending), ...(this.modesError ? { modeError: this.modesError } : {}) });
    } catch (e) {
      this.port?.post({ type: 'settings', fields: this.fields, agents: this.agents, error: e instanceof Error ? e.message : String(e) });
    }
  }

  private async saveSetting(key: string, value: string | number | boolean): Promise<void> {
    try {
      await this.req('settings.set', { changes: { [key]: value } });
    } catch (e) {
      this.port?.post({ type: 'settingsResult', key, ok: false, error: e instanceof Error ? e.message : String(e) });
      return;
    }
    this.port?.post({ type: 'settingsResult', key, ok: true });
    await this.loadSettings();
  }

  setFollow(on: boolean): void {
    this.dispatch({ kind: 'follow', on });
    this.o.ui.setFollow(on);
  }

  clearMarks(): void {
    this.marks.clear();
    this.o.ui.marks(this.marks.snapshot());
  }
}
