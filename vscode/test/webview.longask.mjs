// A permission request with a huge description must stay usable: the text scrolls inside the box, the buttons stay in view.
// Plays the webview in headless Chrome with a prepared state (no backend).  Skipped without Chrome.
import { test } from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { existsSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { buildHtml } from '../src/ext/html.ts';
import { initialState } from '../src/shared/reducer.ts';

const here = path.dirname(fileURLToPath(import.meta.url));
const media = path.join(here, '..', 'media');
const chrome = [process.env.CHROME, '/usr/bin/google-chrome', '/usr/bin/chromium', '/usr/bin/chromium-browser', 'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe'].find((p) => p && existsSync(p));

test('long permission request: text scrolls, buttons stay visible', { skip: chrome ? false : 'no Chrome found', timeout: 60000 }, async () => {
  const { default: puppeteer } = await import('puppeteer-core');
  const server = http.createServer((req, res) => {
    const u = req.url || '/';
    if (u === '/main.js') return res.writeHead(200, { 'content-type': 'text/javascript' }).end(readFileSync(path.join(media, 'main.js')));
    if (u === '/main.css') return res.writeHead(200, { 'content-type': 'text/css' }).end(readFileSync(path.join(media, 'main.css')));
    res.writeHead(200, { 'content-type': 'text/html' }).end(buildHtml({ nonce: 'n0nce1234', cspSource: origin, scriptUri: `${origin}/main.js`, styleUri: `${origin}/main.css` }));
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await puppeteer.launch({ executablePath: chrome, headless: true, args: ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage'] });
  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 420, height: 700 });
    const sent = [];
    await page.exposeFunction('__host', (m) => sent.push(m));
    await page.evaluateOnNewDocument(() => {
      window.acquireVsCodeApi = () => ({ postMessage: (m) => window.__host(m), getState: () => undefined, setState: () => {} });
    });
    await page.goto(origin + '/');
    const st = initialState();
    st.conn = { status: 'ready', detail: '' };
    st.items = [{ k: 'question', id: 1, question: 'q1', kind: 'permission', text: 'bash: ' + 'x'.repeat(5000), detail: 'cmd ' + 'y'.repeat(5000), choices: ['yes', 'always', 'no'], answered: null }];
    await page.evaluate((s) => window.dispatchEvent(new MessageEvent('message', { data: { type: 'init', state: s } })), st);
    await page.waitForSelector('#asks .ask .btn');
    await new Promise((r) => setTimeout(r, 300));
    const m = await page.evaluate(() => {
      const box = document.querySelector('#asks .ask').getBoundingClientRect();
      const body = document.querySelector('.ask .abody');
      const btns = [...document.querySelectorAll('.ask .abt .btn')].map((b) => b.getBoundingClientRect());
      return {
        vh: innerHeight,
        boxH: box.height,
        boxTop: box.top,
        bodyScrolls: body.scrollHeight > body.clientHeight,
        bodyScrollW: body.scrollWidth - body.clientWidth,
        btnsInBox: btns.every((r) => r.top >= box.top && r.bottom <= box.bottom + 0.5),
        btnsInView: btns.every((r) => r.top >= 0 && r.bottom <= innerHeight && r.left >= 0 && r.right <= innerWidth),
        n: btns.length,
      };
    });
    assert.ok(m.boxH <= m.vh * 0.4 + 2, `box too tall: ${m.boxH} of ${m.vh}`);
    assert.ok(m.boxTop >= 0);
    assert.ok(m.bodyScrolls, 'text should scroll inside the box');
    assert.ok(m.bodyScrollW <= 1, 'no sideways scrolling: long words wrap');
    assert.equal(m.n, 3);
    assert.ok(m.btnsInBox && m.btnsInView, 'buttons must stay visible');
    // The last character of the text is reachable by scrolling the body, and the buttons still answer.
    await page.$eval('.ask .abody', (e) => (e.scrollTop = e.scrollHeight));
    assert.ok(await page.$eval('.ask .abt .btn-pri', (b) => b.getBoundingClientRect().bottom <= innerHeight));
    await page.click('.ask .abt .btn-pri');
    assert.ok(sent.some((x) => x.type === 'answer' && x.question === 'q1' && x.choice === 'yes'));
  } finally {
    await browser.close();
    server.close();
  }
});

test('plain permission request: sentence first, raw command folded, keys y / a / n answer, small viewport keeps the buttons', { skip: chrome ? false : 'no Chrome found', timeout: 60000 }, async () => {
  const { default: puppeteer } = await import('puppeteer-core');
  const server = http.createServer((req, res) => {
    const u = req.url || '/';
    if (u === '/main.js') return res.writeHead(200, { 'content-type': 'text/javascript' }).end(readFileSync(path.join(media, 'main.js')));
    if (u === '/main.css') return res.writeHead(200, { 'content-type': 'text/css' }).end(readFileSync(path.join(media, 'main.css')));
    res.writeHead(200, { 'content-type': 'text/html' }).end(buildHtml({ nonce: 'n0nce1234', cspSource: origin, scriptUri: `${origin}/main.js`, styleUri: `${origin}/main.css` }));
  });
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await puppeteer.launch({ executablePath: chrome, headless: true, args: ['--no-sandbox', '--disable-gpu', '--disable-dev-shm-usage'] });
  try {
    const page = await browser.newPage();
    await page.setViewport({ width: 240, height: 260 });
    const sent = [];
    await page.exposeFunction('__host', (m) => sent.push(m));
    await page.evaluateOnNewDocument(() => {
      window.acquireVsCodeApi = () => ({ postMessage: (m) => window.__host(m), getState: () => undefined, setState: () => {} });
    });
    await page.goto(origin + '/');
    const st = initialState();
    st.conn = { status: 'ready', detail: '' };
    const cmd = Array.from({ length: 100 }, (_, i) => `echo line ${i}`).join('\n');
    st.items = [{ k: 'question', id: 1, question: 'q1', kind: 'permission', text: 'Show the changed files\nProgram: git\nAsked because no rule allows this yet', detail: cmd, choices: ['yes', 'always', 'no'], answered: null }];
    await page.evaluate((s) => window.dispatchEvent(new MessageEvent('message', { data: { type: 'init', state: s } })), st);
    await page.waitForSelector('#asks .ask .btn');
    await new Promise((r) => setTimeout(r, 300));
    const m = await page.evaluate(() => {
      const q = document.querySelector('.ask');
      const btns = [...q.querySelectorAll('.abt .btn')].map((b) => b.getBoundingClientRect());
      return {
        first: q.querySelector('.aq').textContent,
        folded: q.querySelector('details.adet') !== null && !q.querySelector('details.adet').open,
        cmdShown: q.querySelector('.acmd').checkVisibility({ contentVisibilityAuto: true }),
        btnsInView: btns.length === 3 && btns.every((r) => r.top >= 0 && r.bottom <= innerHeight && r.right <= innerWidth),
      };
    });
    assert.equal(m.first, 'Show the changed files');
    assert.ok(m.folded && !m.cmdShown, 'the raw command is folded away ' + JSON.stringify(m));
    assert.ok(m.btnsInView, 'yes / always / no stay in view in a small panel');
    await page.$eval('.ask details.adet summary', (s) => s.click());
    const pre = await page.$eval('.ask .acmd', (e) => e.textContent);
    const last = pre.trimEnd().split('\n').pop();
    assert.ok(pre.includes('echo line 0') && !pre.includes('shortened') && pre.includes('echo line 99'), 'the whole command, its end too: ' + last);
    const open = await page.evaluate(() => [...document.querySelectorAll('.ask .abt .btn')].map((b) => b.getBoundingClientRect())
      .every((r) => r.top >= 0 && r.bottom <= innerHeight));
    assert.ok(open, 'yes / always / no stay in view with the whole command open');
    await page.focus('.ask .abody');
    await page.keyboard.press('y');
    assert.equal(sent.filter((x) => x.type === 'answer').length, 0, 'a key too early or without Enter answers nothing');
    await new Promise((r) => setTimeout(r, 600));
    await page.keyboard.press('y');
    assert.equal(sent.filter((x) => x.type === 'answer').length, 0, 'the letter only picks the button');
    await page.keyboard.press('Enter');
    assert.ok(sent.some((x) => x.type === 'answer' && x.question === 'q1' && x.choice === 'yes'), 'y answers yes');
  } finally {
    await browser.close();
    server.close();
  }
});
