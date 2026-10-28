// HTML der Webview. Rein statisch (kein Modelltext), CSP mit Nonce, nur lokale Ressourcen.

export type HtmlOptions = { nonce: string; cspSource: string; scriptUri: string; styleUri: string };

export function buildCsp(nonce: string, cspSource: string): string {
  return [
    "default-src 'none'",
    `style-src ${cspSource}`,
    `script-src 'nonce-${nonce}'`,
    `img-src ${cspSource} data:`,
    `font-src ${cspSource}`,
  ].join('; ');
}

const ICON_NEW = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M8 3v10M3 8h10" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg>';
const ICON_CLOCK = '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="5.6" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M8 4.7V8l2.3 1.5" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round"/></svg>';
const ICON_SET = '<svg viewBox="0 0 16 16" aria-hidden="true"><circle cx="8" cy="8" r="2.2" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M8 1.8v2M8 12.2v2M1.8 8h2M12.2 8h2M3.6 3.6l1.4 1.4M11 11l1.4 1.4M12.4 3.6L11 5M5 11l-1.4 1.4" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>';
const ICON_EYE = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M1.5 8S4 3.5 8 3.5 14.5 8 14.5 8 12 12.5 8 12.5 1.5 8 1.5 8z" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round"/><circle cx="8" cy="8" r="1.8" fill="currentColor"/></svg>';
const ICON_SEND = '<svg viewBox="0 0 16 16" aria-hidden="true"><path d="M3 8h9M8.5 4L12.5 8l-4 4" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>';

export function buildHtml(o: HtmlOptions): string {
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="${buildCsp(o.nonce, o.cspSource)}">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Lightmind</title>
<link rel="stylesheet" href="${o.styleUri}">
</head>
<body>
<div class="app" id="app" data-view="chat">
  <header class="top">
    <div class="ch1">
      <canvas id="sign" role="img" aria-label="Lightmind orbit mark" width="32" height="32"></canvas>
      <span class="sg" id="sig" data-s="done" aria-hidden="true"></span>
      <span class="sname" id="sname">Lightmind</span>
      <button class="ib" id="b-follow" type="button" title="Follow the agent in the editor" aria-label="Follow the agent in the editor" aria-pressed="true">${ICON_EYE}</button>
      <button class="ib" id="b-new" type="button" title="New session" aria-label="New session">${ICON_NEW}</button>
      <button class="ib" id="b-list" type="button" title="Resume a session" aria-label="Sessions">${ICON_CLOCK}</button>
      <button class="ib" id="b-settings" type="button" title="Settings" aria-label="Settings">${ICON_SET}</button>
    </div>
    <div class="ch2" id="meta"></div>
  </header>
  <main class="scroll" id="scroll">
    <div id="banner"></div>
    <div class="log-wrap"><div class="log" id="log" role="log" aria-live="polite"></div></div>
  </main>
  <footer class="dock">
    <div class="asks" id="asks" aria-live="assertive" hidden></div>
    <div class="nt" id="note" hidden></div>
    <div class="cmp">
      <textarea id="input" rows="1" spellcheck="true" aria-label="Message to Lightmind" placeholder="Ask Lightmind  (Enter sends)"></textarea>
      <div class="cmf"><div class="chips" id="chips" role="group" aria-label="Model, effort and mode"></div><span class="sp"></span>
        <button class="sendb" id="send" type="button" aria-disabled="true" title="Send" aria-label="Send">${ICON_SEND}</button>
      </div>
    </div>
    <div class="sl" id="statusline"></div>
    <div class="subs" id="subs"></div>
    <div class="pop" id="pop" role="listbox" hidden></div>
  </footer>
  <section class="n1 sview" id="sview" hidden aria-label="Settings">
    <canvas id="s-grid" aria-hidden="true"></canvas>
    <div class="stack" id="s-stack">
      <section class="pg home" id="s-list" data-s="cur" role="region" aria-label="Settings">
        <div class="sc" id="s-listsc">
          <header class="hero"><i class="slot0" id="s-slot0" aria-hidden="true"></i><h1 class="wm" id="s-wm" tabindex="-1">lightmind</h1><p class="cap">Settings</p></header>
          <div id="s-groups"></div>
        </div>
        <div class="tb"><div class="tbin hb">
          <span class="brand"><i class="slot1" id="s-slot1" aria-hidden="true"></i><b class="wm bwm" aria-hidden="true">lightmind</b></span>
          <button class="xbtn" id="s-close" type="button">Close</button>
        </div></div>
        <canvas id="s-sign" role="img" aria-label="Lightmind orbit mark"></canvas>
      </section>
      <section class="pg" id="s-sub" data-s="right" aria-hidden="true" role="region" aria-label="Setting">
        <div class="sc" id="s-subsc"></div>
        <div class="tb"><div class="tbin">
          <button class="back" id="s-back" type="button" aria-label="Back"><i class="bch" aria-hidden="true"></i><span id="s-backl">Settings</span></button>
          <h1 class="tt" id="s-title" tabindex="-1"></h1>
          <span></span>
        </div></div>
      </section>
    </div>
    <div class="bar" id="s-bar">
      <div class="sumrow"><span class="lead">New chat starts with</span><span class="bstate" id="s-state" role="status" aria-live="polite">saved</span></div>
      <p class="sv" id="s-sum"></p>
      <div class="acts"><button class="btn" id="s-open" type="button">Open config.md</button></div>
    </div>
    <div class="toast" id="s-toast" role="status" aria-live="polite"></div>
  </section>
  <section class="n1 sessv" id="sessv" hidden aria-label="Sessions">
    <div class="stack">
      <section class="pg" data-s="cur" role="region" aria-label="Sessions">
        <div class="sc" id="v-sc">
          <label class="sq"><input id="v-q" type="search" placeholder="Search sessions" aria-label="Search sessions" autocomplete="off" spellcheck="false"></label>
          <div class="svs" id="v-kind" role="group" aria-label="Which sessions"><button type="button" id="v-recent" aria-pressed="true">Recent</button><button type="button" id="v-archive" aria-pressed="false">Archived</button></div>
          <div id="v-list"></div>
          <div id="v-notes" hidden></div>
        </div>
        <div class="tb"><div class="tbin">
          <button class="back" id="v-back" type="button" aria-label="Back to chat"><i class="bch" aria-hidden="true"></i><span>Chat</span></button>
          <h1 class="tt" id="v-title" tabindex="-1">Sessions</h1>
          <span></span>
        </div></div>
      </section>
    </div>
  </section>
</div>
<script nonce="${o.nonce}" src="${o.scriptUri}"></script>
</body>
</html>`;
}

export function makeNonce(bytes: Uint8Array): string {
  let s = '';
  for (const b of bytes) s += b.toString(16).padStart(2, '0');
  return s;
}
