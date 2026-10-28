// Rendert den Markdown-Baum ausschliesslich ueber createElement/textContent (nie innerHTML).
import { parseBlocks } from '../shared/markdown.ts';
import type { Block, Inline } from '../shared/markdown.ts';

function el<K extends keyof HTMLElementTagNameMap>(tag: K, cls?: string, text?: string): HTMLElementTagNameMap[K] {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

function inline(parent: Node, nodes: Inline[]): void {
  for (const n of nodes) {
    switch (n.t) {
      case 'text':
        parent.appendChild(document.createTextNode(n.v));
        break;
      case 'code':
        parent.appendChild(el('code', undefined, n.v));
        break;
      case 'b': {
        const e = el('strong');
        inline(e, n.c);
        parent.appendChild(e);
        break;
      }
      case 'i': {
        const e = el('em');
        inline(e, n.c);
        parent.appendChild(e);
        break;
      }
      case 'a': {
        // Kein href: die Webview navigiert nie selbst. Klick geht als geprueftes postMessage an die Erweiterung.
        const e = el('a', 'lnk');
        e.setAttribute('role', 'link');
        e.tabIndex = 0;
        e.dataset.url = n.href;
        e.title = n.href;
        inline(e, n.c);
        parent.appendChild(e);
        break;
      }
    }
  }
}

function blocks(parent: Node, bs: Block[], tight = false): void {
  for (const b of bs) {
    switch (b.t) {
      case 'p': {
        if (tight) {
          inline(parent, b.c);
        } else {
          const p = el('p');
          inline(p, b.c);
          parent.appendChild(p);
        }
        break;
      }
      case 'h': {
        const p = el('p', `hd h${Math.min(b.level, 4)}`);
        inline(p, b.c);
        parent.appendChild(p);
        break;
      }
      case 'hr':
        parent.appendChild(el('hr'));
        break;
      case 'quote': {
        const q = el('blockquote');
        blocks(q, b.c);
        parent.appendChild(q);
        break;
      }
      case 'list': {
        const l = el(b.ordered ? 'ol' : 'ul');
        for (const item of b.items) {
          const li = el('li');
          blocks(li, item, item.length === 1 && item[0].t === 'p');
          l.appendChild(li);
        }
        parent.appendChild(l);
        break;
      }
      case 'code': {
        const wrap = el('div', 'cb');
        const head = el('div', 'cbh');
        head.appendChild(el('span', undefined, b.lang || 'text'));
        const cp = el('button', 'cp', 'Copy');
        cp.type = 'button';
        cp.dataset.act = 'copy';
        head.appendChild(cp);
        const pre = el('pre');
        pre.appendChild(el('code', undefined, b.text));
        wrap.appendChild(head);
        wrap.appendChild(pre);
        parent.appendChild(wrap);
        break;
      }
    }
  }
}

export function renderMarkdown(src: string): HTMLElement {
  const root = el('div', 'mk');
  blocks(root, parseBlocks(src));
  return root;
}
