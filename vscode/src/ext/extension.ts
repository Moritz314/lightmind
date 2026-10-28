// VS-Code-Schicht: Webview-View, QuickPicks, Dateimarkierungen, Folgemodus, Statusleiste.
import * as vscode from 'vscode';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { randomBytes } from 'node:crypto';
import { Backend } from './backend.ts';
import { buildSpawn, expandHome, trustedSetting } from './binary.ts';
import { ChatCore } from './core.ts';
import type { UiHost } from './core.ts';
import { buildHtml, makeNonce } from './html.ts';
import { changedLines } from '../shared/marks.ts';
import type { MarkSnapshot } from '../shared/marks.ts';
import type { SessionInfo } from '../shared/protocol.ts';

const VIEW_ID = 'lightmind.chat';
const MAX_SNAPSHOT = 1_000_000;

function cfg(): vscode.WorkspaceConfiguration {
  return vscode.workspace.getConfiguration('lightmind');
}

function readHome(): string {
  const raw = trustedSetting(cfg().inspect<string>('home'), '~/lightmind') || '~/lightmind';
  return path.resolve(expandHome(raw, os.homedir()));
}

const keyOf = (p: string): string => (process.platform === 'win32' ? path.resolve(p).toLowerCase() : path.resolve(p));

class MarkDecorations implements vscode.FileDecorationProvider {
  private emitter = new vscode.EventEmitter<vscode.Uri[]>();
  readonly onDidChangeFileDecorations = this.emitter.event;
  private working = new Set<string>();
  private changed = new Set<string>();
  private all = new Map<string, string>();

  update(snap: MarkSnapshot): void {
    const touched = new Set<string>([...this.all.values()]);
    this.working = new Set(snap.working.map(keyOf));
    this.changed = new Set(snap.changed.map(keyOf));
    this.all.clear();
    for (const p of [...snap.working, ...snap.changed]) {
      this.all.set(keyOf(p), p);
      touched.add(p);
    }
    this.emitter.fire([...touched].map((p) => vscode.Uri.file(p)));
  }

  provideFileDecoration(uri: vscode.Uri): vscode.FileDecoration | undefined {
    if (uri.scheme !== 'file') return undefined;
    const k = keyOf(uri.fsPath);
    if (this.working.has(k)) {
      const d = new vscode.FileDecoration('◆', 'Lightmind is working on this file', new vscode.ThemeColor('lightmind.working'));
      d.propagate = true;
      return d;
    }
    if (this.changed.has(k)) {
      const d = new vscode.FileDecoration('LM', 'Changed by Lightmind', new vscode.ThemeColor('lightmind.changed'));
      d.propagate = true;
      return d;
    }
    return undefined;
  }
}

class Host implements UiHost {
  private snapshots = new Map<string, string>();
  private followCol: vscode.ViewColumn | undefined;
  private deco = new MarkDecorations();
  private status: vscode.StatusBarItem;
  private lineDeco = vscode.window.createTextEditorDecorationType({
    isWholeLine: true,
    backgroundColor: new vscode.ThemeColor('lightmind.changedLine'),
  });
  private timers = new Set<ReturnType<typeof setTimeout>>();

  constructor(subs: vscode.Disposable[]) {
    this.status = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Left, 50);
    this.status.command = `${VIEW_ID}.focus`;
    this.status.name = 'Lightmind';
    subs.push(this.status, this.lineDeco, vscode.window.registerFileDecorationProvider(this.deco), { dispose: () => this.timers.forEach(clearTimeout) });
  }

  async pickSession(sessions: SessionInfo[], archived = false): Promise<string | 'new' | 'archive' | undefined> {
    const icon = (s: string): string => (s === 'running' ? '$(sync~spin)' : s === 'paused' ? '$(debug-pause)' : '$(check)');
    type Item = vscode.QuickPickItem & { id: string };
    const items: Item[] = archived ? [] : [{ label: '$(add) New session...', id: 'new', alwaysShow: true }];
    for (const s of sessions) {
      items.push({
        label: `${icon(s.status)} ${s.name}`,
        description: [s.agent, s.model].filter(Boolean).join(' · '),
        detail: [s.origin ? `from ${s.origin}` : '', s.project ? `project ${s.project}` : '', s.status, s.last_active ?? ''].filter(Boolean).join(' · '),
        id: s.id,
      });
    }
    if (!archived) items.push({ label: '$(archive) Archived sessions...', id: 'archive', alwaysShow: true });
    const pick = await vscode.window.showQuickPick(items, {
      title: archived ? 'Lightmind archive' : 'Lightmind sessions',
      placeHolder: archived ? 'Open an archived session (it is restored)' : 'Open a session',
      matchOnDescription: true,
      matchOnDetail: true,
    });
    return pick?.id as string | undefined;
  }

  async pickAgentModel(agents: string[], models: string[]): Promise<{ agent?: string; model?: string } | undefined> {
    const out: { agent?: string; model?: string } = {};
    if (agents.length) {
      const p = await vscode.window.showQuickPick(['Default agent (from config.md)', ...agents], { title: 'New session: agent', placeHolder: 'Agent' });
      if (!p) return undefined;
      if (p !== 'Default agent (from config.md)') out.agent = p;
    }
    if (models.length) {
      const p = await vscode.window.showQuickPick(['Default model', ...models], { title: 'New session: model', placeHolder: 'Model' });
      if (!p) return undefined;
      if (p !== 'Default model') out.model = p;
    }
    return out;
  }

  openSettings(tree: string): void {
    const f = path.join(tree, 'config.md');
    if (!fs.existsSync(f)) {
      void vscode.window.showWarningMessage(`Lightmind: ${f} does not exist. Check the setting "lightmind.home".`);
      return;
    }
    void vscode.window.showTextDocument(vscode.Uri.file(f));
  }

  openFile(p: string, diff: boolean): void {
    const uri = vscode.Uri.file(p);
    if (diff) {
      void Promise.resolve(vscode.commands.executeCommand('git.openChange', uri)).then(undefined, () => vscode.window.showTextDocument(uri));
    } else {
      void vscode.window.showTextDocument(uri);
    }
  }

  openExternal(url: string): void {
    void vscode.env.openExternal(vscode.Uri.parse(url));
  }

  copy(text: string): void {
    void vscode.env.clipboard.writeText(text);
  }

  error(text: string): void {
    void vscode.window.showErrorMessage(`Lightmind: ${text}`);
  }

  marks(snap: MarkSnapshot): void {
    this.deco.update(snap);
    if (snap.working.length) {
      const n = snap.working.length;
      this.status.text = `$(sparkle) Lightmind: editing ${path.basename(snap.working[n - 1])}${n > 1 ? ` +${n - 1}` : ''}`;
      this.status.tooltip = 'Lightmind is working. Click to focus the chat.';
      this.status.show();
    } else {
      this.status.hide();
    }
  }

  followStart(files: string[]): void {
    for (const f of files) {
      try {
        const st = fs.statSync(f);
        this.snapshots.set(f, st.isFile() && st.size <= MAX_SNAPSHOT ? fs.readFileSync(f, 'utf8') : '');
      } catch {
        this.snapshots.set(f, ''); // Datei entsteht erst
      }
    }
    const f = files.find((x) => !this.followSkip(x));
    if (f && fs.existsSync(f)) {
      void this.followShow(f).then(undefined, () => undefined);
    }
  }

  /** Logs and run files change all the time; opening them would only get in the way. */
  private followSkip(f: string): boolean {
    if (!cfg().get<boolean>('followSkipLogs', true)) return false;
    const n = f.replace(/\\/g, '/').toLowerCase();
    return /\/runs\//.test(n) || /\/tool-out\//.test(n) || /\/\.git\//.test(n) || /\.(log|jsonl)$/.test(n) || /\/log\.md$/.test(n);
  }

  /** Shows the file in a second column by default so the user's own editor stays untouched. */
  private async followShow(f: string): Promise<vscode.TextEditor> {
    const uri = vscode.Uri.file(f);
    const key = uri.fsPath.toLowerCase();
    const vis = vscode.window.visibleTextEditors.find((e) => e.document.uri.fsPath.toLowerCase() === key);
    if (vis) return vis;
    if (cfg().get<string>('followWhere', 'side') === 'active') {
      return await vscode.window.showTextDocument(uri, { preview: true, preserveFocus: true, viewColumn: vscode.ViewColumn.Active });
    }
    const act = vscode.window.activeTextEditor?.viewColumn ?? vscode.window.tabGroups.activeTabGroup.viewColumn;
    let col = this.followCol;
    if (!col || col === act || !vscode.window.tabGroups.all.some((g) => g.viewColumn === col)) {
      const other = vscode.window.tabGroups.all.find((g) => g.viewColumn !== act);
      col = other ? other.viewColumn : vscode.ViewColumn.Beside;
    }
    const ed = await vscode.window.showTextDocument(uri, { preview: true, preserveFocus: true, viewColumn: col });
    this.followCol = ed.viewColumn;
    return ed;
  }

  followEnd(files: string[]): void {
    for (const f of files) {
      void this.highlight(f);
    }
  }

  private async highlight(f: string): Promise<void> {
    try {
      await new Promise((r) => setTimeout(r, 250)); // VS Code laedt die Datei von der Platte nach
      const st = fs.statSync(f);
      if (!st.isFile() || st.size > MAX_SNAPSHOT) return;
      const after = fs.readFileSync(f, 'utf8');
      const before = this.snapshots.get(f) ?? '';
      this.snapshots.delete(f);
      const ranges = changedLines(before, after);
      if (!ranges.length || this.followSkip(f)) return;
      const ed = await this.followShow(f);
      const rs = ranges.map((r) => ed.document.validateRange(new vscode.Range(r.start, 0, r.end, 0)));
      ed.setDecorations(this.lineDeco, rs);
      ed.revealRange(rs[0], cfg().get<string>('followWhere', 'side') === 'active' ? vscode.TextEditorRevealType.InCenter : vscode.TextEditorRevealType.InCenterIfOutsideViewport);
      const t = setTimeout(() => {
        this.timers.delete(t);
        ed.setDecorations(this.lineDeco, []);
      }, 3000);
      this.timers.add(t);
    } catch {
      // Folgemodus ist nur Komfort
    }
  }

  setFollow(on: boolean): void {
    void cfg().update('follow', on, vscode.ConfigurationTarget.Global);
  }
}

class Provider implements vscode.WebviewViewProvider {
  core: ChatCore | null = null;
  private view: vscode.WebviewView | null = null;
  private host: Host;
  private out: vscode.OutputChannel;
  private ctx: vscode.ExtensionContext;
  private home = '';

  constructor(ctx: vscode.ExtensionContext, out: vscode.OutputChannel) {
    this.ctx = ctx;
    this.out = out;
    this.host = new Host(ctx.subscriptions);
  }

  private makeCore(): ChatCore {
    this.home = readHome();
    const folder = vscode.workspace.workspaceFolders?.[0]?.uri.fsPath ?? null;
    const home = this.home;
    const blocked = process.env.LM_RUN ? 'Lightmind refuses to start inside a Lightmind run (LM_RUN is set).' : undefined;
    return new ChatCore({
      workdir: folder,
      tree: home,
      blocked,
      follow: cfg().get<boolean>('follow', true),
      ui: this.host,
      log: (s) => this.out.appendLine(s),
      makeBackend: () => new Backend(buildSpawn(home, folder ?? home, process.platform, process.arch)),
    });
  }

  async rebuild(): Promise<void> {
    const old = this.core;
    this.core = null;
    await old?.dispose();
    this.core = this.makeCore();
    if (this.view) {
      this.core.attach({ post: (m) => void this.view?.webview.postMessage(m) });
    }
    this.core.start();
  }

  resolveWebviewView(view: vscode.WebviewView): void {
    this.view = view;
    const media = vscode.Uri.joinPath(this.ctx.extensionUri, 'media');
    view.webview.options = { enableScripts: true, enableCommandUris: false, localResourceRoots: [media] };
    const nonce = makeNonce(randomBytes(16));
    view.webview.html = buildHtml({
      nonce,
      cspSource: view.webview.cspSource,
      scriptUri: view.webview.asWebviewUri(vscode.Uri.joinPath(media, 'main.js')).toString(),
      styleUri: view.webview.asWebviewUri(vscode.Uri.joinPath(media, 'main.css')).toString(),
    });
    let fresh = false;
    if (!this.core) {
      this.core = this.makeCore();
      fresh = true;
    }
    const core = this.core;
    core.attach({ post: (m) => void view.webview.postMessage(m) });
    view.webview.onDidReceiveMessage((m: unknown) => void core.handleMessage(m).catch((e) => this.out.appendLine(`handler error: ${String(e)}`)));
    view.onDidDispose(() => {
      if (this.view === view) {
        this.view = null;
        core.detach();
      }
    });
    if (fresh) core.start();
  }
}

let provider: Provider | null = null;

export function activate(ctx: vscode.ExtensionContext): void {
  const out = vscode.window.createOutputChannel('Lightmind');
  ctx.subscriptions.push(out);
  provider = new Provider(ctx, out);
  ctx.subscriptions.push(vscode.window.registerWebviewViewProvider(VIEW_ID, provider, { webviewOptions: { retainContextWhenHidden: false } }));
  void vscode.commands.executeCommand('setContext', 'lightmind.active', true);

  const withCore = (fn: (c: ChatCore) => unknown) => async (): Promise<void> => {
    if (!provider?.core) {
      await vscode.commands.executeCommand(`${VIEW_ID}.focus`);
    }
    const c = provider?.core;
    if (c) await fn(c);
  };
  const reg = (id: string, fn: () => unknown): void => {
    ctx.subscriptions.push(vscode.commands.registerCommand(id, fn));
  };
  reg('lightmind.newSession', withCore((c) => c.newSessionFlow()));
  reg('lightmind.openSession', withCore((c) => c.showSessions()));
  reg('lightmind.settings', withCore((c) => c.showSettings()));
  reg('lightmind.cancel', withCore((c) => c.handleMessage({ type: 'cancel' })));
  reg('lightmind.restart', withCore((c) => c.restart()));
  reg('lightmind.clearMarks', withCore((c) => c.clearMarks()));
  reg('lightmind.toggleFollow', withCore((c) => c.setFollow(!c.state.follow)));

  ctx.subscriptions.push(
    vscode.workspace.onDidChangeConfiguration((e) => {
      if (e.affectsConfiguration('lightmind.home') && provider?.core) void provider.rebuild();
      if (e.affectsConfiguration('lightmind.follow') && provider?.core) {
        const on = cfg().get<boolean>('follow', true);
        if (on !== provider.core.state.follow) provider.core.dispatch({ kind: 'follow', on });
      }
    }),
  );
}

export async function deactivate(): Promise<void> {
  const c = provider?.core;
  provider = null;
  await c?.dispose();
}
