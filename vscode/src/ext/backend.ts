// Prozess-Huelle um `lm serve --stdio`: JSON-Zeilen, Anfragen mit IDs, Ereignisse.
import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { StringDecoder } from 'node:string_decoder';
import type { SpawnSpec } from './binary.ts';

export const MAX_LINE = 16 * 1024 * 1024;

/** Setzt Datenbloecke zu vollstaendigen Zeilen zusammen (UTF-8-sicher, mit Laengenbremse). */
export class LineSplitter {
  private dec = new StringDecoder('utf8');
  private buf = '';
  private skipping = false;
  onLine: (line: string) => void;
  constructor(onLine: (line: string) => void) {
    this.onLine = onLine;
  }
  push(chunk: Buffer | string): void {
    this.buf += typeof chunk === 'string' ? chunk : this.dec.write(chunk);
    let nl: number;
    while ((nl = this.buf.indexOf('\n')) >= 0) {
      const line = this.buf.slice(0, nl);
      this.buf = this.buf.slice(nl + 1);
      if (this.skipping) {
        this.skipping = false;
        continue;
      }
      const t = line.endsWith('\r') ? line.slice(0, -1) : line;
      if (t.trim() !== '') this.onLine(t);
    }
    if (this.buf.length > MAX_LINE) {
      this.buf = '';
      this.skipping = true;
    }
  }
}

export interface BackendLike {
  start(): void;
  request(type: string, fields?: Record<string, unknown>): Promise<Record<string, unknown>>;
  notify(obj: Record<string, unknown>): void;
  shutdown(graceMs: number): Promise<void>;
  onHello: (hello: unknown) => void;
  onEvent: (ev: unknown) => void;
  onExit: (info: { code: number | null; signal: string | null; spawnError?: string }) => void;
  onLog: (text: string) => void;
}

export class Backend implements BackendLike {
  onHello: (hello: unknown) => void = () => {};
  onEvent: (ev: unknown) => void = () => {};
  onExit: (info: { code: number | null; signal: string | null; spawnError?: string }) => void = () => {};
  onLog: (text: string) => void = () => {};
  private spec: SpawnSpec;
  private env: NodeJS.ProcessEnv;
  private proc: ChildProcessWithoutNullStreams | null = null;
  private pending = new Map<string, { resolve: (d: Record<string, unknown>) => void; reject: (e: Error) => void }>();
  private nextId = 1;
  private exited = false;
  private exitWaiters: Array<() => void> = [];

  constructor(spec: SpawnSpec, env: NodeJS.ProcessEnv = process.env) {
    this.spec = spec;
    this.env = env;
  }

  start(): void {
    const p = spawn(this.spec.command, this.spec.args, {
      cwd: this.spec.cwd,
      stdio: ['pipe', 'pipe', 'pipe'],
      shell: false,
      windowsHide: true,
      env: { ...this.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' },
    });
    this.proc = p;
    let spawnError: string | undefined;
    const out = new LineSplitter((line) => this.handleLine(line));
    p.stdout.on('data', (c: Buffer) => out.push(c));
    const err = new LineSplitter((line) => this.onLog(line));
    p.stderr.on('data', (c: Buffer) => err.push(c));
    p.stdin.on('error', (e) => this.onLog(`stdin: ${e.message}`));
    p.on('error', (e) => {
      spawnError = e.message;
      this.finish({ code: null, signal: null, spawnError });
    });
    p.on('close', (code, signal) => this.finish({ code, signal, spawnError }));
  }

  private finish(info: { code: number | null; signal: string | null; spawnError?: string }): void {
    if (this.exited) return;
    this.exited = true;
    for (const [, w] of this.pending) w.reject(new Error('backend stopped'));
    this.pending.clear();
    for (const w of this.exitWaiters) w();
    this.exitWaiters = [];
    this.onExit(info);
  }

  private handleLine(line: string): void {
    let obj: unknown;
    try {
      obj = JSON.parse(line);
    } catch {
      this.onLog(`non-JSON line: ${line.slice(0, 200)}`);
      return;
    }
    if (typeof obj !== 'object' || obj === null) return;
    const o = obj as Record<string, unknown>;
    if (o.type === 'hello') return this.onHello(o);
    if (o.type === 'result') {
      const id = typeof o.id === 'string' ? o.id : '';
      const w = this.pending.get(id);
      if (!w) return;
      this.pending.delete(id);
      if (o.ok === true) w.resolve(typeof o.data === 'object' && o.data !== null ? (o.data as Record<string, unknown>) : {});
      else w.reject(new Error(typeof o.error === 'string' ? o.error : 'request failed'));
      return;
    }
    this.onEvent(o);
  }

  notify(obj: Record<string, unknown>): void {
    const p = this.proc;
    if (!p || this.exited || !p.stdin.writable) return;
    p.stdin.write(JSON.stringify(obj) + '\n');
  }

  request(type: string, fields: Record<string, unknown> = {}): Promise<Record<string, unknown>> {
    return new Promise((resolve, reject) => {
      if (!this.proc || this.exited) return reject(new Error('backend not running'));
      const id = `r${this.nextId++}`;
      this.pending.set(id, { resolve, reject });
      this.notify({ ...fields, type, id });
    });
  }

  /** `shutdown` senden, auf sauberes Ende warten, erst nach Ablauf der Frist beenden. */
  async shutdown(graceMs: number): Promise<void> {
    if (!this.proc || this.exited) return;
    const done = new Promise<void>((r) => this.exitWaiters.push(r));
    this.notify({ type: 'shutdown' });
    const timer = new Promise<void>((r) => setTimeout(r, graceMs));
    await Promise.race([done, timer]);
    if (!this.exited) {
      this.onLog('backend did not exit after shutdown; terminating');
      this.proc.kill();
    }
  }
}
