// Kern mit echtem Backend-Prozess (fake-lm.mjs) und gespielter VS-Code-Schicht.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import * as path from 'node:path';
import { fileURLToPath } from 'node:url';
import { Backend } from '../src/ext/backend.ts';
import { ChatCore } from '../src/ext/core.ts';
import { makeHost } from './helpers.ts';

const here = path.dirname(fileURLToPath(import.meta.url));
const fake = path.join(here, 'fake-lm.mjs');


const until = async (fn: () => boolean, ms = 8000): Promise<void> => {
  const t = Date.now();
  while (!fn()) {
    if (Date.now() - t > ms) throw new Error('timeout: ' + fn.toString());
    await new Promise((r) => setTimeout(r, 20));
  }
};

function mk(workdir: string, logFile: string, delays = [50, 100, 200]) {
  const h = makeHost();
  const env = { ...process.env, FAKE_LM_LOG: logFile };
  const core = new ChatCore({
    workdir,
    tree: here,
    follow: true,
    ui: h.ui,
    log: () => {},
    restartDelaysMs: delays,
    shutdownGraceMs: 3000,
    makeBackend: () => new Backend({ command: process.execPath, args: [fake, 'serve', '--stdio', '--workdir', workdir], cwd: here }, env),
  });
  return { core, ...h };
}

const lines = (f: string): Array<Record<string, unknown>> =>
  readFileSync(f, 'utf8').split('\n').filter(Boolean).map((l) => JSON.parse(l));

test('Kern: kein Sitzungsstart von selbst; senden legt Sitzung an; Rechtefrage; Datei-Marken', async () => {
  const dir = mkdtempSync(path.join(tmpdir(), 'lmcore-'));
  const logFile = path.join(dir, 'req.log');
  const { core, calls } = mk(dir, logFile);
  core.start();
  await until(() => core.state.conn.status === 'ready');
  await new Promise((r) => setTimeout(r, 150));
  assert.equal(lines(logFile).some((m) => m.type === 'session.new' || m.type === 'session.open'), false, 'keine Sitzung von selbst');
  assert.equal(core.state.session, null);

  await core.handleMessage({ type: 'send', text: 'please run it' });
  assert.ok(core.state.session);
  await until(() => core.state.items.some((i) => i.k === 'question'));
  const q = core.state.items.find((i) => i.k === 'question');
  assert.ok(q && q.k === 'question');
  await core.handleMessage({ type: 'answer', question: q.question, choice: 'yes' });
  await until(() => !core.state.busy && core.state.items.some((i) => i.k === 'turnend'));
  const tools = core.state.items.filter((i) => i.k === 'tool');
  assert.equal(tools.length, 2);
  // Marken: Datei liegt im Workspace -> Folgemodus und Marke "geaendert"
  assert.ok(calls.some((c) => c[0] === 'followStart'));
  assert.ok(calls.some((c) => c[0] === 'followEnd'));
  assert.equal(core.marks.snapshot().changed.length, 1);
  assert.equal(core.marks.snapshot().working.length, 0);
  // Diff nur ueber gemeldete Aufrufnummer, nicht ueber Pfad
  const edit = tools.find((t) => t.k === 'tool' && t.name === 'Edit');
  assert.ok(edit && edit.k === 'tool');
  await core.handleMessage({ type: 'openDiff', call: edit.call, index: 0 });
  assert.ok(calls.some((c) => c[0] === 'openFile' && c[2] === true));
  await core.handleMessage({ type: 'openFile', call: 'gibtsnicht', index: 0 });
  assert.equal(calls.filter((c) => c[0] === 'openFile').length, 1);
  // ungueltige Nachricht
  await core.handleMessage({ type: 'openFile', path: '/etc/passwd' });
  assert.equal(calls.filter((c) => c[0] === 'openFile').length, 1);
  await core.dispose();
});

test('Kern: Einwurf waehrend des Zugs wird gebunden und gelesen', async () => {
  const dir = mkdtempSync(path.join(tmpdir(), 'lmcore-'));
  const { core } = mk(dir, path.join(dir, 'req.log'));
  core.start();
  await until(() => core.state.conn.status === 'ready');
  await core.handleMessage({ type: 'send', text: 'run' });
  await until(() => core.state.items.some((i) => i.k === 'question'));
  assert.ok(core.state.busy);
  await core.handleMessage({ type: 'send', text: 'also check the tests' });
  await until(() => core.state.items.some((i) => i.k === 'user' && i.ij?.state === 'delivered'));
  const u = core.state.items.find((i) => i.k === 'user' && i.text === 'also check the tests');
  assert.ok(u && u.k === 'user' && u.ij?.id === 'i1');
  assert.ok(core.state.busy);
  await core.handleMessage({ type: 'cancel' });
  await until(() => !core.state.busy);
  await core.dispose();
});

test('Kern: Abbruch bei offener Frage, kein Link mit javascript:', async () => {
  const dir = mkdtempSync(path.join(tmpdir(), 'lmcore-'));
  const { core, calls } = mk(dir, path.join(dir, 'req.log'));
  core.start();
  await until(() => core.state.conn.status === 'ready');
  await core.handleMessage({ type: 'send', text: 'run' });
  await until(() => core.state.items.some((i) => i.k === 'question'));
  await core.handleMessage({ type: 'cancel' });
  await until(() => !core.state.busy);
  const last = core.state.items[core.state.items.length - 1];
  assert.ok(last.k === 'turnend' && last.status === 'cancelled');
  await core.handleMessage({ type: 'openLink', url: 'javascript:alert(1)' });
  await core.handleMessage({ type: 'openLink', url: 'https://example.org/x' });
  assert.deepEqual(calls.filter((c) => c[0] === 'openExternal').map((c) => c[1]), ['https://example.org/x']);
  await core.dispose();
});

test('Kern: Absturz -> Neustart mit Wiederaufnahme der Sitzung', async () => {
  const dir = mkdtempSync(path.join(tmpdir(), 'lmcore-'));
  const logFile = path.join(dir, 'req.log');
  const { core } = mk(dir, logFile);
  core.start();
  await until(() => core.state.conn.status === 'ready');
  await core.handleMessage({ type: 'send', text: 'hello' });
  await until(() => !core.state.busy && core.state.items.some((i) => i.k === 'turnend'));
  const sid = core.state.session?.id;
  await core.handleMessage({ type: 'send', text: 'crash now' });
  await until(() => core.state.conn.status === 'restarting');
  await until(() => core.state.conn.status === 'ready');
  await until(() => lines(logFile).some((m) => m.type === 'session.open' && m.session === sid));
  await until(() => core.state.items.some((i) => i.k === 'notice'));
  assert.equal(core.state.session?.id, sid);
  assert.equal(core.state.busy, false);
  // zweiter Prozess wurde mit demselben Arbeitsordner gestartet
  const starts = lines(logFile).filter((m) => m.started);
  assert.equal(starts.length, 2);
  await core.dispose();
});

test('Kern: new session mit Agentenwahl sendet Agent nur bei Abweichung; Sitzungsliste mit origin', async () => {
  const dir = mkdtempSync(path.join(tmpdir(), 'lmcore-'));
  const logFile = path.join(dir, 'req.log');
  const { core, calls } = mk(dir, logFile);
  core.start();
  await until(() => core.state.conn.status === 'ready');
  await core.handleMessage({ type: 'newSession' });
  const pam = calls.find((c) => c[0] === 'pickAgentModel');
  assert.deepEqual(pam?.[1], ['coder', 'writer', 'reviewer']);
  const sn = lines(logFile).find((m) => m.type === 'session.new');
  assert.equal(sn?.agent, 'writer');
  assert.equal('model' in (sn ?? {}), false);
  await core.handleMessage({ type: 'pickSession' });
  await until(() => core.state.session?.id === 'new' || core.state.items.length >= 0);
  assert.ok(calls.some((c) => c[0] === 'pickSession'));
  await core.dispose();
});

test('Kern: Backend fehlt -> Fehlermeldung statt Endlosschleife', async () => {
  const dir = mkdtempSync(path.join(tmpdir(), 'lmcore-'));
  const h = makeHost();
  const core = new ChatCore({
    workdir: dir,
    tree: '/nirgendwo',
    follow: true,
    ui: h.ui,
    log: () => {},
    makeBackend: () => new Backend({ command: path.join(dir, 'gibtsnicht', 'lm.exe'), args: [], cwd: dir }),
  });
  core.start();
  await until(() => core.state.conn.status === 'down');
  assert.match(core.state.conn.detail, /lightmind\.home/);
  await core.dispose();
});

test('Kern: kein hello innerhalb der Frist -> down mit Hinweis, spaeter Retry startet neu', async () => {
  const dir = mkdtempSync(path.join(tmpdir(), 'lmcore-'));
  const h = makeHost();
  let silent = true;
  const core = new ChatCore({
    workdir: dir,
    tree: here,
    follow: true,
    ui: h.ui,
    log: () => {},
    helloTimeoutMs: 300,
    shutdownGraceMs: 200,
    restartGraceMs: 200,
    makeBackend: () =>
      new Backend(
        { command: process.execPath, args: [fake, 'serve', '--stdio', '--workdir', dir], cwd: here },
        { ...process.env, ...(silent ? { FAKE_LM_NO_HELLO: '1' } : {}) },
      ),
  });
  core.start();
  assert.equal(core.state.conn.status, 'starting');
  await new Promise((r) => setTimeout(r, 150));
  assert.equal(core.state.conn.status, 'starting', 'vor Ablauf der Frist noch kein Fehler');
  await until(() => core.state.conn.status === 'down');
  assert.match(core.state.conn.detail, /did not answer within \d+ s.*Retry/);
  await new Promise((r) => setTimeout(r, 400));
  assert.equal(core.state.conn.status, 'down', 'kein Neustart-Zyklus nach dem Ablauf');
  silent = false;
  await core.handleMessage({ type: 'restart' });
  await until(() => core.state.conn.status === 'ready');
  await core.dispose();
});

test('Kern: hello rechtzeitig -> Frist loest nicht aus', async () => {
  const dir = mkdtempSync(path.join(tmpdir(), 'lmcore-'));
  const { core } = mk(dir, path.join(dir, 'req.log'));
  (core as unknown as { o: { helloTimeoutMs: number } }).o.helloTimeoutMs = 400;
  core.start();
  await until(() => core.state.conn.status === 'ready');
  await new Promise((r) => setTimeout(r, 600));
  assert.equal(core.state.conn.status, 'ready');
  await core.dispose();
});
