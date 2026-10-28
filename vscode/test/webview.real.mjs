// Logiktest der Webview gegen ein ECHTES `lm serve --stdio` (geskripteter Provider aus tests/serve_runner.py, kein Modell).
// Vermittler: der echte Kern (ChatCore/Backend) startet serve_runner.py; die Webview-HTML laeuft in headless Chrome mit gespieltem acquireVsCodeApi.
// Umgebung: LM_PYTHON (Python mit lightmind, Standard ~/lightmind-build/venv/bin/python), CHROME, E2E_OUT (Screenshots).
// Ohne Chrome oder Python wird uebersprungen.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { homedir, tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { Backend } from '../src/ext/backend.ts';
import { ChatCore } from '../src/ext/core.ts';
import { buildHtml } from '../src/ext/html.ts';
import { makeHost } from './helpers.ts';

const here = path.dirname(fileURLToPath(import.meta.url));
const media = path.join(here, '..', 'media');
const outDir = process.env.E2E_OUT || path.join(here, 'out');
const runner = path.join(here, '..', '..', 'tests', 'serve_runner.py');
const py = process.env.LM_PYTHON || path.join(homedir(), 'lightmind-build', 'venv', 'bin', 'python');
const chrome = [process.env.CHROME, '/usr/bin/google-chrome', '/usr/bin/chromium', '/usr/bin/chromium-browser', 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'].find((p) => p && existsSync(p));
const skip = !chrome ? 'kein Chrome gefunden' : !existsSync(py) ? 'kein Python mit lightmind (LM_PYTHON)' : false;

// Frischer Baum aus den Vorlagen, ohne den Start-Hook (der ruft uv und git).
const SETUP = [
  'import sys',
  'import shutil',
  'from pathlib import Path',
  'from lightmind.init import init_tree',
  'dst = Path(sys.argv[1]) / "lm"',
  'init_tree(dst)',
  'shutil.copytree(sys.argv[2], dst, dirs_exist_ok=True)',   // Beispiel-Agenten und Konto der Testbeispiele
  'p = dst / "lightmind.md"',
  't = p.read_text("utf-8")',
  'a = t.index("on_start:")',
  'b = t.index("timeout: 30", a) + len("timeout: 30") + 1',
  'p.write_text(t[:a] + "on_start: []\\n" + t[b:], "utf-8")',
].join('\n');

test('Webview gegen echtes lm serve --stdio', { skip, timeout: 180000 }, async () => {
  assert.ok(existsSync(path.join(media, 'main.js')), 'media/main.js fehlt, erst "npm run build"');
  const { default: puppeteer } = await import('puppeteer-core');
  mkdirSync(outDir, { recursive: true });
  const dir = mkdtempSync(path.join(tmpdir(), 'lmreal-'));
  execFileSync(py, ['-I', '-c', SETUP, dir, path.join(here, '..', '..', 'tests', 'fixtures', 'overlay')], { stdio: 'inherit' });
  const tree = path.join(dir, 'lm');
  const wd = path.join(dir, 'work');
  mkdirSync(wd);
  const target = path.join(tree, 'providers', 'extra.md'); // im Baum: write fragt dort nach
  const script = [
    [['deltas', ['Hello ', '**bold** ', 'world']]],
    [['deltas', ['I will write the file.\n']], ['call', 'write', { path: target, content: 'print(1)\n' }, 'c1']],
    [['say', 'All done.']],
    [['deltas', ['tick1 ', 'tick2 ', 'tick3 ']], ['sleep', 30]],
  ];
  const sp = path.join(dir, 'script.json');
  writeFileSync(sp, JSON.stringify(script));

  const server = http.createServer((req, res) => {
    const u = req.url || '/';
    if (u === '/main.js') return res.writeHead(200, { 'content-type': 'text/javascript' }).end(readFileSync(path.join(media, 'main.js')));
    if (u === '/main.css') return res.writeHead(200, { 'content-type': 'text/css' }).end(readFileSync(path.join(media, 'main.css')));
    res.writeHead(200, { 'content-type': 'text/html' }).end(buildHtml({ nonce: 'n0nce1234', cspSource: origin, scriptUri: `${origin}/main.js`, styleUri: `${origin}/main.css` }));
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const origin = `http://127.0.0.1:${server.address().port}`;

  const h = makeHost();
  const env = { ...process.env, PYTHONUNBUFFERED: '1', PYTHONIOENCODING: 'utf-8' };
  for (const k of ['LM_RUN', 'LM_DEPTH', 'LM_PARENT']) delete env[k];
  const core = new ChatCore({
    workdir: wd,
    tree,
    follow: true,
    ui: h.ui,
    log: () => {},
    restartDelaysMs: [100, 200, 400],
    shutdownGraceMs: 5000,
    makeBackend: () => new Backend({ command: py, args: ['-I', runner, sp, '--home', tree, 'serve', '--stdio', '--workdir', wd], cwd: tree }, env),
  });

  const browser = await puppeteer.launch({ executablePath: chrome, headless: true, args: ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage'] });
  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 420, height: 820, deviceScaleFactor: 2 });
    const pageErrors = [];
    page.on('pageerror', (e) => pageErrors.push(String(e)));
    await page.evaluateOnNewDocument(() => {
      window.__st = undefined;
      window.acquireVsCodeApi = () => ({
        postMessage: (m) => window.__host(m),
        getState: () => window.__st,
        setState: (s) => {
          window.__st = s;
        },
      });
      document.addEventListener('DOMContentLoaded', () => {
        document.body.classList.add('vscode-dark');
        const r = document.documentElement.style;
        const v = { '--vscode-sideBar-background': '#181818', '--vscode-editor-background': '#1f1f1f', '--vscode-foreground': '#cccccc', '--vscode-descriptionForeground': '#9d9d9d', '--vscode-input-background': '#313131', '--vscode-widget-border': '#2b2b2b', '--vscode-input-border': '#3c3c3c', '--vscode-editorWidget-background': '#202020', '--vscode-font-family': 'Segoe UI, system-ui, sans-serif', '--vscode-font-size': '13px', '--vscode-editor-font-family': 'Consolas, DejaVu Sans Mono, monospace' };
        for (const [k, val] of Object.entries(v)) r.setProperty(k, val);
      });
    });
    await page.exposeFunction('__host', (m) => core.handleMessage(m));
    core.attach({ post: (m) => void page.evaluate((x) => window.dispatchEvent(new MessageEvent('message', { data: x })), m).catch(() => {}) });
    core.start();
    await page.goto(origin + '/');

    const text = (sel) => page.$$eval(sel, (els) => els.map((e) => e.textContent));
    const wait = (fn, arg) => page.waitForFunction(fn, { timeout: 30000 }, arg);
    const send = async (t) => {
      await wait(() => !document.getElementById('input').disabled);
      await page.click('#input');
      await page.keyboard.type(t);
      await page.keyboard.press('Enter');
    };

    // 1. Neue Sitzung, senden, gestreamte Antwort
    await wait(() => /No session open/.test(document.getElementById('banner').textContent));
    await send('hello there');
    await wait(() => document.querySelector('.am strong')?.textContent === 'bold');
    await wait(() => document.querySelectorAll('.tend').length === 1);
    assert.deepEqual(await text('.um .tx'), ['hello there']);
    assert.match((await text('.am'))[0], /Hello bold world/);
    const sid = core.state.session.id;
    assert.ok(sid);

    // 2. Werkzeug mit Rechtefrage
    await send('write the file');
    await wait(() => document.querySelector('.ask .btn'));
    await new Promise((r) => setTimeout(r, 200));
    await page.screenshot({ path: path.join(outDir, 'vscode-e2e-permission.png') });
    await page.click('.abt .btn-pri');
    await wait(() => document.querySelectorAll('.tc[data-s=done]').length >= 1 && document.querySelectorAll('.tend').length === 2);
    assert.equal(await page.$$eval('.ask', (e) => e.length), 0);
    assert.equal(readFileSync(target, 'utf8'), 'print(1)\n');
    assert.match((await text('.am')).join('\n'), /All done/);

    // 3. Werkzeugkarte mit Datei: Diff nur hier
    assert.equal(await page.$$eval('.od', (e) => e.length), 1);
    await (await page.$$('.tgh')).pop().click(); // the calls are folded into one block; open the last
    await wait(() => document.querySelectorAll('.tgb:not([hidden])').length === 1);
    const heads = await page.$$('.tch');
    await heads[heads.length - 1].click();
    assert.deepEqual(await text('.fl'), ['extra.md']);
    await page.click('.od');
    await new Promise((r) => setTimeout(r, 300));
    assert.ok(h.calls.some((c) => c[0] === 'openFile' && c[2] === true && path.basename(String(c[1])) === 'extra.md'), 'Diff wurde nicht angefordert');

    // 4. Abbruch mitten im Stream
    await send('long text');
    await wait(() => /tick3/.test(document.querySelector('.log').textContent));
    await page.click('#send');
    await wait(() => document.querySelector('.tend[data-s=cancelled]'));
    assert.equal(await page.$eval('#send', (e) => e.dataset.mode), 'send');

    // 5. Andere Sitzung, dann die erste erneut oeffnen: Verlauf
    assert.equal(await core.newSession(), true);
    await wait(() => document.querySelectorAll('.um').length === 0);
    assert.notEqual(core.state.session.id, sid);
    assert.equal(await core.openSession(sid), true);
    await wait(() => document.querySelectorAll('.um').length >= 3);
    assert.deepEqual((await text('.um .tx')).slice(0, 3), ['hello there', 'write the file', 'long text']);
    assert.match((await text('.am')).join('\n'), /Hello bold world/);
    assert.ok((await page.$$('.tc')).length >= 1, 'Werkzeugkarte fehlt im Verlauf');
    assert.equal(await page.$eval('#send', (e) => e.dataset.mode), 'send');
    await page.evaluate(() => document.getElementById('scroll').scrollTo(0, 0));
    await new Promise((r) => setTimeout(r, 200));
    await page.screenshot({ path: path.join(outDir, 'vscode-e2e-reopened.png') });

    // 6. Agent- und Modellliste kommen aus settings.get
    await core.newSessionFlow();
    const pick = h.calls.filter((c) => c[0] === 'pickAgentModel').pop();
    assert.ok(pick && pick[1].length > 0 && pick[2].length > 0, `Agent-/Modellliste leer: ${JSON.stringify(pick)}`);

    // 7. Chips gegen das echte Backend: Aufwand der offenen Sitzung wechseln (Schritt 6 liess das Backend ohne Sitzung zurueck)
    assert.equal(await core.openSession(sid), true);
    await page.click('#chips .chip[data-field=effort]');
    await wait(() => !document.getElementById('pop').hidden && document.querySelectorAll('#pop .po').length >= 3);
    await page.click('#pop .po[data-value=high]');
    try {
      await wait(() => document.querySelector('#chips .chip[data-field=effort] .cvl')?.textContent === 'high');
    } catch (e) {
      throw new Error('Chip wechselt nicht: ' + (await page.evaluate(() => JSON.stringify({ chips: [...document.querySelectorAll('#chips .chip')].map((c) => c.textContent), err: [...document.querySelectorAll('.errl')].map((n) => n.textContent), notices: [...document.querySelectorAll('.sysl')].map((n) => n.textContent) }))) + ' status=' + JSON.stringify(core.state.status));
    }
    assert.equal(core.state.status.effort, 'high');

    // 8. Sitzungsliste gegen das echte Backend: unsere Sitzungen mit Herkunft vscode, die offene markiert
    await page.click('#b-list');
    await wait(() => document.querySelectorAll('#v-list .row.sr').length >= 2);
    assert.equal(await page.$$eval('#v-list .row.sr[aria-current]', (e) => e.length), 1);
    assert.ok((await text('#v-list .row.sr .key')).every((k) => /from vscode/.test(k)));
    await new Promise((r) => setTimeout(r, 300));
    await page.screenshot({ path: path.join(outDir, 'plugin2-real-sessions.png') });
    await page.click('#v-list .row.sr:not([aria-current])');
    await wait(() => document.getElementById('sessv').hidden);

    // 9. Einstellungen mit den echten Feldern und Agenten
    await page.click('#b-settings');
    await wait(() => document.querySelectorAll('#s-listsc .row').length >= 8);
    assert.ok((await page.$$('#s-listsc .row.ag')).length >= 1, 'Agenten fehlen');
    assert.match(await page.$eval('#s-sum', (e) => e.textContent), /\S+ \S+/);
    await new Promise((r) => setTimeout(r, 400));
    await page.screenshot({ path: path.join(outDir, 'plugin2-real-settings.png') });
    await page.click('#s-close');
    assert.deepEqual(pageErrors, []);
  } finally {
    await browser.close();
    await core.dispose();
    server.close();
  }
});
