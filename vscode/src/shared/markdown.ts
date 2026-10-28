// Kleiner sicherer Markdown-Parser: liefert einen Baum, keinen HTML-Text.
// Der Baum wird in der Webview nur ueber createElement/textContent gerendert.
import { safeHref } from './protocol.ts';

export type Inline =
  | { t: 'text'; v: string }
  | { t: 'code'; v: string }
  | { t: 'b'; c: Inline[] }
  | { t: 'i'; c: Inline[] }
  | { t: 'a'; href: string; c: Inline[] };

export type Block =
  | { t: 'p'; c: Inline[] }
  | { t: 'h'; level: number; c: Inline[] }
  | { t: 'code'; lang: string; text: string }
  | { t: 'list'; ordered: boolean; items: Block[][] }
  | { t: 'quote'; c: Block[] }
  | { t: 'hr' };

const MAX_DEPTH = 6;

const RE_FENCE = /^ {0,3}(`{3,}|~{3,})\s*([\w+#.-]*)[^`]*$/;
const RE_HEADING = /^ {0,3}(#{1,6})\s+(.*?)(?:\s+#+)?\s*$/;
const RE_HR = /^ {0,3}([-*_])(?: ?\1){2,}\s*$/;
const RE_QUOTE = /^ {0,3}>\s?(.*)$/;
const RE_ITEM = /^( {0,3})([-*+]|\d{1,9}[.)])\s+(.*)$/;

function isBlockStart(line: string): boolean {
  return RE_FENCE.test(line) || RE_HEADING.test(line) || RE_HR.test(line) || RE_QUOTE.test(line) || RE_ITEM.test(line);
}

export function parseBlocks(src: string, depth = 0): Block[] {
  const lines = src.replace(/\r\n?/g, '\n').split('\n');
  const out: Block[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (line.trim() === '') {
      i++;
      continue;
    }
    const fence = RE_FENCE.exec(line);
    if (fence) {
      const mark = fence[1];
      const buf: string[] = [];
      i++;
      while (i < lines.length) {
        const l = lines[i];
        const t = l.trim();
        if (t.length >= mark.length && t[0] === mark[0] && /^(`+|~+)$/.test(t)) {
          i++;
          break;
        }
        buf.push(l);
        i++;
      }
      out.push({ t: 'code', lang: fence[2] || '', text: buf.join('\n') });
      continue;
    }
    const h = RE_HEADING.exec(line);
    if (h) {
      out.push({ t: 'h', level: h[1].length, c: parseInline(h[2]) });
      i++;
      continue;
    }
    if (RE_HR.test(line)) {
      out.push({ t: 'hr' });
      i++;
      continue;
    }
    if (RE_QUOTE.test(line)) {
      const buf: string[] = [];
      while (i < lines.length) {
        const q = RE_QUOTE.exec(lines[i]);
        if (!q) break;
        buf.push(q[1]);
        i++;
      }
      out.push({ t: 'quote', c: depth >= MAX_DEPTH ? [{ t: 'p', c: [{ t: 'text', v: buf.join('\n') }] }] : parseBlocks(buf.join('\n'), depth + 1) });
      continue;
    }
    const item = RE_ITEM.exec(line);
    if (item) {
      const ordered = /^\d/.test(item[2]);
      const items: Block[][] = [];
      while (i < lines.length) {
        const m = RE_ITEM.exec(lines[i]);
        if (!m || /^\d/.test(m[2]) !== ordered || m[1].length > 3) break;
        const content: string[] = [m[3]];
        i++;
        while (i < lines.length) {
          const l = lines[i];
          if (l.trim() === '') {
            // Leerzeile gehoert noch zum Eintrag, wenn eingerueckter Text folgt.
            if (i + 1 < lines.length && /^ {2,}\S/.test(lines[i + 1])) {
              content.push('');
              i++;
              continue;
            }
            break;
          }
          if (/^ {2,}\S/.test(l)) {
            content.push(l.replace(/^ {1,4}/, ''));
            i++;
            continue;
          }
          break;
        }
        items.push(depth >= MAX_DEPTH ? [{ t: 'p', c: [{ t: 'text', v: content.join('\n') }] }] : parseBlocks(content.join('\n'), depth + 1));
        if (i < lines.length && lines[i].trim() === '' && i + 1 < lines.length && RE_ITEM.test(lines[i + 1])) i++;
      }
      out.push({ t: 'list', ordered, items });
      continue;
    }
    const buf: string[] = [line];
    i++;
    while (i < lines.length && lines[i].trim() !== '' && !isBlockStart(lines[i])) {
      buf.push(lines[i]);
      i++;
    }
    out.push({ t: 'p', c: parseInline(buf.join('\n')) });
  }
  return out;
}

function pushText(out: Inline[], v: string): void {
  if (v === '') return;
  const last = out[out.length - 1];
  if (last && last.t === 'text') last.v += v;
  else out.push({ t: 'text', v });
}

const PUNCT = /[\\`*_{}[\]()#+\-.!>~|<]/;

export function parseInline(s: string, depth = 0): Inline[] {
  const out: Inline[] = [];
  if (depth > MAX_DEPTH) {
    pushText(out, s);
    return out;
  }
  let i = 0;
  const n = s.length;
  while (i < n) {
    const ch = s[i];
    if (ch === '\\' && i + 1 < n && PUNCT.test(s[i + 1])) {
      pushText(out, s[i + 1]);
      i += 2;
      continue;
    }
    if (ch === '`') {
      let run = 1;
      while (s[i + run] === '`') run++;
      const fence = '`'.repeat(run);
      const end = s.indexOf(fence, i + run);
      if (end > 0) {
        let v = s.slice(i + run, end);
        if (v.length > 2 && v.startsWith(' ') && v.endsWith(' ')) v = v.slice(1, -1);
        out.push({ t: 'code', v });
        i = end + run;
        continue;
      }
      pushText(out, fence);
      i += run;
      continue;
    }
    if (ch === '[') {
      const close = findClosingBracket(s, i);
      if (close > 0 && s[close + 1] === '(') {
        const pe = s.indexOf(')', close + 2);
        if (pe > 0) {
          let url = s.slice(close + 2, pe).trim();
          const sp = url.search(/\s/);
          if (sp > 0) url = url.slice(0, sp);
          if (url.startsWith('<') && url.endsWith('>')) url = url.slice(1, -1);
          const label = s.slice(i + 1, close);
          const href = safeHref(url);
          if (href) out.push({ t: 'a', href, c: parseInline(label, depth + 1) });
          else {
            for (const x of parseInline(label, depth + 1)) out.push(x);
            pushText(out, ` (${url})`);
          }
          i = pe + 1;
          continue;
        }
      }
      pushText(out, ch);
      i++;
      continue;
    }
    if ((ch === '*' || ch === '_') && i + 1 < n) {
      const double = s[i + 1] === ch;
      const mark = double ? ch + ch : ch;
      const boundaryOk = ch === '*' || i === 0 || !/[A-Za-z0-9]/.test(s[i - 1]);
      const after = s[i + mark.length];
      if (boundaryOk && after !== undefined && !/\s/.test(after)) {
        const end = findClosing(s, i + mark.length, mark);
        if (end > 0) {
          const inner = parseInline(s.slice(i + mark.length, end), depth + 1);
          out.push(double ? { t: 'b', c: inner } : { t: 'i', c: inner });
          i = end + mark.length;
          continue;
        }
      }
    }
    if ((ch === 'h' || ch === 'H') && (i === 0 || /[\s(]/.test(s[i - 1]))) {
      const m = /^https?:\/\/[^\s<>]+/i.exec(s.slice(i, i + 2100));
      if (m) {
        let u = m[0];
        let trail = '';
        while (u.length > 8 && /[.,;:!?)\]'"]$/.test(u)) {
          trail = u[u.length - 1] + trail;
          u = u.slice(0, -1);
        }
        const href = safeHref(u);
        if (href) {
          out.push({ t: 'a', href, c: [{ t: 'text', v: u }] });
          pushText(out, trail);
          i += m[0].length;
          continue;
        }
      }
    }
    pushText(out, ch);
    i++;
  }
  return out;
}

function findClosingBracket(s: string, open: number): number {
  let d = 0;
  for (let i = open; i < s.length; i++) {
    if (s[i] === '\\') {
      i++;
      continue;
    }
    if (s[i] === '[') d++;
    else if (s[i] === ']') {
      d--;
      if (d === 0) return i;
    }
  }
  return -1;
}

function findClosing(s: string, from: number, mark: string): number {
  for (let i = from; i < s.length; i++) {
    if (s[i] === '\\') {
      i++;
      continue;
    }
    if (s[i] === '`') {
      const e = s.indexOf('`', i + 1);
      if (e < 0) return -1;
      i = e;
      continue;
    }
    if (s.startsWith(mark, i) && i > from && !/\s/.test(s[i - 1]) && !(mark[0] === '_' && /[A-Za-z0-9]/.test(s[i + mark.length] ?? ''))) {
      if (mark.length === 1 && s[i + 1] === mark) {
        i++;
        continue;
      }
      return i;
    }
  }
  return -1;
}
