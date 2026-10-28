// Reine Logik: Pfadpruefung, Zeilenvergleich, Markierungen fuer Dateien, an denen der Agent arbeitet.
import * as nodePath from 'node:path';

type PathMod = typeof nodePath.posix | typeof nodePath.win32;

/** True, wenn file innerhalb eines der Wurzelordner liegt (nur Textvergleich, keine Symlinks). */
export function isInside(file: string, roots: string[], mod: PathMod = nodePath): boolean {
  if (!file || file.includes('\0') || !mod.isAbsolute(file)) return false;
  const win = mod === nodePath.win32 || (mod === nodePath && process.platform === 'win32');
  const norm = (p: string): string => {
    const r = mod.resolve(p);
    return win ? r.toLowerCase() : r;
  };
  const f = norm(file);
  for (const root of roots) {
    if (!root || !mod.isAbsolute(root)) continue;
    const r = norm(root);
    const rel = mod.relative(r, f);
    if (rel === '' || (!rel.startsWith('..') && !mod.isAbsolute(rel))) return true;
  }
  return false;
}

export type LineRange = { start: number; end: number }; // 0-basiert, end inklusive

/** Geaenderte Zeilen im neuen Text (Praefix/Suffix-Vergleich, danach Zeilenabgleich per LCS bei kleinen Dateien). */
export function changedLines(oldText: string, newText: string): LineRange[] {
  if (oldText === newText) return [];
  const a = oldText.split('\n');
  const b = newText.split('\n');
  let p = 0;
  while (p < a.length && p < b.length && a[p] === b[p]) p++;
  let sa = a.length - 1;
  let sb = b.length - 1;
  while (sa >= p && sb >= p && a[sa] === b[sb]) {
    sa--;
    sb--;
  }
  if (sb < p) return []; // nur Loeschungen
  const am = a.slice(p, sa + 1);
  const bm = b.slice(p, sb + 1);
  if (am.length * bm.length > 4_000_000) return [{ start: p, end: sb }];
  // LCS ueber den mittleren Teil
  const w = bm.length + 1;
  const dp = new Uint32Array((am.length + 1) * w);
  for (let i = am.length - 1; i >= 0; i--) {
    for (let j = bm.length - 1; j >= 0; j--) {
      dp[i * w + j] = am[i] === bm[j] ? dp[(i + 1) * w + j + 1] + 1 : Math.max(dp[(i + 1) * w + j], dp[i * w + j + 1]);
    }
  }
  const same = new Array<boolean>(bm.length).fill(false);
  let i = 0;
  let j = 0;
  while (i < am.length && j < bm.length) {
    if (am[i] === bm[j]) {
      same[j] = true;
      i++;
      j++;
    } else if (dp[(i + 1) * w + j] >= dp[i * w + j + 1]) i++;
    else j++;
  }
  const out: LineRange[] = [];
  for (let k = 0; k < bm.length; k++) {
    if (same[k]) continue;
    const line = p + k;
    const last = out[out.length - 1];
    if (last && last.end === line - 1) last.end = line;
    else out.push({ start: line, end: line });
  }
  return out;
}

export type MarkSnapshot = { working: string[]; changed: string[] };

/** Merkt, an welchen Dateien gerade gearbeitet wird (working) und welche geaendert wurden (changed). */
export class MarkTracker {
  private roots: string[] = [];
  private work = new Map<string, Set<string>>(); // pfad -> aufrufe
  private chg = new Set<string>();

  setRoots(roots: string[]): void {
    this.roots = roots.filter(Boolean);
  }

  private ok(p: string): boolean {
    return isInside(p, this.roots);
  }

  start(call: string, files: string[]): string[] {
    const used: string[] = [];
    for (const f of files) {
      if (!this.ok(f)) continue;
      let s = this.work.get(f);
      if (!s) this.work.set(f, (s = new Set()));
      s.add(call);
      used.push(f);
    }
    return used;
  }

  end(call: string, files: string[]): { worked: string[]; changed: string[] } {
    const worked: string[] = [];
    for (const [f, s] of this.work) {
      if (s.delete(call)) worked.push(f);
      if (s.size === 0) this.work.delete(f);
    }
    const changed: string[] = [];
    for (const f of files) {
      if (!this.ok(f)) continue;
      this.chg.add(f);
      changed.push(f);
    }
    return { worked, changed };
  }

  /** Alle laufenden Aufrufe beenden (Rundenende, Absturz). */
  endAll(): void {
    this.work.clear();
  }

  clear(): void {
    this.work.clear();
    this.chg.clear();
  }

  snapshot(): MarkSnapshot {
    return { working: [...this.work.keys()], changed: [...this.chg] };
  }
}
