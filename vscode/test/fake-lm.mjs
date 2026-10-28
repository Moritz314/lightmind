// Gespieltes Backend: spricht Protokoll v1 ueber stdin/stdout. Nur fuer Tests.
// Argumente wie beim echten lm: serve --stdio --workdir <ordner>. Umgebung FAKE_LM_LOG: Datei fuer Anfrageprotokoll.
import { appendFileSync, writeFileSync } from 'node:fs';
import { createInterface } from 'node:readline';

const wd = process.argv[process.argv.indexOf('--workdir') + 1] || process.cwd();
const logFile = process.env.FAKE_LM_LOG || '';
const log = (o) => {
  if (logFile) appendFileSync(logFile, JSON.stringify(o) + '\n');
};
const out = (o) => process.stdout.write(JSON.stringify(o) + '\n');
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let session = null;
let turnSeq = 0;
let cancelled = false;
let running = false; // a turn is in progress
let ijSeq = 0;
let bgSub = null; // id des laufenden Hintergrund-Unteragenten
let pendingQ = null; // { id, resolve }
let history = [];
const names = { title: 'Old name', description: 'Old description' }; // notes.get / notes.name
const settingsStore = { 'defaults.provider': 'claude-abo', 'defaults.context': '200k', 'defaults.mode': 'auto', 'defaults.model': 'sonnet', 'defaults.effort': 'medium', 'sessions.max_parallel': 2, 'ui.boot_wait': true, 'vscode.agent': 'coder', 'vscode.note': 'hello' };
const sessions = [
  { id: 'old1', name: 'Fix login bug', agent: 'coder', project: 'demo', status: 'paused', last_active: '2026-10-20 18:02', model: 'sonnet', origin: 'tui' },
  { id: 'old2', name: 'Docs pass', agent: 'writer', project: null, status: 'done', last_active: '2026-10-19 09:10', model: 'haiku', origin: 'vscode' },
  { id: 'old3', name: 'Release notes', agent: 'writer', project: 'demo', status: 'running', last_active: '2026-10-21 20:30', model: 'opus', origin: '', readonly: true },
];
const archive = [
  { id: 'arc1', name: 'Old experiment', agent: 'coder', project: 'demo', status: 'done', last_active: '2026-10-10 12:00', model: 'haiku', origin: 'tui', archived: true },
  { id: 'arc2', name: 'Draft letter', agent: 'writer', project: null, status: 'done', last_active: '2026-10-09 08:30', model: 'sonnet', origin: 'vscode', archived: true },
];
const restore = (id) => {
  const at = archive.findIndex((x) => x.id === id);
  if (at < 0) return null;
  const [s] = archive.splice(at, 1);
  s.archived = false;
  sessions.unshift(s);
  return s;
};
const agents = [
  { name: 'coder', project: 'demo', provider: null, model: 'opus', effort: 'high', mode: null, effective: { project: 'demo', provider: 'claude-abo', model: 'opus', effort: 'high', mode: 'auto' } },
  { name: 'writer', project: null, provider: 'claude-abo-2', model: 'sonnet', effort: null, mode: 'plan', effective: { project: null, provider: 'claude-abo-2', model: 'sonnet', effort: 'medium', mode: 'plan' } },
];
let cur = { model: 'sonnet', effort: 'high', mode: 'auto' };
const status = (s) => out({ type: 'status', session: s.id, model: cur.model, effort: cur.effort, mode: cur.mode, project: 'demo', account: 'acct' });

if (!process.env.FAKE_LM_NO_HELLO) out({ type: 'hello', version: 1, lm: '0.1.1', codename: 'orbit v1', tree: process.cwd(), workdir: wd, project: null });
log({ started: true, pid: process.pid, argv: process.argv.slice(2), cwd: process.cwd() });

function ev(o) {
  const e = { session: session.id, ...o };
  history.push(e);
  out(e);
}

async function runTurn(text) {
  cancelled = false;
  running = true;
  const turn = `t${++turnSeq}`;
  history.push({ type: 'text', session: session.id, turn: `h${turnSeq}`, text, delta: false, role: 'user' });
  ev({ type: 'turn.start', turn });
  const stream = async (s, ms = 15) => {
    for (const w of s.match(/\S+\s*/g) || []) {
      if (cancelled) return;
      ev({ type: 'text', turn, text: w, delta: true });
      await sleep(ms);
    }
  };
  if (/xss/.test(text)) {
    await stream('Try <img src=x onerror="window.__pwned=1"> and [bad](javascript:window.__pwned=2) done.');
  } else if (/long/.test(text)) {
    for (let i = 0; i < 400 && !cancelled; i++) {
      ev({ type: 'text', turn, text: `tick${i} `, delta: true });
      await sleep(40);
    }
  } else if (/crash/.test(text)) {
    ev({ type: 'text', turn, text: 'about to crash', delta: true });
    await sleep(100);
    process.exit(3);
  } else if (/bgsub/.test(text)) {
    // ein Unteragent im Hintergrund: Zug endet, er arbeitet weiter, bis er fertig ist (bgdone) oder gestoppt wird
    const call = `c${turnSeq}`;
    ev({ type: 'tool.start', turn, call, name: 'spawn', summary: 'Spawn read the docs', files: [] });
    ev({ type: 'subagent.start', turn, id: call, name: 'scout', task: 'read the docs' });
    ev({ type: 'tool.end', turn, call, ok: true, summary: 'started sub-agent scout in the background', files: [] });
    bgSub = call;
    await stream('Scout is on it.');
  } else if (/bgdone/.test(text)) {
    ev({ type: 'subagent.end', turn, id: bgSub || 'c0', ok: true, summary: 'the docs say <b>yes</b>' });
    bgSub = null;
    ev({ type: 'turn.end', turn, status: 'done', cost_usd: 0, context_used: 0.1 });
    const t2 = `t${++turnSeq}`;
    ev({ type: 'subagent.message', turn: t2, name: 'scout', ok: true, summary: 'the docs say <b>yes</b>, and a long answer follows that does not fit one line of the narrow side panel', file: '/work/out/sub-c1.md' });
    ev({ type: 'turn.start', turn: t2 });
    ev({ type: 'text', turn: t2, text: 'Thanks scout.', delta: true });
    ev({ type: 'turn.end', turn: t2, status: 'done', cost_usd: 0, context_used: 0.1 });
    return;
  } else if (/run/.test(text)) {
    await stream('I will **edit** the file now.\n\n- first check\n- then change\n\n```py\nprint("hi")\n```\n');
    const call = `c${turnSeq}`;
    const file = `${wd}/src/app.py`;
    ev({ type: 'tool.start', turn, call, name: 'Bash', summary: 'pytest -q tests/test_app.py', files: [] });
    const q = `q${turnSeq}`;
    ev({ type: 'question', question: q, kind: 'permission', text: 'Run this command?', detail: 'pytest -q tests/test_app.py', choices: ['yes', 'always', 'no'] });
    const choice = await new Promise((resolve) => {
      pendingQ = { id: q, resolve };
    });
    pendingQ = null;
    ev({ type: 'question.closed', question: q, choice });
    if (cancelled) {
      ev({ type: 'turn.end', turn, status: 'cancelled', cost_usd: 0.001, context_used: 0.2 });
      return;
    }
    if (choice === 'no') {
      ev({ type: 'tool.end', turn, call, ok: false, summary: 'denied by user', files: [] });
      await stream('Understood, I did not run it.');
    } else {
      ev({ type: 'tool.end', turn, call, ok: true, summary: '3 passed in 0.4s', files: [] });
      ev({ type: 'tool.start', turn, call: call + 'b', name: 'Edit', summary: 'Edit src/app.py', files: [file] });
      await sleep(30);
      ev({ type: 'tool.end', turn, call: call + 'b', ok: true, summary: 'Edited src/app.py (+4 -1)', files: [file] });
      await stream('All done, tests pass.');
    }
  } else if (/ask/.test(text)) {
    const q = `q${turnSeq}`;
    ev({ type: 'question', question: q, kind: 'ask', text: 'Which database?', detail: '', choices: ['sqlite', 'postgres'] });
    const choice = await new Promise((resolve) => {
      pendingQ = { id: q, resolve };
    });
    pendingQ = null;
    ev({ type: 'question.closed', question: q, choice });
    await stream(`You picked ${choice}.`);
  } else {
    await stream('Hello from the fake backend. This is **bold**, *italic* and `code`.');
  }
  running = false;
  ev({ type: 'turn.end', turn, status: cancelled ? 'cancelled' : 'done', cost_usd: 0.0123, context_used: 0.34 });
}

function open(id) {
  const base = sessions.find((s) => s.id === id) || { id, name: 'New session', agent: 'coder', project: null, status: 'running', last_active: null, model: 'sonnet', origin: 'vscode' };
  session = base;
  history = [];
  return base;
}

const rl = createInterface({ input: process.stdin });
rl.on('line', (line) => {
  let m;
  try {
    m = JSON.parse(line);
  } catch {
    return;
  }
  log(m);
  const ok = (data = {}) => out({ type: 'result', id: m.id, ok: true, data });
  const no = (error) => out({ type: 'result', id: m.id, ok: false, error });
  switch (m.type) {
    case 'shutdown':
      process.exit(0);
      break;
    case 'session.list':
      ok({ sessions: m.archived ? archive : sessions });
      break;
    case 'session.unarchive': {
      const s = restore(m.session);
      if (!s) return no(`not in the archive: ${m.session}`);
      ok({ session: s.id, text: 'restored' });
      break;
    }
    case 'notes.get': {
      if (m.session === 'gone') return no('no such chat: gone');
      ok({ session: m.session, title: names.title, description: names.description, items: [
        { type: 'folder', id: 'f1', title: 'Plan', created: '', items: [{ type: 'task', id: 't1', title: 'first step', state: 'doing', info: 'working on it', updated: '', remarks: [{ id: 'r1', text: 'please hurry', by: 'user', ts: '', seen: true }] }] },
        { type: 'note', id: 'n1', title: 'idea', text: 'a longer text', updated: '' },
      ] });
      break;
    }
    case 'notes.name': {
      if (m.session === 'gone') return no('no such chat: gone');
      names[m.field] = m.text || '';
      ok({ session: m.session, title: names.title, description: names.description, text: `${m.field}: ${m.text}` });
      break;
    }
    case 'session.end': {
      if (session && session.id === m.session) return no('this chat is open here: open another chat or start a new one to leave it');
      const s = sessions.find((x) => x.id === m.session);
      if (!s) return no(`no such chat: ${m.session}`);
      s.status = 'done';
      ok({ session: m.session, text: 'ended' });
      break;
    }
    case 'session.new': {
      const s = open(`new${Date.now() % 100000}`);
      if (m.agent) s.agent = m.agent;
      if (m.model) s.model = m.model;
      cur = { model: s.model, effort: m.effort || 'high', mode: m.mode || 'auto' };
      sessions.unshift(s);
      ok({ session: s });
      status(s);
      break;
    }
    case 'session.set': {
      if (!session) return no('no session is open');
      if (session.readonly) return no('read only: this chat is open in another window');
      for (const k of ['model', 'effort', 'mode']) if (m[k]) cur[k] = m[k];
      ok({ session });
      status(session);
      break;
    }
    case 'session.open': {
      const restored = !!restore(m.session); // opening an archived chat brings it back, as the real backend does
      const s = open(m.session);
      ok({
        ...(restored ? { restored: true } : {}),
        session: s,
        history: [
          { type: 'text', session: s.id, turn: 'h1', text: 'earlier question', delta: false, role: 'user' },
          { type: 'turn.start', session: s.id, turn: 'h1' },
          { type: 'text', session: s.id, turn: 'h1', text: 'earlier **answer**', delta: false },
          { type: 'turn.end', session: s.id, turn: 'h1', status: 'done', cost_usd: 0.01, context_used: 0.1 },
        ],
      });
      cur = { model: s.model, effort: 'high', mode: 'auto' };
      status(s);
      break;
    }
    case 'send':
      if (!session) return no('no session');
      if (running) {
        const id = `i${++ijSeq}`;
        ok({ interject: id, state: 'pending' });
        ev({ type: 'interject', id, state: 'pending', text: '' });
        ev({ type: 'interject', id, state: 'delivered', text: m.text });
        break;
      }
      ok();
      void runTurn(m.text);
      break;
    case 'cancel':
      cancelled = true;
      if (pendingQ) pendingQ.resolve('(cancelled)');
      ok();
      break;
    case 'subagent.cancel':
      if (!bgSub || m.sub !== bgSub) return no(`no sub-agent '${m.sub}' is working in the background`);
      ev({ type: 'subagent.end', turn: `t${turnSeq}`, id: bgSub, ok: false, summary: 'cancelled by the user' });
      bgSub = null;
      ok();
      break;
    case 'answer':
      if (pendingQ && pendingQ.id === m.question) pendingQ.resolve(m.choice);
      ok();
      break;
    case 'settings.get':
      ok({
        fields: [
          { key: 'defaults.provider', label: 'Account', help: 'Which account pays for a new chat.', value: settingsStore['defaults.provider'], choices: ['claude-abo', 'claude-abo-2'], scope: 'config' },
          { key: 'defaults.model', label: 'Model', help: 'A model of that account, or a tier.', value: settingsStore['defaults.model'], choices: ['sonnet', 'haiku', 'opus'], scope: 'config' },
          { key: 'defaults.effort', label: 'Effort', help: 'How hard the model thinks.', value: settingsStore['defaults.effort'], choices: ['low', 'medium', 'high'], scope: 'config' },
          { key: 'defaults.context', label: 'Context', help: 'How much a chat holds.', value: settingsStore['defaults.context'], choices: ['100k', '200k', '500k'], scope: 'config' },
          { key: 'defaults.mode', label: 'Mode', help: 'How free a new chat is.', value: settingsStore['defaults.mode'], choices: ['auto', 'plan', 'bypass'], scope: 'config' },
          { key: 'sessions.max_parallel', label: 'Max parallel', help: 'How many helpers run at once.', value: settingsStore['sessions.max_parallel'], choices: [1, 2, 4, 8], scope: 'config' },
          { key: 'ui.boot_wait', label: 'Wait for Enter', help: 'Whether the checklist stays.', value: settingsStore['ui.boot_wait'], choices: [true, false], scope: 'config' },
          { key: 'vscode.agent', label: 'Agent', help: 'Agent of a new chat in VS Code.', value: settingsStore['vscode.agent'], choices: ['coder', 'writer', 'reviewer'], scope: 'config' },
          { key: 'vscode.note', label: 'Note', help: 'Free text.', value: settingsStore['vscode.note'], choices: [], scope: 'config' },
        ],
        agents,
        ...(process.env.FAKE_LM_MODES_ERROR ? { modes_error: process.env.FAKE_LM_MODES_ERROR } : {}),
      });
      break;
    case 'settings.set': {
      const ch = m.changes || {};
      const bad = Object.values(ch).find((v) => v === 'bogus');
      if (bad !== undefined) { no('Effort: not one of the allowed values'); break; }
      Object.assign(settingsStore, ch);
      ok({ saved: Object.keys(ch).length });
      break;
    }
    default:
      no(`unknown type ${m.type}`);
  }
});
rl.on('close', () => process.exit(0));
