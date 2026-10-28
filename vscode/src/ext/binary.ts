import * as fs from 'node:fs';
import * as path from 'node:path';

export type SpawnSpec = { command: string; args: string[]; cwd: string };

export function expandHome(p: string, home: string, mod: typeof path.posix | typeof path.win32 = path): string {
  if (p === '~') return home;
  if (p.startsWith('~/') || p.startsWith('~\\')) return mod.join(home, p.slice(2));
  return p;
}

export function runtimeDirName(platform: string, arch: string): string {
  const os = platform === 'win32' ? 'windows' : platform === 'darwin' ? 'macos' : 'linux';
  const a = arch === 'x64' ? 'x86_64' : arch === 'arm64' ? 'arm64' : arch;
  return `${os}-${a}`;
}

/** Liest den Zeiger auf den neuesten Build (eine Zeile, z. B. "r3"); null, wenn er fehlt oder kein einfacher Name ist. */
export type ReadPointer = (file: string) => string | null;

function readPointerFromDisk(file: string): string | null {
  try {
    return fs.readFileSync(file, 'utf8').trim();
  } catch {
    return null;
  }
}

/**
 * Der Ordner der Runtime, die neue Starts benutzen: system/runtimes/<os>-<arch>/<rN>, auf die der Zeiger
 * system/runtimes/<os>-<arch>.current zeigt; ohne Zeiger die einzelne Runtime frueherer Launcher
 * (system/runtime-<os>-<arch>). So bekommt ein neuer Chat die neue Version, ein offener bleibt auf seiner.
 */
export function runtimeDir(tree: string, platform: string, arch: string, readPointer: ReadPointer = readPointerFromDisk): string {
  const mod = platform === 'win32' ? path.win32 : path.posix;
  const name = runtimeDirName(platform, arch);
  const id = readPointer(mod.join(tree, 'system', 'runtimes', `${name}.current`));
  if (id && /^r[0-9]+$/.test(id)) return mod.join(tree, 'system', 'runtimes', name, id);
  return mod.join(tree, 'system', `runtime-${name}`);
}

/**
 * Baut den Startaufruf des Backends. Direkt auf lm.exe bzw. bin/lm, ohne Shell und ohne .cmd.
 * Auf dem Zielrechner (auch bei Remote-SSH, wo die Erweiterung dort laeuft) zaehlt dessen Betriebssystem.
 * Der Zeiger wird bei jedem Start neu gelesen; ein laufendes Backend bleibt auf seiner Runtime (sie wird erst
 * entfernt, wenn niemand sie mehr benutzt).
 */
export function buildSpawn(tree: string, workdir: string, platform: string, arch: string, readPointer: ReadPointer = readPointerFromDisk): SpawnSpec {
  const mod = platform === 'win32' ? path.win32 : path.posix;
  const dir = runtimeDir(tree, platform, arch, readPointer);
  const exe = platform === 'win32' ? mod.join(dir, 'Scripts', 'lm.exe') : mod.join(dir, 'bin', 'lm');
  return { command: exe, args: ['serve', '--stdio', '--workdir', workdir], cwd: tree };
}

/**
 * Pfad-/Befehlseinstellungen nur aus Benutzer-/Maschineneinstellungen oder Standard lesen.
 * Workspace-Werte (.vscode/settings.json eines fremden Projekts) werden ignoriert.
 */
export function trustedSetting<T>(info: { globalValue?: T; defaultValue?: T; workspaceValue?: T; workspaceFolderValue?: T } | undefined, fallback: T): T {
  return info?.globalValue ?? info?.defaultValue ?? fallback;
}
