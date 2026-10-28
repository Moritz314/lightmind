import { test } from 'node:test';
import assert from 'node:assert/strict';
import * as path from 'node:path';
import { parseBlocks, parseInline } from '../src/shared/markdown.ts';
import { parseWebviewMessage } from '../src/shared/messages.ts';
import { normalizeEvent, safeHref } from '../src/shared/protocol.ts';
import type { BackendEvent } from '../src/shared/protocol.ts';
import { initialState, openQuestions, reduce } from '../src/shared/reducer.ts';
import type { Action, State, ToolItem, QuestionItem } from '../src/shared/reducer.ts';
import { changedLines, isInside, MarkTracker } from '../src/shared/marks.ts';
import { buildSpawn, expandHome, runtimeDirName, trustedSetting } from '../src/ext/binary.ts';
import { LineSplitter } from '../src/ext/backend.ts';

const ev = (e: Record<string, unknown>): Action => ({ kind: 'event', event: normalizeEvent({ session: 's1', ...e }) as BackendEvent });
const run = (s: State, ...a: Action[]): State => a.reduce(reduce, s);
const sess = { id: 's1', name: 'S', agent: 'a', project: null, status: 'running', last_active: null, model: null, origin: null, readonly: false, parent: null, parentName: null };
const opened = (): State => reduce(initialState(), { kind: 'open', session: sess, history: [] });

test('markdown: Absaetze, Fett, Kursiv, Code', () => {
  const b = parseBlocks('Hallo **fett** und *schief* und `x < y`.\n\nZweiter Absatz');
  assert.equal(b.length, 2);
  const p = b[0];
  assert.equal(p.t, 'p');
  if (p.t !== 'p') return;
  assert.deepEqual(p.c.map((c) => c.t), ['text', 'b', 'text', 'i', 'text', 'code', 'text']);
});

test('markdown: Codeblock, auch ungeschlossen (Streaming)', () => {
  const b = parseBlocks('```ts\nconst a = 1;\n<b>x</b>\n```\nnachher');
  assert.equal(b[0].t, 'code');
  if (b[0].t === 'code') assert.equal(b[0].text, 'const a = 1;\n<b>x</b>');
  assert.equal(b[1].t, 'p');
  const open = parseBlocks('```\nhalb');
  assert.equal(open.length, 1);
  assert.equal(open[0].t, 'code');
});

test('markdown: Listen und Verschachtelung', () => {
  const b = parseBlocks('- eins\n- zwei\n  - innen\n- drei\n\n1. a\n2. b');
  assert.equal(b.length, 2);
  const l = b[0];
  assert.ok(l.t === 'list' && !l.ordered && l.items.length === 3);
  if (l.t === 'list') assert.equal(l.items[1].some((x) => x.t === 'list'), true);
  assert.ok(b[1].t === 'list' && b[1].ordered);
});

test('markdown: Links nur http(s), sonst Text', () => {
  const ok = parseInline('[Doku](https://example.org/a?b=1) und https://x.de/p.');
  assert.equal(ok.filter((n) => n.t === 'a').length, 2);
  const bad = parseInline('[klick](javascript:alert(1)) [x](data:text/html,hi) [y](vscode://command)');
  assert.equal(bad.filter((n) => n.t === 'a').length, 0);
  assert.equal(safeHref('javascript:alert(1)'), null);
  assert.equal(safeHref('file:///etc/passwd'), null);
  assert.ok(safeHref('https://example.org'));
});

test('markdown: HTML bleibt Text, kein Tag-Knoten moeglich', () => {
  const b = parseBlocks('<img src=x onerror=alert(1)> <script>x</script>');
  const p = b[0];
  assert.ok(p.t === 'p');
  if (p.t === 'p') assert.ok(p.c.every((n) => n.t === 'text'));
});

test('markdown: Pathologische Eingaben bleiben schnell', () => {
  const t = Date.now();
  parseInline('*'.repeat(20000));
  parseInline('['.repeat(5000) + ']'.repeat(5000));
  parseInline('`'.repeat(3000));
  parseBlocks('> '.repeat(500) + 'x');
  parseBlocks('- '.repeat(500) + 'x');
  assert.ok(Date.now() - t < 3000, 'zu langsam');
});

test('messages: Schema prueft Typ, Pflichtfelder, Laengen', () => {
  assert.deepEqual(parseWebviewMessage({ type: 'send', text: 'hi', extra: 1 }), { type: 'send', text: 'hi' });
  assert.equal(parseWebviewMessage({ type: 'send' }), null);
  assert.equal(parseWebviewMessage({ type: 'send', text: '' }), null);
  assert.equal(parseWebviewMessage({ type: 'send', text: 'x'.repeat(100001) }), null);
  assert.equal(parseWebviewMessage({ type: 'nope' }), null);
  assert.equal(parseWebviewMessage({ type: '__proto__' }), null);
  assert.equal(parseWebviewMessage({ type: 'constructor' }), null);
  assert.equal(parseWebviewMessage('send'), null);
  assert.equal(parseWebviewMessage(null), null);
  assert.equal(parseWebviewMessage({ type: 'openFile', call: 'c', index: -1 }), null);
  assert.equal(parseWebviewMessage({ type: 'openFile', call: 'c', index: 1.5 }), null);
  assert.deepEqual(parseWebviewMessage({ type: 'openDiff', call: 'c', index: 2 }), { type: 'openDiff', call: 'c', index: 2 });
  assert.deepEqual(parseWebviewMessage({ type: 'answer', question: 'q', choice: 'yes' }), { type: 'answer', question: 'q', choice: 'yes' });
  assert.deepEqual(parseWebviewMessage({ type: 'toggleFollow' }), { type: 'toggleFollow' });
});

test('protocol: Ereignisse normalisieren, Unbekanntes verwerfen', () => {
  assert.equal(normalizeEvent({ type: 'gibtsnicht' }), null);
  assert.equal(normalizeEvent('x'), null);
  assert.equal(normalizeEvent({ type: 'question' }), null);
  const t = normalizeEvent({ type: 'turn.end', session: 's', turn: 3, status: 'x', cost_usd: -1, context_used: 7 });
  assert.deepEqual(t, { type: 'turn.end', session: 's', turn: '3', status: 'done', cost_usd: 0, context_used: 1 });
  const ts = normalizeEvent({ type: 'tool.start', session: 's', turn: 't', call: 'c', name: 'Edit', summary: 'x', files: ['/a', 5] });
  assert.deepEqual((ts as { files: string[] }).files, ['/a']);
});

test('reducer: Streaming mit delta und ganzen Bloecken', () => {
  const s = run(
    opened(),
    ev({ type: 'turn.start', turn: 't1' }),
    ev({ type: 'text', turn: 't1', text: 'Hal', delta: true }),
    ev({ type: 'text', turn: 't1', text: 'lo', delta: true }),
    ev({ type: 'text', turn: 't1', text: 'Block', delta: false }),
    ev({ type: 'text', turn: 't1', text: 'Mehr', delta: true }),
  );
  const a = s.items.filter((i) => i.k === 'assistant');
  assert.deepEqual(a.map((i) => (i as { text: string }).text), ['Hallo', 'Block', 'Mehr']);
  assert.equal(s.busy, true);
});

test('reducer: Werkzeugkarte, Rundenende, Kosten', () => {
  let s = run(
    opened(),
    { kind: 'user', text: 'los' },
    ev({ type: 'tool.start', turn: 't', call: 'c1', name: 'Edit', summary: 'Edit a.py', files: ['/w/a.py'] }),
    ev({ type: 'tool.start', turn: 't', call: 'c2', name: 'Bash', summary: 'ls' }),
    ev({ type: 'tool.end', turn: 't', call: 'c1', ok: true, summary: 'done a.py', files: ['/w/a.py'] }),
  );
  const t1 = s.items.find((i) => i.k === 'tool' && i.call === 'c1') as ToolItem;
  assert.equal(t1.status, 'done');
  assert.deepEqual(t1.files, ['/w/a.py']);
  s = run(s, ev({ type: 'turn.end', turn: 't', status: 'done', cost_usd: 0.25, context_used: 0.4 }), ev({ type: 'turn.end', turn: 'u', status: 'done', cost_usd: 0.5, context_used: 0.5 }));
  assert.equal((s.items.find((i) => i.k === 'tool' && i.call === 'c2') as ToolItem).status, 'stop');
  assert.equal(s.busy, false);
  assert.equal(s.cost, 0.75);
  assert.equal(s.context, 0.5);
});

test('reducer: Fragen, Antwort, Schliessen, Abbruch schliesst offene Fragen', () => {
  let s = run(
    opened(),
    { kind: 'user', text: 'x' },
    ev({ type: 'question', question: 'q1', kind: 'permission', text: 'Run?', detail: 'rm -rf x', choices: ['yes', 'always', 'no'] }),
    ev({ type: 'question', question: 'q1', kind: 'permission', text: 'Run?', choices: ['yes'] }),
  );
  assert.equal(s.items.filter((i) => i.k === 'question').length, 1);
  s = run(s, { kind: 'answered', question: 'q1', choice: 'yes' });
  assert.equal((s.items.find((i) => i.k === 'question') as QuestionItem).answered, 'yes');
  s = run(s, ev({ type: 'question', question: 'q2', kind: 'ask', text: 'Wie?', choices: [] }), { kind: 'cancelling' });
  assert.equal(s.cancelling, true);
  s = run(s, ev({ type: 'turn.end', turn: 't', status: 'cancelled', cost_usd: 0, context_used: null }));
  assert.equal((s.items.find((i) => i.k === 'question' && i.question === 'q2') as QuestionItem).answered, '(closed)');
  assert.equal(s.busy, false);
  assert.equal(s.cancelling, false);
});

test('reducer: Ereignisse fremder Sitzungen werden ignoriert, Verlauf wird eingelesen', () => {
  let s = opened();
  s = run(s, { kind: 'event', event: normalizeEvent({ type: 'text', session: 'andere', turn: 't', text: 'fremd', delta: false }) as BackendEvent });
  assert.equal(s.items.length, 0);
  const hist = [
    normalizeEvent({ type: 'user', text: 'frage' }),
    normalizeEvent({ type: 'turn.start', turn: 't' }),
    normalizeEvent({ type: 'text', turn: 't', text: 'antwort', delta: false }),
    normalizeEvent({ type: 'tool.start', turn: 't', call: 'c', name: 'Bash', summary: 'ls' }),
  ] as BackendEvent[];
  s = run(s, { kind: 'open', session: sess, history: hist });
  assert.equal(s.busy, false); // Verlauf sperrt nie die Eingabe
  assert.equal(s.items.length, 3);
  assert.equal((s.items[2] as ToolItem).status, 'stop');
});

test('reducer: Verbindungsverlust beendet den Zug, Zustand bleibt reine Funktion', () => {
  const a = run(opened(), { kind: 'user', text: 'x' }, ev({ type: 'tool.start', turn: 't', call: 'c', name: 'n', summary: '' }));
  const frozen = JSON.stringify(a);
  const b = reduce(a, { kind: 'conn', status: 'restarting', detail: 'x' });
  assert.equal(JSON.stringify(a), frozen);
  assert.equal(b.busy, false);
  assert.equal((b.items.find((i) => i.k === 'tool') as ToolItem).status, 'stop');
  assert.equal(b.session?.id, 's1');
});

test('marks: Pfadpruefung (posix und windows)', () => {
  assert.equal(isInside('/w/a/b.py', ['/w'], path.posix), true);
  assert.equal(isInside('/w', ['/w'], path.posix), true);
  assert.equal(isInside('/wx/a.py', ['/w'], path.posix), false);
  assert.equal(isInside('/w/../etc/passwd', ['/w'], path.posix), false);
  assert.equal(isInside('rel/a.py', ['/w'], path.posix), false);
  assert.equal(isInside('/w/a\0.py', ['/w'], path.posix), false);
  assert.equal(isInside('C:\\Users\\x\\proj\\a.py', ['c:\\users\\x\\proj'], path.win32), true);
  assert.equal(isInside('C:\\Users\\x\\proj2\\a.py', ['C:\\Users\\x\\proj'], path.win32), false);
  assert.equal(isInside('C:\\Users\\x\\proj\\..\\other\\a.py', ['C:\\Users\\x\\proj'], path.win32), false);
  assert.equal(isInside('D:\\a.py', ['C:\\'], path.win32), false);
  assert.equal(isInside('/w/a.py', [], path.posix), false);
});

test('marks: Zeilenvergleich', () => {
  assert.deepEqual(changedLines('a\nb\nc', 'a\nb\nc'), []);
  assert.deepEqual(changedLines('a\nb\nc', 'a\nX\nc'), [{ start: 1, end: 1 }]);
  assert.deepEqual(changedLines('a\nb\nc', 'a\nb\nc\nd\ne'), [{ start: 3, end: 4 }]);
  assert.deepEqual(changedLines('', 'neu\nzwei'), [{ start: 0, end: 1 }]);
  assert.deepEqual(changedLines('a\nb\nc', 'a\nc'), []); // nur Loeschung: nichts hervorzuheben
  assert.deepEqual(changedLines('1\n2\n3\n4\n5\n6', '1\nA\n3\n4\nB\nC\n6'), [{ start: 1, end: 1 }, { start: 4, end: 5 }]);
  assert.deepEqual(changedLines('a\nb\nc\nd', 'a\nNEU\nb\nc\nd'), [{ start: 1, end: 1 }]);
});

test('marks: Tracker arbeitet nur im Workspace/Baum, Markierung bleibt bis clear', () => {
  const t = new MarkTracker();
  const root = path.resolve('/w');
  const tree = path.resolve('/tree');
  t.setRoots([root, tree]);
  const inside = path.join(root, 'a.py');
  const outside = path.resolve('/etc/passwd');
  assert.deepEqual(t.start('c1', [inside, outside]), [inside]);
  assert.deepEqual(t.snapshot(), { working: [inside], changed: [] });
  const r = t.end('c1', [inside, outside]);
  assert.deepEqual(r.changed, [inside]);
  assert.deepEqual(t.snapshot(), { working: [], changed: [inside] });
  t.start('c2', [inside]);
  t.endAll();
  assert.deepEqual(t.snapshot().working, []);
  t.clear();
  assert.deepEqual(t.snapshot(), { working: [], changed: [] });
});

test('binary: Startaufruf je Betriebssystem, ohne Shell/.cmd', () => {
  const none = () => null;                                    // kein Zeiger: die einzelne Runtime frueherer Launcher
  const w = buildSpawn('C:\\Users\\m\\lightmind', 'C:\\proj', 'win32', 'x64', none);
  assert.equal(w.command, 'C:\\Users\\m\\lightmind\\system\\runtime-windows-x86_64\\Scripts\\lm.exe');
  assert.deepEqual(w.args, ['serve', '--stdio', '--workdir', 'C:\\proj']);
  assert.equal(w.cwd, 'C:\\Users\\m\\lightmind');
  const l = buildSpawn('/home/m/lightmind', '/home/m/p', 'linux', 'x64', none);
  assert.equal(l.command, '/home/m/lightmind/system/runtime-linux-x86_64/bin/lm');
  assert.equal(l.cwd, '/home/m/lightmind');
  assert.equal(runtimeDirName('linux', 'arm64'), 'linux-arm64');
  assert.equal(runtimeDirName('darwin', 'arm64'), 'macos-arm64');
  assert.ok(!/\.cmd$/i.test(w.command));
  assert.equal(expandHome('~/lightmind', '/home/m', path.posix), '/home/m/lightmind');
  assert.equal(expandHome('/abs', '/home/m', path.posix), '/abs');
});

test('binary: der Zeiger waehlt den neuesten Build, ein ungueltiger Zeiger faellt zurueck', () => {
  const seen: string[] = [];
  const pointer = (id: string | null) => (file: string) => { seen.push(file); return id; };
  const w = buildSpawn('C:\\Users\\m\\lightmind', 'C:\\proj', 'win32', 'x64', pointer('r3'));
  assert.equal(w.command, 'C:\\Users\\m\\lightmind\\system\\runtimes\\windows-x86_64\\r3\\Scripts\\lm.exe');
  assert.equal(seen[0], 'C:\\Users\\m\\lightmind\\system\\runtimes\\windows-x86_64.current');
  const l = buildSpawn('/home/m/lightmind', '/home/m/p', 'linux', 'x64', pointer('r12'));
  assert.equal(l.command, '/home/m/lightmind/system/runtimes/linux-x86_64/r12/bin/lm');
  for (const bad of ['../x', 'r3/..', 'x', '', 'r']) {
    assert.equal(buildSpawn('/t', '/w', 'linux', 'x64', pointer(bad)).command, '/t/system/runtime-linux-x86_64/bin/lm', bad);
  }
});

test('backend: Zeilen ueber Blockgrenzen, UTF-8, CRLF', () => {
  const got: string[] = [];
  const ls = new LineSplitter((l) => got.push(l));
  const bytes = Buffer.from('{"a":"\u00e4\u00f6"}\r\n{"b":1}\n\n{"c"', 'utf8');
  ls.push(bytes.subarray(0, 8)); // trennt mitten im Umlaut
  ls.push(bytes.subarray(8));
  ls.push(':2}\n');
  assert.deepEqual(got, ['{"a":"\u00e4\u00f6"}', '{"b":1}', '{"c":2}']);
});

test('markdown: Unterstriche in Namen sind kein Fett/Kursiv', () => {
  const n = parseInline('window.__pwned=1 und my_var_name und a__b__c');
  assert.ok(n.every((x) => x.t === 'text'));
  assert.equal(parseInline('_kursiv_ und __fett__').filter((x) => x.t === 'i' || x.t === 'b').length, 2);
});

test('Einstellung lightmind.home: Workspace-Wert wird ignoriert', () => {
  assert.equal(trustedSetting({ globalValue: '/mein/baum', workspaceValue: '/boese', workspaceFolderValue: '/boese2', defaultValue: '~/lightmind' }, 'x'), '/mein/baum');
  assert.equal(trustedSetting({ workspaceValue: '/boese', defaultValue: '~/lightmind' }, 'x'), '~/lightmind');
  assert.equal(trustedSetting({ workspaceFolderValue: '/boese' }, '~/lightmind'), '~/lightmind');
  assert.equal(trustedSetting(undefined, '~/lightmind'), '~/lightmind');
});

test('protokoll: text mit role user ist ein Nutzertext; Diff nur bei gemeldeten Aenderungen', () => {
  const u = normalizeEvent({ type: 'text', session: 's', turn: 'h1', text: 'frage', delta: false, role: 'user' });
  assert.deepEqual(u, { type: 'user', session: 's', text: 'frage' });
  let s = opened();
  s = run(
    s,
    ev({ type: 'tool.start', turn: 't', call: 'b', name: 'bash', summary: 'ls', files: [] }),
    ev({ type: 'tool.end', turn: 't', call: 'b', ok: true, summary: 'ok', files: [] }),
    ev({ type: 'tool.start', turn: 't', call: 'r', name: 'read', summary: 'read a', files: ['/w/a'] }),
    ev({ type: 'tool.end', turn: 't', call: 'r', ok: true, summary: 'ok', files: [] }),
    ev({ type: 'tool.start', turn: 't', call: 'e', name: 'edit', summary: 'edit a', files: ['/w/a'] }),
    ev({ type: 'tool.end', turn: 't', call: 'e', ok: true, summary: 'Edited a', files: ['/w/a'] }),
  );
  assert.deepEqual(s.items.map((i) => (i.k === 'tool' ? i.changed : null)), [false, false, true]);
});

test('Unteragent-Mitteilung: eigene Zeile, nie ein Nutzertext, auch im Verlauf', () => {
  const raw = { type: 'subagent.message', session: 's1', turn: 't2', name: 'scout', ok: false, summary: 'x'.repeat(5000), file: '/o/sub-c1.md' };
  const e = normalizeEvent(raw);
  assert.equal(e?.type, 'subagent.message');
  assert.equal((e as { summary: string }).summary.length, 2000);
  assert.equal((normalizeEvent({ ...raw, name: '' }) as { name: string }).name, 'agent');
  const s = run(opened(), ev({ type: 'text', turn: 't1', text: 'Hal', delta: true }), ev({ type: 'subagent.message', turn: 't2', name: 'scout', ok: true, summary: 'found it', file: '/o/a.md' }));
  assert.deepEqual(s.items.map((i) => i.k), ['assistant', 'submsg']);
  assert.equal((s.items[0] as { open: boolean }).open, false, 'der laufende Text wird abgeschlossen');
  assert.equal(s.items.some((i) => i.k === 'user'), false);
  // Verlauf (session.open): dieselbe Zeile
  const hist = normalizeEvent({ type: 'subagent.message', session: 's1', turn: 'h1', name: 'scout', ok: true, summary: 'old', file: '' }) as BackendEvent;
  const h = reduce(initialState(), { kind: 'open', session: sess, history: [hist] });
  assert.deepEqual(h.items.map((i) => i.k), ['submsg']);
});

test('Webview-Nachricht subCancel: nur mit Id', () => {
  assert.deepEqual(parseWebviewMessage({ type: 'subCancel', id: 'c1' }), { type: 'subCancel', id: 'c1' });
  assert.equal(parseWebviewMessage({ type: 'subCancel' }), null);
  assert.equal(parseWebviewMessage({ type: 'subCancel', id: '' }), null);
});

test('openQuestions: offene Anfragen in Reihenfolge, beantwortete nicht mehr', () => {
  let s = run(
    opened(),
    ev({ type: 'question', question: 'q1', kind: 'permission', text: 'Run?', choices: ['yes', 'no'] }),
    { kind: 'user', text: 'weiter' } as Action,
    ev({ type: 'question', question: 'q2', kind: 'ask', text: 'Wie?', choices: [] }),
  );
  assert.deepEqual(openQuestions(s).map((q) => q.question), ['q1', 'q2']);
  s = run(s, { kind: 'answered', question: 'q1', choice: 'yes' });
  assert.deepEqual(openQuestions(s).map((q) => q.question), ['q2']);
});

test('Einwurf: pending dann delivered; Bindung an aeltesten ungebundenen Eintrag', () => {
  assert.equal(normalizeEvent({ type: 'interject', session: 's1', id: 'i1', state: 'bogus', text: 'x' }), null);
  assert.equal((normalizeEvent({ type: 'interject', session: 's1', id: 'x'.repeat(99), state: 'pending', text: '' }) as { id: string }).id.length, 40);
  let s = run(opened(), ev({ type: 'turn.start', turn: 't1' }), { kind: 'user', text: 'one', interject: true }, { kind: 'user', text: 'two', interject: true });
  assert.equal(s.busy, true);
  const ij = (i: number) => (s.items[i] as { ij?: { id: string | null; state: string } }).ij;
  assert.deepEqual(ij(0), { id: null, state: 'pending' });
  s = run(s, ev({ type: 'interject', id: 'i1', state: 'pending', text: '' }));
  assert.deepEqual(ij(0), { id: 'i1', state: 'pending' });
  assert.deepEqual(ij(1), { id: null, state: 'pending' });
  s = run(s, ev({ type: 'interject', id: 'i2', state: 'pending', text: '' }), ev({ type: 'interject', id: 'i1', state: 'delivered', text: 'one' }));
  assert.deepEqual(ij(0), { id: 'i1', state: 'delivered' });
  assert.deepEqual(ij(1), { id: 'i2', state: 'pending' });
  s = run(s, ev({ type: 'turn.end', turn: 't1', status: 'done', cost_usd: 0, context_used: null }));
  assert.deepEqual(ij(1), { id: 'i2', state: 'pending' }, 'turn.end keeps states');
  // without a local item (history): a new user item
  const h = run(opened(), ev({ type: 'interject', id: 'i9', state: 'queued', text: 'old words' }));
  assert.deepEqual(h.items.map((i) => i.k), ['user']);
  assert.equal((h.items[0] as { text: string }).text, 'old words');
});

test('Einwurf: queued dann sent; dropped behaelt den Text; Unteragent -> Hinweis; Sendefehler', () => {
  let s = run(opened(), { kind: 'user', text: 'later', interject: true }, ev({ type: 'interject', id: 'q7', state: 'queued', text: '' }), ev({ type: 'interject', id: 'q7', state: 'sent', text: '' }));
  assert.deepEqual((s.items[0] as { ij: unknown }).ij, { id: 'q7', state: 'sent' });
  s = run(opened(), { kind: 'user', text: 'lost', interject: true }, ev({ type: 'interject', id: 'i3', state: 'pending', text: '' }), ev({ type: 'interject', id: 'i3', state: 'dropped', text: 'lost' }));
  const u = s.items[0] as { text: string; ij: { state: string } };
  assert.equal(u.ij.state, 'dropped');
  assert.equal(u.text, 'lost');
  s = run(opened(), ev({ type: 'interject', id: 'i4', state: 'delivered', text: '', parent: 'c1', agent: 'scout' }));
  assert.deepEqual(s.items.map((i) => i.k), ['notice']);
  assert.equal((s.items[0] as { text: string }).text, 'scout read the message passed on to it');
  s = run(opened(), ev({ type: 'turn.start', turn: 't1' }), { kind: 'user', text: 'nope', interject: true }, { kind: 'ijfailed', text: 'boom' });
  assert.deepEqual((s.items[0] as { ij: unknown }).ij, { id: null, state: 'dropped' });
  assert.equal(s.items[1].k, 'error');
  assert.equal(s.busy, true);
  s = run(opened(), { kind: 'user', text: 'raced', interject: true }, { kind: 'ijclear' });
  assert.equal((s.items[0] as { ij?: unknown }).ij, undefined);
});
