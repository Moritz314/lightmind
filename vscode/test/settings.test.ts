// Einstellungen: Gruppenlogik, Schema der Nachrichten, Kern mit echtem Backend-Prozess (fake-lm.mjs).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import * as path from 'node:path';
import { fileURLToPath } from 'node:url';
import { Backend } from '../src/ext/backend.ts';
import { ChatCore } from '../src/ext/core.ts';
import { parseWebviewMessage } from '../src/shared/messages.ts';
import { normalizeFields } from '../src/shared/protocol.ts';
import { choiceIndex, fieldTitle, groupFields, groupId, showValue } from '../src/shared/settings-view.ts';
import { makeHost } from './helpers.ts';

const here = path.dirname(fileURLToPath(import.meta.url));
const fake = path.join(here, 'fake-lm.mjs');

test('Einstellungen: Felder werden normalisiert, Werte und Auswahllisten behalten ihren Typ', () => {
  const f = normalizeFields({
    fields: [
      { key: 'sessions.max_parallel', label: 'Max parallel', help: 'h', value: 2, choices: [1, 2, 4], scope: 'config' },
      { key: 'ui.boot_wait', value: true, choices: [true, false, { x: 1 }, null] },
      { key: '', value: 1 },
      { nokey: 1 },
    ],
  });
  assert.equal(f.length, 2);
  assert.deepEqual(f[0].choices, [1, 2, 4]);
  assert.deepEqual(f[1].choices, [true, false]);
  assert.equal(f[1].label, '');
  assert.deepEqual(normalizeFields(null), []);
});

test('Einstellungen: Gruppen nach Schluesselpraefix in Reihenfolge, Anzeigewerte', () => {
  const f = normalizeFields({
    fields: [
      { key: 'defaults.model', value: 'sonnet', choices: ['sonnet'] },
      { key: 'vscode.agent', value: null, choices: ['a'] },
      { key: 'defaults.effort', value: 'low', choices: ['low', 'high'] },
      { key: 'plain', value: 3 },
    ],
  });
  const g = groupFields(f);
  assert.deepEqual(g.map((x) => [x.id, x.fields.length]), [['defaults', 2], ['vscode', 1], ['general', 1]]);
  assert.equal(g[1].title, 'VS Code');
  assert.equal(groupId('a.b.c'), 'a');
  assert.equal(fieldTitle(f[2]), 'Effort');
  assert.equal(fieldTitle({ ...f[0], label: 'Model' }), 'Model');
  assert.equal(showValue(null), 'not set');
  assert.equal(showValue(true), 'yes');
  assert.equal(showValue(4), '4');
  assert.equal(choiceIndex(f[2]), 0);
});

test('Einstellungen: die workflow-Schluessel sind die Gruppe Ultracode, das Effort-Feld endet mit max und ultracode', () => {
  const f = normalizeFields({
    fields: [
      { key: 'defaults.effort', value: 'max', choices: ['low', 'medium', 'high', 'xhigh', 'max', 'ultracode'] },
      { key: 'workflow.max_agents', value: 1000, choices: [10, 1000] },
      { key: 'workflow.notify', value: 'off', choices: ['both', 'off'] },
    ],
  });
  const g = groupFields(f);
  assert.deepEqual(g.map((x) => [x.id, x.title, x.fields.length]), [['defaults', 'Defaults', 1], ['workflow', 'Ultracode', 2]]);
  assert.deepEqual(f[0].choices?.slice(-2), ['max', 'ultracode']);
});

test('Einstellungen: Schema der neuen Nachrichten', () => {
  assert.deepEqual(parseWebviewMessage({ type: 'settingsGet' }), { type: 'settingsGet' });
  assert.deepEqual(parseWebviewMessage({ type: 'settingsPick', key: 'a.b', index: 2, x: 1 }), { type: 'settingsPick', key: 'a.b', index: 2 });
  assert.equal(parseWebviewMessage({ type: 'settingsPick', key: 'a.b', index: -1 }), null);
  assert.equal(parseWebviewMessage({ type: 'settingsPick', key: '', index: 0 }), null);
  assert.equal(parseWebviewMessage({ type: 'settingsPick', key: 'a', index: 200 }), null);
  assert.deepEqual(parseWebviewMessage({ type: 'settingsText', key: 'a', text: '' }), { type: 'settingsText', key: 'a', text: '' });
  assert.equal(parseWebviewMessage({ type: 'settingsText', key: 'a', text: 'x'.repeat(2001) }), null);
  assert.equal(parseWebviewMessage({ type: 'settingsText', key: 'a' }), null);
});

test('Kern: settings.get, Auswahl, Text, Fehlerfall und Wahl ausserhalb der Liste', async () => {
  const dir = mkdtempSync(path.join(tmpdir(), 'lmset-'));
  const logFile = path.join(dir, 'req.log');
  const h = makeHost();
  const core = new ChatCore({
    workdir: dir,
    tree: here,
    follow: true,
    ui: h.ui,
    log: () => {},
    restartDelaysMs: [50],
    shutdownGraceMs: 3000,
    makeBackend: () => new Backend({ command: process.execPath, args: [fake, 'serve', '--stdio', '--workdir', dir], cwd: here }, { ...process.env, FAKE_LM_LOG: logFile }),
  });
  const posted: Array<Record<string, unknown>> = [];
  core.attach({ post: (m) => void posted.push(m as Record<string, unknown>) });
  core.start();
  const until = async (fn: () => boolean): Promise<void> => {
    const t = Date.now();
    while (!fn()) {
      if (Date.now() - t > 8000) throw new Error('timeout');
      await new Promise((r) => setTimeout(r, 20));
    }
  };
  await until(() => core.state.conn.status === 'ready');
  const reqs = (): Array<Record<string, unknown>> => readFileSync(logFile, 'utf8').split('\n').filter(Boolean).map((l) => JSON.parse(l));
  const last = (type: string): Record<string, unknown> | undefined => [...posted].reverse().find((m) => m.type === type);

  await core.handleMessage({ type: 'settingsGet' });
  const fields = (last('settings')?.fields ?? []) as Array<{ key: string; value: unknown }>;
  assert.ok(fields.length >= 5);
  assert.equal(fields.find((f) => f.key === 'defaults.effort')?.value, 'medium');

  // Auswahl: Index 2 von effort = 'high'
  await core.handleMessage({ type: 'settingsPick', key: 'defaults.effort', index: 2 });
  assert.deepEqual(reqs().filter((r) => r.type === 'settings.set').pop()?.changes, { 'defaults.effort': 'high' });
  assert.deepEqual(last('settingsResult'), { type: 'settingsResult', key: 'defaults.effort', ok: true });
  assert.equal(((last('settings')?.fields ?? []) as Array<{ key: string; value: unknown }>).find((f) => f.key === 'defaults.effort')?.value, 'high');

  // Zahl und Wahrheitswert behalten ihren Typ
  await core.handleMessage({ type: 'settingsPick', key: 'sessions.max_parallel', index: 2 });
  assert.deepEqual(reqs().filter((r) => r.type === 'settings.set').pop()?.changes, { 'sessions.max_parallel': 4 });
  await core.handleMessage({ type: 'settingsPick', key: 'ui.boot_wait', index: 1 });
  assert.deepEqual(reqs().filter((r) => r.type === 'settings.set').pop()?.changes, { 'ui.boot_wait': false });

  // Textfeld
  await core.handleMessage({ type: 'settingsText', key: 'vscode.note', text: '  new note ' });
  assert.deepEqual(reqs().filter((r) => r.type === 'settings.set').pop()?.changes, { 'vscode.note': 'new note' });

  // Fehler aus dem Backend (ok:false) kommt als Meldung zurueck
  const before = reqs().filter((r) => r.type === 'settings.set').length;
  await core.handleMessage({ type: 'settingsText', key: 'defaults.effort', text: 'bogus' });
  const bad = last('settingsResult');
  assert.equal(bad?.ok, false);
  assert.match(String(bad?.error), /not one of the allowed/);
  assert.equal(reqs().filter((r) => r.type === 'settings.set').length, before + 1);

  // Index ausserhalb der Liste, unbekannter Schluessel: nichts geht ans Backend
  const n = reqs().filter((r) => r.type === 'settings.set').length;
  await core.handleMessage({ type: 'settingsPick', key: 'defaults.effort', index: 99 });
  assert.equal(last('settingsResult')?.ok, false);
  await core.handleMessage({ type: 'settingsText', key: 'nope', text: 'x' });
  assert.equal(last('settingsResult')?.ok, false);
  assert.equal(reqs().filter((r) => r.type === 'settings.set').length, n);

  // Befehl "Settings" oeffnet die Ansicht in der Webview
  core.showSettings();
  assert.deepEqual(last('view'), { type: 'view', name: 'settings' });
  // "Open config.md" oeffnet die Datei wie bisher
  await core.handleMessage({ type: 'settings' });
  assert.ok(h.calls.some((c) => c[0] === 'openSettings'));
  await core.dispose();
});

test('Unteragenten: Zeile je Agent, Verlauf getrennt vom Chat, fertige gehen mit dem naechsten Zug', async () => {
  const { initialState, reduce } = await import('../src/shared/reducer.ts');
  const { normalizeEvent } = await import('../src/shared/protocol.ts');
  const ev = (raw: Record<string, unknown>) => normalizeEvent({ session: 's1', turn: 'h1', ...raw })!;
  let s = initialState();
  s = reduce(s, { kind: 'event', event: ev({ type: 'subagent.start', id: 'a1', name: 'coder', task: 'fix it' }) });
  s = reduce(s, { kind: 'event', event: ev({ type: 'tool.start', call: 'c1', name: 'read', summary: 'app.py', files: [], parent: 'a1' }) });
  s = reduce(s, { kind: 'event', event: ev({ type: 'text', text: 'looking at it', delta: false, parent: 'a1' }) });
  assert.equal(s.items.length, 0, 'Unteragenten-Schritte gehoeren nicht in den Chat');
  assert.deepEqual(s.subs.map((b) => [b.id, b.name, b.done, b.log.length]), [['a1', 'coder', false, 2]]);
  s = reduce(s, { kind: 'event', event: ev({ type: 'subagent.end', id: 'a1', ok: true, summary: 'done it' }) });
  assert.equal(s.subs[0].done, true);
  assert.equal(s.subs[0].summary, 'done it');
  assert.equal(normalizeEvent({ type: 'subagent.start', session: 's' }), null);
  s = reduce(s, { kind: 'event', event: ev({ type: 'turn.start' }) });
  assert.equal(s.subs.length, 0);
});

test('Workflows: Plan als Hinweis, Phase und Agenten in einer Fortschrittszeile, Ende als Hinweis', async () => {
  const { initialState, reduce } = await import('../src/shared/reducer.ts');
  const { normalizeEvent } = await import('../src/shared/protocol.ts');
  const ev = (raw: Record<string, unknown>) => normalizeEvent({ session: 's1', turn: 't1', ...raw })!;
  let s = initialState();
  s = reduce(s, { kind: 'event', event: ev({ type: 'workflow.start', id: 'wf-1', name: 'audit', description: 'check all', phases: ['Find', 'Check'] }) });
  assert.deepEqual(s.flows.map((w) => [w.id, w.name, w.phases]), [['wf-1', 'audit', ['Find', 'Check']]]);
  assert.match((s.items[0] as { text: string }).text, /Workflow audit: check all · phases: Find, Check/);
  s = reduce(s, { kind: 'event', event: ev({ type: 'workflow.phase', id: 'wf-1', title: 'Find' }) });
  for (let i = 0; i < 30; i++) s = reduce(s, { kind: 'event', event: ev({ type: 'subagent.start', id: `wf-1.a${i}`, name: `f${i}`, task: 't', workflow: 'wf-1', phase: 'Find' }) });
  s = reduce(s, { kind: 'event', event: ev({ type: 'subagent.start', id: 'c1', name: 'helper', task: 't' }) });
  assert.equal(s.flows[0].phase, 'Find');
  assert.equal(s.subs.filter((b) => b.workflow === 'wf-1').length, 30, 'Agenten eines Workflows fallen nicht nach 20 heraus');
  assert.equal(s.subs.find((b) => b.id === 'c1')?.workflow, undefined);
  s = reduce(s, { kind: 'event', event: ev({ type: 'workflow.end', id: 'wf-1', ok: false, summary: 'failed; agents: 30 live' }) });
  assert.equal(s.flows[0].done, true);
  assert.match((s.items[s.items.length - 1] as { text: string }).text, /Workflow audit failed/);
  assert.equal(normalizeEvent({ type: 'workflow.phase', session: 's' }), null);
});

test('Kern: kann das Backend keine Modi nennen, traegt die Auswahl den Grund', async () => {
  const dir = mkdtempSync(path.join(tmpdir(), 'lmmode-'));
  const h = makeHost();
  const core = new ChatCore({
    workdir: dir, tree: here, follow: true, ui: h.ui, log: () => {}, restartDelaysMs: [50], shutdownGraceMs: 3000,
    makeBackend: () => new Backend({ command: process.execPath, args: [fake, 'serve', '--stdio', '--workdir', dir], cwd: here }, { ...process.env, FAKE_LM_MODES_ERROR: 'permissions.md has no `modes:` mapping' }),
  });
  const posted: Array<Record<string, unknown>> = [];
  core.attach({ post: (m) => void posted.push(m as Record<string, unknown>) });
  core.start();
  const t0 = Date.now();
  while (core.state.conn.status !== 'ready') {
    if (Date.now() - t0 > 8000) throw new Error('timeout');
    await new Promise((r) => setTimeout(r, 20));
  }
  await core.handleMessage({ type: 'choicesGet' });
  const c = [...posted].reverse().find((m) => m.type === 'choices');
  assert.equal(c?.modeError, 'permissions.md has no `modes:` mapping');
  await core.dispose();
});
