// Chips, Sitzungsliste, Fusszeile der Einstellungen: reine Logik, Nachrichtenschema und Kern mit gespieltem Backend (fake-lm.mjs).
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import * as path from 'node:path';
import { fileURLToPath } from 'node:url';
import { Backend } from '../src/ext/backend.ts';
import { ChatCore } from '../src/ext/core.ts';
import { parseWebviewMessage } from '../src/shared/messages.ts';
import { normalizeAgents, normalizeFields, normalizeSession } from '../src/shared/protocol.ts';
import { ago, chipValue, choicesFromFields, emptyChoices, filterSessions, groupSessions, isCurrent, nextMode, parseWhen, sessionMeta, summaryParts, toolDoing, toolSummary } from '../src/shared/views.ts';
import { makeHost } from './helpers.ts';

const here = path.dirname(fileURLToPath(import.meta.url));
const fake = path.join(here, 'fake-lm.mjs');

const sess = (o: Record<string, unknown>) => normalizeSession({ id: 'x', ...o })!;

test('Sitzungen: readonly und Herkunft werden normalisiert', () => {
  assert.equal(sess({}).readonly, false);
  assert.equal(sess({ readonly: true }).readonly, true);
  assert.equal(sess({ readonly: 'yes' }).readonly, false);
  assert.equal(sess({ origin: 'vscode' }).origin, 'vscode');
});

test('Sitzungen: archived wird nur bei true uebernommen', () => {
  assert.equal(sess({}).archived, false);
  assert.equal(sess({ archived: true }).archived, true);
  assert.equal(sess({ archived: 'yes' }).archived, false);
});

test('Nachrichten: archiveGet und sessionRestore', () => {
  assert.deepEqual(parseWebviewMessage({ type: 'archiveGet' }), { type: 'archiveGet' });
  assert.deepEqual(parseWebviewMessage({ type: 'sessionRestore', id: 'arc1' }), { type: 'sessionRestore', id: 'arc1' });
  assert.equal(parseWebviewMessage({ type: 'sessionRestore' }), null);
  assert.equal(parseWebviewMessage({ type: 'sessionRestore', id: '' }), null);
});

test('Agenten: eigene und geltende Werte, kaputte Eintraege fallen weg', () => {
  const a = normalizeAgents({
    agents: [
      { name: 'coder', project: 'demo', provider: null, model: 'opus', effort: '', mode: null, effective: { effort: 'medium', mode: 'auto' } },
      { name: '' },
      'x',
      { name: 'w', error: 'broken' },
    ],
  });
  assert.equal(a.length, 2);
  assert.deepEqual(a[0].own, { project: 'demo', provider: null, model: 'opus', effort: null, mode: null });
  assert.equal(a[0].eff.effort, 'medium');
  assert.equal(a[1].error, 'broken');
  assert.deepEqual(normalizeAgents(null), []);
});

test('Nachrichten: neue Typen werden geprueft', () => {
  assert.deepEqual(parseWebviewMessage({ type: 'sessionsGet' }), { type: 'sessionsGet' });
  assert.deepEqual(parseWebviewMessage({ type: 'notesGet', id: 'a' }), { type: 'notesGet', id: 'a' });
  assert.deepEqual(parseWebviewMessage({ type: 'notesName', id: 'a', field: 'title', text: '' }), { type: 'notesName', id: 'a', field: 'title', text: '' });
  assert.equal(parseWebviewMessage({ type: 'notesName', id: 'a', field: 'title', text: 'x'.repeat(401) }), null);
  assert.deepEqual(parseWebviewMessage({ type: 'sessionEnd', id: 'a' }), { type: 'sessionEnd', id: 'a' });
  assert.equal(parseWebviewMessage({ type: 'sessionEnd' }), null);
  assert.deepEqual(parseWebviewMessage({ type: 'choicesGet' }), { type: 'choicesGet' });
  assert.deepEqual(parseWebviewMessage({ type: 'openSession', id: 'abc' }), { type: 'openSession', id: 'abc' });
  assert.equal(parseWebviewMessage({ type: 'openSession', id: '' }), null);
  assert.deepEqual(parseWebviewMessage({ type: 'sessionSet', field: 'effort', value: 'high', extra: 1 }), { type: 'sessionSet', field: 'effort', value: 'high' });
  assert.equal(parseWebviewMessage({ type: 'sessionSet', field: 'effort' }), null);
  assert.equal(parseWebviewMessage({ type: 'sessionSet', field: 'effort', value: 'x'.repeat(81) }), null);
});

test('Chips: Listen und Vorgaben aus den Einstellungen, Wahl ohne Sitzung gewinnt', () => {
  const f = normalizeFields({
    fields: [
      { key: 'defaults.model', value: 'sonnet', choices: ['sonnet', 'haiku'] },
      { key: 'vscode.model', value: null, choices: ['sonnet', 'haiku', 'opus'] },
      { key: 'defaults.effort', value: 'medium', choices: ['low', 'medium', 'high'] },
      { key: 'defaults.mode', value: 'auto', choices: ['auto', 'plan', 'bypass', 7] },
    ],
  });
  const c = choicesFromFields(f, {});
  assert.deepEqual(c.model, ['sonnet', 'haiku', 'opus']); // erste Liste gilt (vscode.model), Vorgabe vom ersten Wert
  assert.equal(c.current.model, 'sonnet');
  assert.deepEqual(c.mode, ['auto', 'plan', 'bypass']);
  assert.equal(choicesFromFields(f, { effort: 'high' }).current.effort, 'high');
  assert.deepEqual(choicesFromFields([], {}), emptyChoices());
});

test('Chips: aufgeloester Modellname passt zur Auswahl, Anzeige nimmt Status vor Vorgabe', () => {
  assert.equal(isCurrent('model', 'sonnet', 'claude-sonnet-4-5'), true);
  assert.equal(isCurrent('effort', 'high', 'high'), true);
  assert.equal(isCurrent('effort', 'high', 'highest'), false);
  assert.equal(isCurrent('mode', 'auto', null), false);
  const ch = { ...emptyChoices(), current: { model: 'haiku', effort: 'low', mode: 'plan' } };
  assert.equal(chipValue('model', { model: 'opus', effort: null, mode: null }, 'x', ch), 'opus');
  assert.equal(chipValue('model', { model: null, effort: null, mode: null }, 'x', ch), 'x');
  assert.equal(chipValue('effort', { model: null, effort: null, mode: null }, null, ch), 'low');
});

test('Sitzungsliste: Zeitangabe, Meta-Zeile, Suche', () => {
  const now = Date.parse('2026-10-21T20:00:00');
  assert.equal(ago('2026-10-21 19:59:30', now), 'just now');
  assert.equal(ago('2026-10-21 19:15', now), '45 min ago');
  assert.equal(ago('2026-10-21 17:00', now), '3 h ago');
  assert.equal(ago('2026-10-19 20:00', now), '2 d ago');
  assert.equal(ago('2026-09-01 10:00', now), '2026-09-01');
  assert.equal(ago(null, now), '');
  assert.equal(ago('gestern', now), 'gestern');
  assert.equal(parseWhen('nonsense'), null);
  const a = sess({ id: 'a', name: 'Fix login bug', agent: 'coder', project: 'demo', status: 'paused', last_active: '2026-10-21 19:15', origin: 'tui' });
  const b = sess({ id: 'b', name: 'Release notes', agent: 'writer', status: 'running', origin: '', readonly: true });
  assert.equal(sessionMeta(a, now), 'coder · project demo · from tui · 45 min ago');
  assert.match(sessionMeta(b, now), /from tui/); // leere Herkunft heisst Terminal
  assert.deepEqual(filterSessions([a, b], '').map((s) => s.id), ['a', 'b']);
  assert.deepEqual(filterSessions([a, b], 'login').map((s) => s.id), ['a']);
  assert.deepEqual(filterSessions([a, b], 'writer running').map((s) => s.id), ['b']);
  assert.deepEqual(filterSessions([a, b], 'elsewhere').map((s) => s.id), ['b']);
  assert.deepEqual(filterSessions([a, b], 'demo coder').map((s) => s.id), ['a']);
  assert.deepEqual(filterSessions([a, b], 'zzz'), []);
});

test('Fusszeile der Einstellungen: Zusammenfassung der Vorgaben eines neuen Chats', () => {
  const f = normalizeFields({
    fields: [
      { key: 'defaults.provider', value: 'claude-abo' },
      { key: 'defaults.model', value: 'sonnet' },
      { key: 'defaults.effort', value: null },
      { key: 'defaults.context', value: '200k' },
      { key: 'defaults.mode', value: 'auto' },
    ],
  });
  assert.deepEqual(summaryParts(f), [
    { text: 'claude-abo', mode: false },
    { text: 'sonnet', mode: false },
    { text: '200k', mode: false },
    { text: 'auto', mode: true },
  ]);
});

// ---- Kern ----
const until = async (fn: () => boolean, ms = 8000): Promise<void> => {
  const t = Date.now();
  while (!fn()) {
    if (Date.now() - t > ms) throw new Error('timeout: ' + fn.toString());
    await new Promise((r) => setTimeout(r, 20));
  }
};
const lines = (f: string): Array<Record<string, unknown>> =>
  readFileSync(f, 'utf8').split('\n').filter(Boolean).map((l) => JSON.parse(l));

function mk() {
  const dir = mkdtempSync(path.join(tmpdir(), 'lmviews-'));
  const logFile = path.join(dir, 'req.log');
  const h = makeHost();
  const posts: Array<Record<string, unknown>> = [];
  const core = new ChatCore({
    workdir: dir,
    tree: here,
    follow: true,
    ui: h.ui,
    log: () => {},
    restartDelaysMs: [50, 100, 200],
    shutdownGraceMs: 3000,
    makeBackend: () => new Backend({ command: process.execPath, args: [fake, 'serve', '--stdio', '--workdir', dir], cwd: here }, { ...process.env, FAKE_LM_LOG: logFile }),
  });
  core.attach({ post: (m) => posts.push(m as Record<string, unknown>) });
  const reqs = () => lines(logFile);
  const last = (type: string) => posts.filter((p) => p.type === type).pop();
  return { core, posts, reqs, last };
}

test('Kern: Chips ohne Sitzung merken die Wahl und geben sie der naechsten neuen Sitzung mit', async () => {
  const { core, reqs, last } = mk();
  core.start();
  await until(() => core.state.conn.status === 'ready');
  await core.handleMessage({ type: 'choicesGet' });
  const c0 = last('choices') as { model: string[]; effort: string[]; mode: string[]; current: Record<string, string> };
  assert.deepEqual(c0.effort, ['low', 'medium', 'high']);
  assert.deepEqual(c0.mode, ['auto', 'plan', 'bypass']);
  assert.equal(c0.current.model, 'sonnet');
  await core.handleMessage({ type: 'sessionSet', field: 'model', value: 'haiku' });
  await core.handleMessage({ type: 'sessionSet', field: 'mode', value: 'plan' });
  assert.equal((last('choices') as { current: Record<string, string> }).current.model, 'haiku');
  assert.equal(reqs().some((m) => m.type === 'session.set'), false, 'ohne Sitzung keine Anfrage');
  await core.handleMessage({ type: 'send', text: 'hello' });
  await until(() => !core.state.busy && core.state.items.some((i) => i.k === 'turnend'));
  const sn = reqs().find((m) => m.type === 'session.new');
  assert.equal(sn?.model, 'haiku');
  assert.equal(sn?.mode, 'plan');
  assert.equal('effort' in (sn ?? {}), false);
  assert.equal(core.state.status.mode, 'plan');
  // die Wahl ist verbraucht: die naechste neue Sitzung nimmt wieder die Vorgaben
  await core.newSession({});
  const sns = reqs().filter((m) => m.type === 'session.new');
  assert.equal('model' in sns[1], false);
  await core.dispose();
});

test('Kern: Chips mit offener Sitzung senden session.set, der Status folgt', async () => {
  const { core, reqs } = mk();
  core.start();
  await until(() => core.state.conn.status === 'ready');
  await core.newSession({});
  await core.handleMessage({ type: 'sessionSet', field: 'effort', value: 'low' });
  await until(() => core.state.status.effort === 'low');
  const set = reqs().find((m) => m.type === 'session.set');
  assert.equal(set?.effort, 'low');
  assert.equal('model' in (set ?? {}), false);
  await core.handleMessage({ type: 'sessionSet', field: 'nonsense', value: 'x' });
  assert.equal(reqs().filter((m) => m.type === 'session.set').length, 1, 'unbekanntes Feld wird verworfen');
  await core.dispose();
});

test('Kern: das Archiv wird gelistet, Wiederherstellen bringt den Chat in die Liste, Oeffnen stellt ihn her', async () => {
  const { core, reqs, last } = mk();
  core.start();
  await until(() => core.state.conn.status === 'ready');
  await core.handleMessage({ type: 'archiveGet' });
  const shelf = last('sessions') as { sessions: Array<{ id: string; archived: boolean }>; archived: boolean };
  assert.equal(shelf.archived, true);
  assert.deepEqual(shelf.sessions.map((s) => s.id), ['arc1', 'arc2']);
  assert.ok(shelf.sessions.every((s) => s.archived));
  assert.equal(reqs().some((m) => m.type === 'session.list' && m.archived === true), true);
  await core.handleMessage({ type: 'sessionsGet' });
  const recent = last('sessions') as { sessions: Array<{ id: string }>; archived: boolean };
  assert.equal(recent.archived, false);
  assert.deepEqual(recent.sessions.map((s) => s.id), ['old1', 'old2', 'old3'], 'the archive is not in the normal list');
  await core.handleMessage({ type: 'sessionRestore', id: 'arc1' });
  assert.deepEqual(last('sessionMoved'), { type: 'sessionMoved', id: 'arc1', ok: true, text: 'restored' });
  assert.deepEqual((last('sessions') as { sessions: Array<{ id: string }> }).sessions.map((s) => s.id), ['arc2'], 'the archive list follows');
  await core.handleMessage({ type: 'sessionsGet' });
  assert.ok((last('sessions') as { sessions: Array<{ id: string }> }).sessions.some((s) => s.id === 'arc1'), 'arc1 is in the normal list now');
  await core.handleMessage({ type: 'sessionRestore', id: 'nope' });
  const failed = last('sessionMoved') as { ok: boolean; text: string };
  assert.equal(failed.ok, false);
  assert.match(failed.text, /not in the archive/);
  await core.handleMessage({ type: 'openSession', id: 'arc2' });
  assert.equal(core.state.session?.id, 'arc2');
  await core.handleMessage({ type: 'sessionsGet' });
  assert.ok((last('sessions') as { sessions: Array<{ id: string }> }).sessions.some((s) => s.id === 'arc2'), 'opening restored it');
  await core.handleMessage({ type: 'archiveGet' });
  assert.deepEqual((last('sessions') as { sessions: unknown[] }).sessions, []);
  await core.dispose();
});

test('Kern: Sitzungsliste, Oeffnen mit Verlauf, nur lesbare Sitzung nimmt keine Wahl an', async () => {
  const { core, posts, reqs, last } = mk();
  core.start();
  await until(() => core.state.conn.status === 'ready');
  await core.handleMessage({ type: 'sessionsGet' });
  const list = (last('sessions') as { sessions: Array<{ id: string; readonly: boolean; origin: string | null }> }).sessions;
  assert.deepEqual(list.map((s) => s.id), ['old1', 'old2', 'old3']);
  assert.equal(list.find((s) => s.id === 'old3')?.readonly, true);
  assert.equal(list.find((s) => s.id === 'old1')?.readonly, false);
  await core.handleMessage({ type: 'openSession', id: 'old1' });
  assert.equal(core.state.session?.id, 'old1');
  assert.ok(core.state.items.some((i) => i.k === 'user' && i.text === 'earlier question'));
  assert.deepEqual(last('view'), { type: 'view', name: 'chat' });
  await core.handleMessage({ type: 'openSession', id: 'old3' });
  assert.equal(core.state.session?.readonly, true);
  await core.handleMessage({ type: 'sessionSet', field: 'mode', value: 'bypass' });
  assert.equal(reqs().some((m) => m.type === 'session.set'), false);
  assert.ok(core.state.items.some((i) => i.k === 'notice' && /elsewhere/.test(i.text)));
  assert.equal(posts.filter((p) => p.type === 'sessionsError').length, 0);
  core.showSessions();
  assert.deepEqual(last('view'), { type: 'view', name: 'sessions' });
  await core.dispose();
});

test('Kern: Notizen eines Chats lesen, Name und Beschreibung setzen, Chat beenden', async () => {
  const { core, posts, reqs, last } = mk();
  core.start();
  await until(() => core.state.conn.status === 'ready');
  await core.handleMessage({ type: 'notesGet', id: 'old1' });
  const n = (last('notes') as { notes: { session: string; title: string; description: string; items: Array<Record<string, unknown>> } }).notes;
  assert.equal(n.session, 'old1');
  assert.equal(n.title, 'Old name');
  assert.deepEqual(n.items.map((i) => i.type), ['folder', 'note']);
  assert.equal(((n.items[0].items as Array<Record<string, unknown>>)[0]).state, 'doing');
  await core.handleMessage({ type: 'notesGet', id: 'gone' });
  assert.match((last('notesError') as { error: string }).error, /no such chat/);
  await core.handleMessage({ type: 'notesName', id: 'old1', field: 'title', text: 'New name' });
  const saved = last('notesSaved') as { ok: boolean; title: string };
  assert.equal(saved.ok, true);
  assert.equal(saved.title, 'New name');
  assert.equal(reqs().find((m) => m.type === 'notes.name')?.field, 'title');
  await core.handleMessage({ type: 'notesName', id: 'gone', field: 'title', text: 'x' });
  assert.equal((last('notesSaved') as { ok: boolean }).ok, false);
  // Beenden: ein Chat, der nicht hier offen ist, wird beendet und die Liste kommt neu mit Status "done"
  await core.handleMessage({ type: 'sessionEnd', id: 'old1' });
  assert.equal((last('sessionEnded') as { ok: boolean; text: string }).text, 'ended');
  const list = (last('sessions') as { sessions: Array<{ id: string; status: string }> }).sessions;
  assert.equal(list.find((s) => s.id === 'old1')?.status, 'done');
  // der hier offene Chat wird abgelehnt
  await core.handleMessage({ type: 'openSession', id: 'old2' });
  await core.handleMessage({ type: 'sessionEnd', id: 'old2' });
  const refused = last('sessionEnded') as { ok: boolean; text: string };
  assert.equal(refused.ok, false);
  assert.match(refused.text, /open here/);
  assert.equal(posts.filter((p) => p.type === 'sessionsError').length, 0);
  await core.dispose();
});

test('Kern: ein anderes Fenster beendet den offenen Chat: die Sitzung ist zu, die naechste Nachricht startet eine neue', async () => {
  const { core } = mk();
  core.start();
  await until(() => core.state.conn.status === 'ready');
  await core.newSession({});
  const id = core.state.session?.id ?? '';
  assert.ok(id);
  const { reduce } = await import('../src/shared/reducer.ts');
  const s2 = reduce(core.state, { kind: 'event', event: { type: 'session.ended', session: id, text: 'This chat was ended from the resume menu.' } });
  assert.equal(s2.session, null);
  assert.equal(s2.busy, false);
  assert.ok(s2.items.some((i) => i.k === 'notice' && /resume menu/.test(i.text)));
  await core.dispose();
});

test('Kern: settings.get liefert die Agenten an die Webview', async () => {
  const { core, last } = mk();
  core.start();
  await until(() => core.state.conn.status === 'ready');
  await core.handleMessage({ type: 'settingsGet' });
  const m = last('settings') as { fields: unknown[]; agents: Array<{ name: string; model: string | null; effective: Record<string, string | null> }> };
  assert.deepEqual(m.agents.map((a) => a.name), ['coder', 'writer']);
  assert.equal(m.agents[1].model, 'sonnet');
  assert.equal(m.agents[0].effective.mode, 'auto');
  assert.ok((last('choices') as { model: string[] }).model.length > 0);
  await core.dispose();
});

test('groupSessions folds runs under the session that started them', () => {
  const mk = (id: string, parent: string | null, last: string, parentName: string | null = null) =>
    normalizeSession({ id, name: id, parent, parent_name: parentName, last_active: last })!;
  const list = [mk('a', null, '2026-10-23 10:00'), mk('b', 'a', '2026-10-23 09:00'), mk('c', 'b', '2026-10-23 08:00'),
    mk('x', 'gone', '2026-10-23 11:00', 'Bot'), mk('y', 'gone', '2026-10-22 11:00', 'Bot')];
  const ids = (open: string[]) => groupSessions(list, new Set(open)).map((l) => `${l.id}:${l.depth}:${l.kids}`);
  assert.deepEqual(ids([]), ['gone:0:2', 'a:0:2']);
  assert.deepEqual(ids(['a', 'b']), ['gone:0:2', 'a:0:2', 'b:1:1', 'c:2:0']);
  const head = groupSessions(list, new Set(['gone']));
  assert.equal(head[0].session, null);
  assert.equal(head[0].label, 'Bot');
  assert.deepEqual(head.map((l) => l.id), ['gone', 'x', 'y', 'a']);
});

test('groupSessions keeps rows of a parent loop', () => {
  const mk = (id: string, parent: string | null) => normalizeSession({ id, name: id, parent })!;
  const ids = groupSessions([mk('p', 'p'), mk('q', 'r'), mk('r', 'q')], new Set()).map((l) => l.id).sort();
  assert.deepEqual(ids, ['p', 'q']);
});

test('askPreview: the raw command is shown whole without control characters, only a huge one is shortened', async () => {
  const { askPreview, ASK_DETAIL_MAX } = await import('../src/shared/views.ts');
  const p = askPreview(Array.from({ length: 40 }, (_, i) => `echo ${i}\u001b[2J`).join('\n') + 'x'.repeat(500) + '\u202e; rm -rf ~');
  assert.equal(p.text.split('\n').length, 40);
  assert.ok(!p.cut && !p.text.includes('\u001b') && !p.text.includes('\u202e') && p.text.endsWith('; rm -rf ~'));
  const one = askPreview('ls -la');
  assert.deepEqual(one, { text: 'ls -la', cut: false, chars: 6 });
  const huge = askPreview('y'.repeat(ASK_DETAIL_MAX + 10) + 'TAIL');
  assert.ok(huge.cut && huge.text.endsWith('TAIL') && huge.text.length < ASK_DETAIL_MAX + 10);
});

test('Werkzeugbloecke: Zusammenfassung in Klartext, naechster Modus', () => {
  assert.equal(toolSummary(['Read', 'Read', 'Bash', 'Read', 'Edit']), '5 tool calls: 3 files read, 1 command, 1 edit');
  assert.equal(toolSummary(['Grep']), '1 tool call: 1 search');
  assert.equal(toolSummary(['mcp__x__y', 'Task']), '2 tool calls: 1 other call, 1 sub-agent');
  assert.equal(toolDoing('Bash'), 'running a command');
  assert.deepEqual(['auto', 'plan', 'bypass', 'auto'].map((m, i, a) => nextMode(['auto', 'plan', 'bypass'], m)), ['plan', 'bypass', 'auto', 'plan']);
  assert.equal(nextMode(['auto', 'plan'], 'weird'), 'auto');
  assert.equal(nextMode([], 'auto'), null);
});
