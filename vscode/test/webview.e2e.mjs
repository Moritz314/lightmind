// Logiktest der Webview-HTML in headless Chrome: gespieltes acquireVsCodeApi, echter Kern, gespieltes Backend (fake-lm.mjs).
// Chrome: Umgebungsvariable CHROME oder ein Standardpfad. Ohne Chrome wird uebersprungen.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { existsSync, mkdirSync, mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { Backend } from '../src/ext/backend.ts';
import { ChatCore } from '../src/ext/core.ts';
import { buildHtml } from '../src/ext/html.ts';
import { makeHost } from './helpers.ts';

const here = path.dirname(fileURLToPath(import.meta.url));
const media = path.join(here, '..', 'media');
const outDir = process.env.E2E_OUT || path.join(here, 'out');
const fake = path.join(here, 'fake-lm.mjs');

const chrome = [process.env.CHROME, '/usr/bin/google-chrome', '/usr/bin/chromium', '/usr/bin/chromium-browser', 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'].find((p) => p && existsSync(p));

test('Webview in headless Chrome: echter Arbeitsablauf', { skip: chrome ? false : 'kein Chrome gefunden', timeout: 120000 }, async () => {
  assert.ok(existsSync(path.join(media, 'main.js')), 'media/main.js fehlt, erst "npm run build"');
  const { default: puppeteer } = await import('puppeteer-core');
  mkdirSync(outDir, { recursive: true });

  const dir = mkdtempSync(path.join(tmpdir(), 'lme2e-'));
  const logFile = path.join(dir, 'req.log');
  const reqs = () => (existsSync(logFile) ? readFileSync(logFile, 'utf8').split('\n').filter(Boolean).map((l) => JSON.parse(l)) : []);

  const server = http.createServer((req, res) => {
    const u = req.url || '/';
    if (u === '/main.js') return res.writeHead(200, { 'content-type': 'text/javascript' }).end(readFileSync(path.join(media, 'main.js')));
    if (u === '/main.css') return res.writeHead(200, { 'content-type': 'text/css' }).end(readFileSync(path.join(media, 'main.css')));
    res.writeHead(200, { 'content-type': 'text/html' }).end(buildHtml({ nonce: 'n0nce1234', cspSource: origin, scriptUri: `${origin}/main.js`, styleUri: `${origin}/main.css` }));
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const origin = `http://127.0.0.1:${server.address().port}`;

  const h = makeHost();
  const core = new ChatCore({
    workdir: dir,
    tree: here,
    follow: true,
    ui: h.ui,
    log: () => {},
    restartDelaysMs: [100, 200, 400],
    shutdownGraceMs: 3000,
    makeBackend: () => new Backend({ command: process.execPath, args: [fake, 'serve', '--stdio', '--workdir', dir], cwd: here }, { ...process.env, FAKE_LM_LOG: logFile }),
  });

  const browser = await puppeteer.launch({ executablePath: chrome, headless: true, args: ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage'] });
  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 420, height: 820, deviceScaleFactor: 2 });
    const consoleMsgs = [];
    const pageErrors = [];
    page.on('console', (m) => consoleMsgs.push(m.text()));
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
        const r = document.documentElement.style; // CSSOM, von der CSP erlaubt
        const v = { '--vscode-sideBar-background': '#181818', '--vscode-editor-background': '#1f1f1f', '--vscode-foreground': '#cccccc', '--vscode-descriptionForeground': '#9d9d9d', '--vscode-input-background': '#313131', '--vscode-widget-border': '#2b2b2b', '--vscode-input-border': '#3c3c3c', '--vscode-editorWidget-background': '#202020', '--vscode-font-family': 'Segoe UI, system-ui, sans-serif', '--vscode-font-size': '13px', '--vscode-editor-font-family': 'Consolas, DejaVu Sans Mono, monospace' };
        for (const [k, val] of Object.entries(v)) r.setProperty(k, val);
      });
    });
    await page.exposeFunction('__host', (m) => core.handleMessage(m));
    core.attach({ post: (m) => void page.evaluate((x) => window.dispatchEvent(new MessageEvent('message', { data: x })), m).catch(() => {}) });
    core.start();
    await page.goto(origin + '/');

    // Zeilen unter der Leiste unten sind fuer die Maus verdeckt: erst in die Mitte rollen, Uebergang abwarten
    const ck = async (sel) => {
      await page.$eval(sel, (e) => e.scrollIntoView({ block: 'center' }));
      await new Promise((r) => setTimeout(r, 380));
      await page.click(sel);
    };
    const text = (sel) => page.$$eval(sel, (els) => els.map((e) => e.textContent));
    const wait = (fn, arg) => page.waitForFunction(fn, { timeout: 15000 }, arg);
    const send = async (t) => {
      await wait(() => !document.getElementById('input').disabled);
      await page.click('#input');
      await page.keyboard.type(t);
      await page.keyboard.press('Enter');
    };
    const idle = () => wait(() => document.getElementById('send').dataset.mode === 'send' && document.querySelector('.tend'));

    // 1. Start: leere Ansicht, keine Sitzung von selbst
    await wait(() => /No session open/.test(document.getElementById('banner').textContent));
    await new Promise((r) => setTimeout(r, 300));
    assert.equal(reqs().some((m) => String(m.type).startsWith('session.')), false, 'Sitzung darf nicht von selbst geoeffnet werden');

    // 1b. Chips ohne Sitzung: Vorgaben aus den Einstellungen, die Wahl gilt fuer die naechste neue Sitzung
    await wait(() => document.querySelector('#chips .chip .cvl')?.textContent === 'sonnet');
    assert.deepEqual(await text('#chips .chip .cvl'), ['sonnet', 'medium', 'auto']);
    await page.click('#chips .chip[data-field=model]');
    await wait(() => !document.getElementById('pop').hidden);
    assert.deepEqual(await text('#pop .po .pv'), ['sonnet', 'haiku', 'opus']);
    await page.click('#pop .po[data-value=haiku]');
    await wait(() => document.querySelector('#chips .chip[data-field=model] .cvl')?.textContent === 'haiku');
    assert.equal(reqs().some((m) => m.type === 'session.set'), false);

    // 2. Senden mit Enter, gestreamte Antwort
    await send('hello there');
    await wait(() => document.querySelector('.am strong')?.textContent === 'bold');
    assert.deepEqual(await text('.um .tx'), ['hello there']);
    await wait(() => document.querySelectorAll('.tend').length === 1);
    assert.match((await text('.tend'))[0], /Done.*\$0\.0123.*context 34%/);
    assert.ok(reqs().some((m) => m.type === 'session.new'));
    assert.equal(await page.$eval('#input', (e) => e.value), '');
    assert.equal(reqs().find((m) => m.type === 'session.new').model, 'haiku', 'Chipwahl geht an die neue Sitzung');
    assert.match(await page.$eval('#statusline', (e) => e.textContent), /project demo/);
    assert.deepEqual(await text('#chips .chip .cvl'), ['haiku', 'high', 'auto']);

    // 3. Shift+Enter = neue Zeile, sendet nicht
    const umBefore = (await text('.um')).length;
    await page.click('#input');
    await page.keyboard.type('a');
    await page.keyboard.down('Shift');
    await page.keyboard.press('Enter');
    await page.keyboard.up('Shift');
    await page.keyboard.type('b');
    assert.equal(await page.$eval('#input', (e) => e.value), 'a\nb');
    assert.equal((await text('.um')).length, umBefore);
    await page.$eval('#input', (e) => {
      e.value = '';
      e.dispatchEvent(new Event('input'));
    });

    // 4. Modelltext wird nie als HTML ausgefuehrt
    await send('xss please');
    await wait(() => document.querySelectorAll('.tend').length === 2);
    assert.equal(await page.$$eval('.am img, .am script', (e) => e.length), 0);
    assert.equal(await page.evaluate(() => window.__pwned), undefined);
    assert.match((await text('.am')).join('\n'), /<img src=x onerror=/);
    assert.equal(await page.$$eval('a.lnk', (e) => e.filter((x) => /^javascript/i.test(x.dataset.url || '')).length), 0);

    // 5. Werkzeugaufruf mit Rechtefrage
    await send('please run it');
    await wait(() => document.querySelector('.ask .btn'));
    assert.match((await text('.acmd'))[0], /pytest -q/);
    // The open request is pinned in its own area right above the input, never inside the message list.
    assert.equal(await page.$$eval('#log .ask', (e) => e.length), 0);
    assert.equal(await page.$$eval('#asks > .ask', (e) => e.length), 1);
    assert.equal(await page.$eval('#asks', (e) => e.nextElementSibling.id !== 'asks' && e.parentElement.classList.contains('dock')), true);
    assert.deepEqual(await text('.abt .btn'), ['Yes', 'Always', 'No']);
    assert.equal(await page.$eval('#send', (e) => e.dataset.mode), 'stop');
    assert.equal(await page.$eval('.tc[data-s=run] .mt', (e) => e.textContent), 'running');
    await new Promise((r) => setTimeout(r, 200));
    await page.screenshot({ path: path.join(outDir, 'chat-permission.png') });
    await page.screenshot({ path: path.join(outDir, 'plugin-chat.png') });
    await page.click('.abt .btn-pri'); // Yes
    await wait(() => document.querySelectorAll('.tc[data-s=done]').length >= 2 && document.querySelectorAll('.tend').length === 3);
    assert.equal(await page.$$eval('.ask', (e) => e.length), 0);
    assert.match((await text('.ans'))[0], /Run this command\?\s+yes/);
    assert.ok(reqs().some((m) => m.type === 'answer' && m.choice === 'yes'));
    // Aufeinanderfolgende Werkzeugaufrufe sind ein Block: eine Zeile in Klartext, standardmaessig zu, Alt+T oeffnet alle
    assert.ok((await page.$$eval('.tg', (e) => e.length)) >= 1);
    assert.match((await text('.tgs'))[0], /^\d+ tool calls?: \d+ /);
    assert.equal(await page.$$eval('.tgb:not([hidden])', (e) => e.length), 0);
    assert.equal(await page.$$eval('.tgh', (e) => e.every((x) => x.getAttribute('aria-expanded') === 'false')), true);
    await page.keyboard.down('Alt');
    await page.keyboard.press('KeyT');
    await page.keyboard.up('Alt');
    await wait(() => document.querySelectorAll('.tgb:not([hidden])').length >= 1);
    await page.keyboard.down('Alt');
    await page.keyboard.press('KeyT');
    await page.keyboard.up('Alt');
    await wait(() => document.querySelectorAll('.tgb:not([hidden])').length === 0);
    await (await page.$$('.tgh')).pop().click(); // a click on a header opens just this block
    await wait(() => document.querySelectorAll('.tgb:not([hidden])').length === 1);
    // Werkzeugkarte aufklappen, Diff oeffnen
    const heads = await page.$$('.tch');
    await heads[heads.length - 1].click();
    await page.click('.od');
    await wait(() => true);
    await new Promise((r) => setTimeout(r, 200));
    assert.ok(h.calls.some((c) => c[0] === 'openFile' && c[2] === true && String(c[1]).endsWith('app.py')), 'Diff wurde nicht angefordert');
    assert.ok(h.calls.some((c) => c[0] === 'followStart'));
    await page.evaluate(() => document.getElementById('scroll').scrollTo(0, 1e6));
    await new Promise((r) => setTimeout(r, 150));
    await page.screenshot({ path: path.join(outDir, 'tool-card.png') });

    // 6. Abbruch mitten im Stream
    await send('long text');
    await wait(() => document.getElementById('send').dataset.mode === 'stop');
    await wait(() => /tick3/.test(document.querySelector('.log').textContent));
    await page.click('#send');
    await wait(() => document.querySelector('.tend[data-s=cancelled]'));
    assert.equal(await page.$eval('#send', (e) => e.dataset.mode), 'send');
    assert.ok(reqs().some((m) => m.type === 'cancel'));

    // 7. Frage der KI mit Vorschlaegen und freier Antwort
    await send('ask me');
    await wait(() => document.querySelector('.ask .free input'));
    assert.deepEqual((await text('.abt .btn')).slice(0, 2), ['sqlite', 'postgres']);
    await page.type('.ask .free input', 'mysql');
    await page.keyboard.press('Enter');
    await wait(() => /mysql/.test(document.querySelector('.log').textContent) && /You picked mysql/.test(document.querySelector('.log').textContent));
    assert.ok(reqs().some((m) => m.type === 'answer' && m.choice === 'mysql'));

    // 8. Folgemodus-Schalter
    await page.click('#b-follow');
    await wait(() => document.getElementById('b-follow').getAttribute('aria-pressed') === 'false');
    assert.ok(h.calls.some((c) => c[0] === 'setFollow' && c[1] === false));

    // 8b. Einstellungen (N1): Kopf mit Orbit-Zeichen, Abschnitte mit Quelle, Zeilen, Fusszeile, Unterseiten
    await page.click('#b-settings');
    try {
      await wait(() => !document.getElementById('sview').hidden && document.querySelectorAll('#s-listsc .row').length >= 8);
    } catch (e) {
      throw new Error('Einstellungen nicht sichtbar: ' + (await page.evaluate(() => JSON.stringify({ hidden: document.getElementById('sview').hidden, view: document.getElementById('app').dataset.view, text: document.getElementById('s-listsc').textContent }))) + ' console=' + consoleMsgs.join('|') + ' errors=' + pageErrors.join('|'));
    }
    assert.equal(await page.$eval('#scroll', (e) => e.offsetParent === null), true, 'Chat muss verdeckt sein');
    assert.equal(await page.$eval('#app', (e) => e.dataset.view), 'settings');
    assert.deepEqual(await text('#s-listsc .gh h2'), ['Defaults', 'Agents', 'Sessions', 'Interface', 'VS Code']);
    assert.deepEqual(await text('#s-listsc .gh code'), ['config.md', 'agents/', 'config.md', 'config.md', 'config.md']);
    assert.equal(await page.$$eval('#s-listsc .gh .g', (e) => e.length), 5, 'Punkt-Symbol je Abschnitt');
    assert.equal(await page.$eval('#s-wm', (e) => e.textContent), 'lightmind');
    assert.equal(await page.$eval('#s-wm', (e) => getComputedStyle(e).textTransform), 'uppercase');
    assert.equal(await page.$eval('.hero .cap', (e) => e.textContent), 'Settings');
    assert.match(await page.$eval('#s-listsc .row[data-key="defaults.effort"]', (e) => e.textContent), /Effort.*defaults.effort.*(medium|high)/);
    assert.match(await page.$eval('#s-listsc .row[data-key="ui.boot_wait"]', (e) => e.textContent), /yes/);
    assert.equal(await page.$eval('#s-listsc .row[data-key="defaults.mode"] .val span', (e) => e.className), 'm-auto');
    // Agenten aus agents/: Name, Projekt, Modell, Aufwand, Modus; geerbte Werte gedimmt
    assert.deepEqual(await text('#s-listsc .row.ag .name'), ['coder', 'writer']);
    assert.match(await page.$eval('#s-listsc .row.ag[data-agent="coder"]', (e) => e.textContent), /project demo.*opus.*high.*auto/);
    assert.equal(await page.$eval('#s-listsc .row.ag[data-agent="coder"] .val .inh', (e) => e.textContent), 'auto');
    assert.match(await page.$eval('#s-listsc .row.ag[data-agent="writer"]', (e) => e.textContent), /project none/);
    // Fusszeile
    assert.equal(await page.$eval('#s-bar .lead', (e) => e.textContent), 'New chat starts with');
    assert.match(await page.$eval('#s-sum', (e) => e.textContent), /claude-abo.*sonnet.*medium.*200k.*auto/);
    assert.equal(await page.$eval('#s-state', (e) => e.textContent), 'saved');
    assert.equal(await page.$eval('#s-bar', (e) => e.hasAttribute('data-dirty')), false);
    // Hintergrund und Zeichen: das Zeichen dreht sich, das Raster reagiert auf die Maus
    assert.ok((await page.$eval('#s-sign', (e) => e.width)) >= 100, 'Zeichen im Kopf ist gross');
    assert.ok((await page.$eval('#s-grid', (e) => e.width)) > 300, 'Raster ist gezeichnet');
    const shot = (sel) => page.$eval(sel, (e) => e.toDataURL());
    await new Promise((r) => setTimeout(r, 450));
    const sign1 = await shot('#s-sign');
    await new Promise((r) => setTimeout(r, 350));
    assert.notEqual(await shot('#s-sign'), sign1, 'Zeichen steht still');
    const grid1 = await shot('#s-grid');
    await page.mouse.move(120, 300);
    await page.mouse.move(210, 330, { steps: 6 });
    await new Promise((r) => setTimeout(r, 250));
    assert.notEqual(await shot('#s-grid'), grid1, 'Raster reagiert nicht auf die Maus');
    await page.screenshot({ path: path.join(outDir, 'plugin2-settings-top.png') });
    // Scrollen: das Zeichen wandert klein in die Titelleiste
    await page.$eval('#s-listsc', (e) => e.scrollTo(0, 130));
    await new Promise((r) => setTimeout(r, 300));
    assert.ok(Number(await page.$eval('#s-list', (e) => e.style.getPropertyValue('--bm'))) > 0.9, 'Schriftzug in der Titelleiste fehlt');
    await page.screenshot({ path: path.join(outDir, 'plugin2-settings-scrolled.png') });
    await page.$eval('#s-listsc', (e) => e.scrollTo(0, 360));
    await new Promise((r) => setTimeout(r, 250));
    await page.screenshot({ path: path.join(outDir, 'plugin2-settings-agents.png') });
    await page.$eval('#s-listsc', (e) => e.scrollTo(0, 0));
    // Agentenseite (nur lesen)
    await ck('#s-listsc .row.ag[data-agent="writer"]');
    await wait(() => document.getElementById('s-sub').dataset.s === 'cur' && document.querySelectorAll('#s-subsc .row.info').length === 5);
    assert.equal(await page.$eval('#s-title', (e) => e.textContent), 'writer');
    assert.match(await page.$eval('#s-subsc', (e) => e.textContent), /uses default/);
    await new Promise((r) => setTimeout(r, 450));
    await page.screenshot({ path: path.join(outDir, 'plugin2-settings-agent.png') });
    await page.click('#s-back');
    await wait(() => document.getElementById('s-list').dataset.s === 'cur');
    // Modusseite: Ring, Beschreibung, Warnung bei bypass
    await ck('#s-listsc .row[data-key="defaults.mode"]');
    await wait(() => document.querySelectorAll('#s-subsc .opt.m-bypass .tag.warn').length === 1);
    assert.deepEqual(await text('#s-subsc .opt .name'), ['auto', 'plan', 'bypass']);
    assert.equal(await page.$$eval('#s-subsc .opt .ring', (e) => e.length), 3);
    assert.equal(await page.$eval('#s-subsc .callout', (e) => e.hasAttribute('data-on')), false);
    await page.click('#s-back');
    await wait(() => document.getElementById('s-list').dataset.s === 'cur');
    // Auswahlseite mit Haken
    await ck('#s-listsc .row[data-key="defaults.effort"]');
    await wait(() => document.getElementById('s-sub').dataset.s === 'cur' && document.querySelectorAll('#s-subsc .opt').length === 3);
    assert.deepEqual(await text('#s-subsc .opt .name'), ['low', 'medium', 'high']);
    assert.deepEqual(await page.$$eval('#s-subsc .opt', (e) => e.map((x) => x.getAttribute('aria-checked'))), ['false', 'true', 'false']);
    assert.equal(await page.$eval('#s-title', (e) => e.textContent), 'Effort');
    await new Promise((r) => setTimeout(r, 450));
    await page.screenshot({ path: path.join(outDir, 'plugin2-settings-sub.png') });
    const sets = () => reqs().filter((m) => m.type === 'settings.set');
    await page.click('#s-subsc .opt:nth-child(3)');
    await wait(() => document.querySelectorAll('#s-subsc .opt')[2]?.getAttribute('aria-checked') === 'true');
    assert.deepEqual(sets().pop().changes, { 'defaults.effort': 'high' });
    await wait(() => document.getElementById('s-toast').dataset.show === '1' && /Saved/.test(document.getElementById('s-toast').textContent));
    await wait(() => document.getElementById('s-state').textContent === 'saved');
    await page.click('#s-back');
    await wait(() => document.getElementById('s-list').dataset.s === 'cur');
    assert.match(await page.$eval('#s-listsc .row[data-key="defaults.effort"]', (e) => e.textContent), /high/);
    // Zahl behaelt ihren Typ
    await ck('#s-listsc .row[data-key="sessions.max_parallel"]');
    await wait(() => document.querySelectorAll('#s-subsc .opt').length === 4);
    await page.click('#s-subsc .opt:nth-child(4)');
    for (let i = 0; i < 100 && !sets().some((m) => m.changes['sessions.max_parallel'] === 8); i++) await new Promise((r) => setTimeout(r, 50));
    assert.ok(sets().some((m) => m.changes['sessions.max_parallel'] === 8), 'Zahl 8 kam nicht an');
    await page.keyboard.press('Escape');
    await wait(() => document.getElementById('s-list').dataset.s === 'cur');
    // Textfeld und Fehlerfall (das Backend lehnt "bogus" ab)
    await ck('#s-listsc .row[data-key="vscode.note"]');
    await wait(() => document.querySelector('#s-subsc input'));
    assert.equal(await page.$eval('#s-subsc input', (e) => e.value), 'hello');
    await page.$eval('#s-subsc input', (e) => e.select());
    await page.keyboard.type('bogus');
    await page.keyboard.press('Enter');
    await wait(() => document.querySelector('#s-subsc .callout.bad'));
    assert.match(await page.$eval('#s-subsc .callout.bad', (e) => e.textContent), /not one of the allowed/);
    assert.equal(await page.$eval('#s-subsc .callout.bad', (e) => e.getAttribute('role')), 'alert');
    assert.equal(sets().pop().changes['vscode.note'], 'bogus');
    await wait(() => document.getElementById('s-state').textContent === 'not saved');
    // Link "Open config.md" oeffnet die Datei, Zurueck fuehrt in die Liste, Close in den Chat
    await page.click('#s-open');
    await new Promise((r) => setTimeout(r, 150));
    assert.ok(h.calls.some((c) => c[0] === 'openSettings'));
    await page.click('#s-back');
    await wait(() => document.getElementById('s-list').dataset.s === 'cur');
    // Ruhig bei prefers-reduced-motion: das Zeichen steht, das Raster reagiert nicht
    await page.emulateMediaFeatures([{ name: 'prefers-reduced-motion', value: 'reduce' }]);
    await new Promise((r) => setTimeout(r, 300));
    const still1 = await shot('#s-sign');
    const gridR = await shot('#s-grid');
    await page.mouse.move(100, 400);
    await page.mouse.move(250, 420, { steps: 5 });
    await new Promise((r) => setTimeout(r, 450));
    assert.equal(await shot('#s-sign'), still1, 'Zeichen bewegt sich trotz reduce');
    assert.equal(await shot('#s-grid'), gridR, 'Raster reagiert trotz reduce');
    await page.emulateMediaFeatures([{ name: 'prefers-reduced-motion', value: 'no-preference' }]);
    await page.click('#s-close');
    await wait(() => document.getElementById('sview').hidden && document.getElementById('scroll').offsetParent !== null);
    assert.equal(await page.$eval('#app', (e) => e.dataset.view), 'chat');
    // Der Befehl "Settings" oeffnet die Ansicht ueber die Erweiterung, Escape schliesst
    core.showSettings();
    await wait(() => !document.getElementById('sview').hidden);
    await page.keyboard.press('Escape');
    await wait(() => document.getElementById('sview').hidden);

    // 8c. Chips unter dem Eingabefeld: Modell, Aufwand, Modus
    assert.deepEqual(await text('#chips .chip .cvl'), ['haiku', 'high', 'auto']);
    // Shift+Tab im Eingabefeld schaltet den Modus durch (auto, plan, bypass, auto), wie in der TUI
    await page.focus('#input');
    for (const next of ['plan', 'bypass', 'auto']) {
      await page.keyboard.down('Shift');
      await page.keyboard.press('Tab');
      await page.keyboard.up('Shift');
      await wait((n) => document.querySelector('#chips .chip[data-field=mode] .cvl')?.textContent === n, next);
    }
    assert.equal(await page.$eval('#pop', (e) => e.hidden), true);
    await page.click('#chips .chip[data-field=effort]');
    await wait(() => !document.getElementById('pop').hidden);
    assert.deepEqual(await text('#pop .po .pv'), ['low', 'medium', 'high']);
    assert.deepEqual(await page.$$eval('#pop .po', (e) => e.map((x) => x.getAttribute('aria-selected'))), ['false', 'false', 'true']);
    assert.equal(await page.$eval('#chips .chip[data-field=effort]', (e) => e.getAttribute('aria-expanded')), 'true');
    assert.equal(await page.evaluate(() => document.activeElement?.classList.contains('po')), true, 'Fokus gehoert in die Liste');
    await page.screenshot({ path: path.join(outDir, 'plugin2-chips-effort.png') });
    await page.keyboard.press('Escape');
    await wait(() => document.getElementById('pop').hidden);
    assert.equal(await page.evaluate(() => document.activeElement?.classList.contains('chip')), true, 'Fokus kehrt zum Chip zurueck');
    await page.click('#chips .chip[data-field=mode]');
    await wait(() => !document.getElementById('pop').hidden);
    assert.deepEqual(await page.$$eval('#pop .po', (e) => e.map((x) => x.className.replace('po ', ''))), ['m-auto', 'm-plan', 'm-bypass']);
    await page.screenshot({ path: path.join(outDir, 'plugin2-chips-mode.png') });
    await page.mouse.click(200, 120); // ausserhalb schliesst
    await wait(() => document.getElementById('pop').hidden);
    await page.click('#chips .chip[data-field=effort]');
    await wait(() => !document.getElementById('pop').hidden);
    await page.keyboard.press('ArrowUp');
    await page.click('#pop .po[data-value=low]');
    await wait(() => document.getElementById('pop').hidden);
    await wait(() => document.querySelector('#chips .chip[data-field=effort] .cvl')?.textContent === 'low');
    const setReq = reqs().filter((m) => m.type === 'session.set').pop();
    assert.equal(setReq.effort, 'low');
    assert.equal('model' in setReq, false);
    assert.match(await page.$eval('#statusline', (e) => e.textContent), /project demo/);
    await page.click('#chips .chip[data-field=model]');
    await wait(() => !document.getElementById('pop').hidden);
    await page.click('#pop .po[data-value=opus]');
    await wait(() => document.querySelector('#chips .chip[data-field=model] .cvl')?.textContent === 'opus');
    assert.equal(reqs().filter((m) => m.type === 'session.set').pop().model, 'opus');

    // 8d. Sitzung fortsetzen: Uhr-Symbol, Liste mit Suche, Oeffnen mit Verlauf, nur lesbare Sitzung
    await page.click('#b-list');
    await wait(() => !document.getElementById('sessv').hidden && document.querySelectorAll('#v-list .row.sr').length >= 3);
    assert.equal(await page.$eval('#app', (e) => e.dataset.view), 'sessions');
    assert.equal(await page.$eval('#scroll', (e) => e.offsetParent === null), true);
    assert.equal(reqs().some((m) => m.type === 'session.list'), true);
    assert.match(await page.$eval('#v-list .row.sr[data-id=old1]', (e) => e.textContent), /Fix login bug.*coder.*project demo.*from tui.*paused/);
    assert.match(await page.$eval('#v-list .row.sr[data-id=old2]', (e) => e.textContent), /Docs pass.*from vscode.*done/);
    assert.equal(await page.$eval('#v-list .row.sr[data-id=old3] .tag.warn', (e) => e.textContent), 'open elsewhere');
    assert.equal(await page.$eval('#v-list .row.sr[data-id=old3]', (e) => e.hasAttribute('data-readonly')), true);
    assert.equal(await page.$eval('#v-list .row.sr[data-id=old1]', (e) => e.hasAttribute('data-readonly')), false);
    assert.equal(await page.evaluate(() => document.activeElement?.id), 'v-q', 'Suchfeld hat den Fokus');
    await new Promise((r) => setTimeout(r, 250));
    await page.screenshot({ path: path.join(outDir, 'plugin2-sessions.png') });
    await page.type('#v-q', 'writer');
    await wait(() => document.querySelectorAll('#v-list .row.sr').length === 2);
    assert.deepEqual(await page.$$eval('#v-list .row.sr', (e) => e.map((x) => x.dataset.id).sort()), ['old2', 'old3']);
    await page.$eval('#v-q', (e) => { e.value = ''; });
    await page.type('#v-q', 'nothing like this');
    await wait(() => /No session matches/.test(document.getElementById('v-list').textContent));
    await page.$eval('#v-q', (e) => { e.value = 'elsewhere'; e.dispatchEvent(new Event('input')); });
    await wait(() => document.querySelectorAll('#v-list .row.sr').length === 1);
    // Escape schliesst, die Liste oeffnet sich erneut leer gesucht
    await page.keyboard.press('Escape');
    await wait(() => document.getElementById('sessv').hidden && document.getElementById('scroll').offsetParent !== null);
    await page.click('#b-list');
    await wait(() => document.querySelectorAll('#v-list .row.sr').length >= 3);
    assert.equal(await page.$eval('#v-q', (e) => e.value), '');
    // 8e. Notizen eines Chats: Name und Beschreibung oben (Enter speichert), darunter der To-do-Baum; Beenden mit Rueckfrage
    assert.equal(await page.$$eval('#v-list .sra[data-for=old3] .sact', (e) => e.length), 2, 'Notes und End je Chat');
    assert.equal(await page.$$eval('#v-list .sra[data-for=old2] .sact', (e) => e.map((x) => x.textContent).join()), 'Notes', 'ein beendeter Chat hat kein End');
    await page.click('#v-list .sra[data-for=old1] .sact:not(.end)');
    await wait(() => !document.getElementById('v-notes').hidden && document.querySelector('#v-notes #n-title'));
    assert.equal(await page.$eval('#v-list', (e) => e.hidden), true);
    assert.equal(await page.$eval('#v-title', (e) => e.textContent), 'Notes');
    assert.equal(await page.$eval('#n-title', (e) => e.value), 'Old name');
    assert.equal(await page.$eval('#n-description', (e) => e.value), 'Old description');
    assert.match(await page.$eval('#v-notes', (e) => e.textContent), /Plan.*first step.*working on it.*user: please hurry.*idea.*a longer text/s);
    await page.screenshot({ path: path.join(outDir, 'plugin2-notes.png') });
    await page.click('#n-title', { clickCount: 3 });
    await page.keyboard.type('Renamed chat');
    await page.keyboard.press('Enter');
    await wait(() => /Saved/.test(document.querySelector('#v-notes .nst')?.textContent ?? ''));
    const nameReq = reqs().filter((m) => m.type === 'notes.name').pop();
    assert.deepEqual([nameReq.session, nameReq.field, nameReq.text], ['old1', 'title', 'Renamed chat']);
    await page.keyboard.press('Escape');                       // Escape in den Notizen: zurueck zur Liste, nicht zum Chat
    await wait(() => !document.getElementById('v-list').hidden && document.getElementById('v-notes').hidden);
    assert.equal(await page.$eval('#sessv', (e) => e.hidden), false);
    assert.equal(await page.$eval('#v-title', (e) => e.textContent), 'Sessions');
    await page.click('#v-list .sra[data-for=old1] .sact.end');
    await wait(() => /End this chat\?/.test(document.querySelector('#v-list .sra[data-for=old1]')?.textContent ?? ''));
    await page.click('#v-list .sra[data-for=old1] .sact:not(.yes):not(.end)');   // No
    await wait(() => document.querySelector('#v-list .sra[data-for=old1] .sact.end'));
    assert.equal(reqs().some((m) => m.type === 'session.end'), false);
    await page.click('#v-list .sra[data-for=old1] .sact.end');
    await wait(() => document.querySelector('#v-list .sra[data-for=old1] .sact.yes'));
    await page.click('#v-list .sra[data-for=old1] .sact.yes');
    await wait(() => document.querySelector('#v-list .row.sr[data-id=old1] .val.st-done'));
    assert.equal(reqs().filter((m) => m.type === 'session.end').pop().session, 'old1');
    assert.match(await page.$eval('#v-list', (e) => e.textContent), /ended/);
    // das Archiv: Umschalter, Restore holt einen Chat in die normale Liste zurueck
    assert.equal(await page.$eval('#v-recent', (e) => e.getAttribute('aria-pressed')), 'true');
    assert.equal(await page.$('#v-list .row.sr[data-id=arc1]'), null, 'die normale Liste zeigt das Archiv nicht');
    await page.click('#v-archive');
    await wait(() => document.querySelectorAll('#v-list .row.sr').length === 2 && document.getElementById('v-archive').getAttribute('aria-pressed') === 'true');
    assert.deepEqual(await page.$$eval('#v-list .row.sr', (e) => e.map((x) => x.dataset.id).sort()), ['arc1', 'arc2']);
    assert.equal(await page.$$eval('#v-list .sra[data-for=arc1] .sact', (e) => e.map((x) => x.textContent).join()), 'Restore,Notes');
    assert.match(await page.$eval('#v-list', (e) => e.textContent), /Archived sessions are not in the normal list/);
    await page.click('#v-list .sra[data-for=arc1] .sact.restore');
    await wait(() => document.querySelectorAll('#v-list .row.sr').length === 1);
    assert.match(await page.$eval('#v-list', (e) => e.textContent), /restored/);
    assert.ok(reqs().some((m) => m.type === 'session.unarchive' && m.session === 'arc1'));
    await page.screenshot({ path: path.join(outDir, 'plugin2-sessions-archive.png') });
    await page.click('#v-recent');
    await wait(() => document.querySelector('#v-list .row.sr[data-id=arc1]'));
    assert.equal(await page.$('#v-list .row.sr[data-id=arc2]'), null, 'was noch im Archiv ist, steht nicht in der normalen Liste');
    // nur lesbare Sitzung oeffnen: Verlauf sichtbar, Eingabe und Chips gesperrt
    await ck('#v-list .row.sr[data-id=old3]');
    await wait(() => document.getElementById('sessv').hidden);
    for (let i = 0; i < 100 && core.state.session?.id !== 'old3'; i++) await new Promise((r) => setTimeout(r, 30));
    assert.equal(core.state.session?.id, 'old3');
    await wait(() => /earlier question/.test(document.querySelector('.log').textContent));
    assert.equal(core.state.session.readonly, true);
    assert.equal(await page.$eval('#input', (e) => e.disabled), true);
    assert.equal(await page.$$eval('#chips .chip:not(:disabled)', (e) => e.length), 0);
    assert.match(await page.$eval('#note', (e) => e.textContent), /Open elsewhere/);
    assert.equal(await page.$eval('#note', (e) => e.hidden), false);
    await page.screenshot({ path: path.join(outDir, 'plugin2-session-readonly.png') });
    // fortsetzen: eine beschreibbare Sitzung aus der Liste
    await page.click('#b-list');
    await wait(() => document.querySelectorAll('#v-list .row.sr').length >= 3);
    assert.equal(await page.$eval('#v-list .row.sr[data-id=old3]', (e) => e.hasAttribute('aria-current')), true, 'offene Sitzung ist markiert');
    await ck('#v-list .row.sr[data-id=old1]');
    await wait(() => document.getElementById('sessv').hidden);
    for (let i = 0; i < 100 && core.state.session?.id !== 'old1'; i++) await new Promise((r) => setTimeout(r, 30));
    assert.equal(core.state.session?.id, 'old1');
    await wait(() => /coder/.test(document.getElementById('meta').textContent) && !document.getElementById('input').disabled);
    assert.equal(await page.$eval('#input', (e) => e.disabled), false);
    assert.equal(await page.$eval('#note', (e) => e.hidden), true);
    assert.equal(await page.$$eval('#chips .chip:not(:disabled)', (e) => e.length), 3);
    assert.ok(reqs().some((m) => m.type === 'session.open' && m.session === 'old1'));
    assert.match(await page.$eval('#meta', (e) => e.textContent), /coder.*from tui.*project demo/);
    // der Befehl "Open session" oeffnet die Liste in der Webview, kein QuickPick
    core.showSessions();
    await wait(() => !document.getElementById('sessv').hidden);
    assert.equal(h.calls.some((c) => c[0] === 'pickSession'), false);
    await page.click('#v-back');
    await wait(() => document.getElementById('sessv').hidden);

    // 9. Backend stuerzt ab: Neustart, Sitzung wird fortgesetzt
    const sid = core.state.session.id;
    await send('crash it');
    await wait(() => /Reconnecting|Lightmind is not running/.test(document.getElementById('banner').textContent) || document.querySelector('.sysl'));
    await wait(() => /earlier question/.test(document.querySelector('.log').textContent) && /resumed/.test(document.querySelector('.log').textContent));
    assert.equal(core.state.session.id, sid);
    assert.equal(await page.$eval('#send', (e) => e.dataset.mode), 'send');
    assert.ok(reqs().some((m) => m.type === 'session.open' && m.session === sid));

    // 9b. Unteragent im Hintergrund: Zeile mit Stopp-Knopf, Ergebnis als eigene Mitteilung (nie "You")
    await send('bgsub please');
    await wait(() => document.querySelectorAll('#subs .subrow .sub[data-s=run]').length === 1);
    assert.equal(await page.$$eval('#subs .substop', (e) => e.length), 1);
    assert.match(await page.$eval('#subs .substop', (e) => e.getAttribute('aria-label')), /scout/);
    await idle();
    await page.click('#subs .substop');
    await wait(() => document.querySelectorAll('#subs .substop').length === 0 && document.querySelector('#subs .sub[data-s=fail]'));
    const cancelReq = reqs().find((m) => m.type === 'subagent.cancel');
    assert.match(cancelReq.sub, /^c\d+$/);
    // zweiter Lauf: fertig, Ergebnis kommt als Mitteilung
    await send('bgsub again');
    await wait(() => document.querySelectorAll('#subs .substop').length === 1);
    const usersBefore = await page.$$eval('.log .um', (e) => e.length);
    await send('bgdone now');
    await wait(() => document.querySelectorAll('.log .submsg').length === 1);
    const msg = await page.$eval('.log .submsg', (e) => ({ open: e.open, head: e.querySelector('summary').textContent, body: e.querySelector('.smb').textContent, html: e.querySelector('.smb').innerHTML }));
    assert.equal(msg.open, false, 'zugeklappt');
    assert.match(msg.head, /^◆scout/);
    assert.match(msg.head, /finished/);
    assert.match(msg.body, /the docs say <b>yes<\/b>/);
    assert.match(msg.body, /Full answer: \/work\/out\/sub-c1\.md/);
    assert.doesNotMatch(msg.html, /<b>yes/, 'Text bleibt Text, kein HTML');
    assert.equal(await page.$$eval('.log .um', (e) => e.length), usersBefore + 1, 'nur der eigene Text "bgdone now" ist You, die Mitteilung nicht');
    assert.equal(await page.$$eval('.log .um .wh', (e) => e.filter((x) => x.closest('.submsg')).length), 0);
    await page.click('.log .submsg summary');
    await wait(() => document.querySelector('.log .submsg').open);
    assert.equal(await page.$eval('.log .submsg .smb', (e) => e.getBoundingClientRect().height > 10), true);
    await page.screenshot({ path: path.join(outDir, 'plugin2-submsg.png') });
    await idle();

    // 10. Fremde Ressourcen und Inline-Skripte sind durch die CSP gesperrt
    const csp = await page.$eval('meta[http-equiv=Content-Security-Policy]', (m) => m.content);
    assert.match(csp, /script-src 'nonce-n0nce1234'/);
    assert.doesNotMatch(csp, /unsafe-inline|unsafe-eval|https?:\/\/(?!127)/);
    assert.equal(consoleMsgs.filter((m) => /Content Security Policy|Refused to/i.test(m)).length, 0, consoleMsgs.join('\n'));
    const inl = await page.evaluate(() => {
      const s = document.createElement('script');
      s.textContent = 'window.__inline = 1';
      document.head.appendChild(s);
      return window.__inline;
    });
    assert.equal(inl, undefined, 'Inline-Skript duerfte nicht laufen');
    assert.deepEqual(pageErrors, []);
  } finally {
    await browser.close();
    await core.dispose();
    server.close();
  }
});
