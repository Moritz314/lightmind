// Sitzungsliste in der Webview (Stil N1): Suchfeld, Zeilen mit Name, Agent, Projekt, Herkunft, zuletzt aktiv und Status.
// Klick oeffnet die Sitzung samt Verlauf; eine Sitzung, die in einem anderen Prozess offen ist, ist als "open elsewhere"
// markiert und nur lesbar. Rendert nur per DOM-API (textContent).
import type { NoteLeaf, NotesInfo, SessionInfo } from '../shared/protocol.ts';
import { filterSessions, groupSessions, sessionMeta } from '../shared/views.ts';

type Post = (m: Record<string, unknown>) => void;

function h<K extends keyof HTMLElementTagNameMap>(tag: K, cls?: string, text?: string): HTMLElementTagNameMap[K] {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

export type SessionsView = {
  open(): void;
  close(): void;
  isOpen(): boolean;
  onList(list: SessionInfo[], error: string | undefined, current: string | null, archived: boolean): void;
  onError(text: string): void;
  onNotes(n: NotesInfo): void;
  onNotesError(id: string, text: string): void;
  onNotesSaved(id: string, ok: boolean, title: string, description: string, error: string): void;
  onEnded(id: string, ok: boolean, text: string): void;
  onMoved(id: string, ok: boolean, text: string): void;
};

export function initSessions(post: Post, getCurrent: () => string | null, onVisibility: (open: boolean) => void): SessionsView {
  const $ = (id: string): HTMLElement => document.getElementById(id) as HTMLElement;
  const root = $('sessv');
  const listEl = $('v-list');
  const qEl = $('v-q') as HTMLInputElement;
  let all: SessionInfo[] = [];
  let loaded = false;
  let error = '';
  let current: string | null = null;
  let opening: string | null = null;
  const unfolded = new Set<string>();
  const notesEl = $('v-notes');
  const titleEl = $('v-title');
  let archived = false; // the list shows the archive instead of the recent chats
  const recentBtn = $('v-recent');
  const archiveBtn = $('v-archive');
  let confirmEnd: string | null = null; // the chat whose "End" waits for a yes
  let note = ''; // result line of the last "End" (shown above the list)
  let notesFor: SessionInfo | null = null; // the chat whose notes are shown
  let notes: NotesInfo | null = null;
  let notesMsg = '';
  let notesErr = '';
  let notesLoadErr = '';

  function render(): void {
    listEl.textContent = '';
    if (error) {
      const e = h('div', 'callout bad', error);
      e.setAttribute('role', 'alert');
      listEl.appendChild(e);
    }
    if (!loaded && !error) {
      listEl.appendChild(h('p', 'none', 'Loading...'));
      return;
    }
    const found = filterSessions(all, qEl.value);
    // a search shows the hits flat; without one, runs that another run started are folded under their parent
    const lines = qEl.value.trim() ? found.map((s) => ({ id: s.id, session: s, label: '', depth: 0, kids: 0 })) : groupSessions(all, unfolded);
    if (!found.length) {
      if (loaded && !error) listEl.appendChild(h('p', 'none', all.length ? 'No session matches.' : archived ? 'The archive is empty.' : 'No sessions yet.'));
      if (note) listEl.insertBefore(h('p', 'gf sn', note), listEl.firstChild); // e.g. "restored" after the last archived chat left
      return;
    }
    const now = Date.now();
    const card = h('div', 'card');
    card.setAttribute('role', 'list');
    for (const line of lines) {
      const s = line.session;
      const row = h(s ? 'button' : 'div', 'row sr') as HTMLButtonElement;
      if (s) row.type = 'button';
      row.dataset.id = line.id;
      if (line.depth) {
        row.dataset.depth = String(line.depth);
        row.style.marginLeft = `${Math.min(line.depth, 4) * 18}px`;
      }
      if (s?.readonly) row.dataset.readonly = '1';
      if (s && s.id === current) row.setAttribute('aria-current', 'true');
      const lab = h('span', 'lab');
      if (line.kids) {
        const open = unfolded.has(line.id);
        const tog = h('span', 'tog', open ? '\u25be' : '\u25b8');
        tog.setAttribute('role', 'button');
        tog.tabIndex = 0;
        tog.setAttribute('aria-expanded', String(open));
        tog.setAttribute('aria-label', `${open ? 'Fold' : 'Unfold'} ${line.kids} sub-sessions`);
        const flip = (ev: Event): void => {
          ev.stopPropagation();
          if (unfolded.has(line.id)) unfolded.delete(line.id);
          else unfolded.add(line.id);
          render();
        };
        tog.addEventListener('click', flip);
        tog.addEventListener('keydown', (ev) => {
          if (ev.key === 'Enter' || ev.key === ' ') {
            ev.preventDefault();
            flip(ev);
          }
        });
        lab.appendChild(tog);
      }
      lab.appendChild(h('span', 'name', s ? s.name : line.label));
      lab.appendChild(h('span', 'key', s ? sessionMeta(s, now) : ''));
      row.appendChild(lab);
      if (line.kids) row.appendChild(h('em', 'tag', unfolded.has(line.id) ? `${line.kids} sub` : `+${line.kids}`));
      if (!s) {
        row.addEventListener('click', () => {
          if (unfolded.has(line.id)) unfolded.delete(line.id);
          else unfolded.add(line.id);
          render();
        });
        card.appendChild(row);
        continue;
      }
      if (s.readonly) row.appendChild(h('em', 'tag warn', 'open elsewhere'));
      else if (s.id === current) row.appendChild(h('em', 'tag', 'open here'));
      row.appendChild(h('span', `val st-${s.status}`, s.status));
      row.appendChild(h('i', 'chev'));
      row.addEventListener('click', () => {
        if (opening) return;
        opening = s.id;
        error = '';
        post({ type: 'openSession', id: s.id });
      });
      card.appendChild(row);
      card.appendChild(actions(s));
    }
    listEl.appendChild(card);
    if (note) listEl.insertBefore(h('p', 'gf sn', note), listEl.firstChild);
    if (archived) listEl.appendChild(h('p', 'gf', 'Archived sessions are not in the normal list. Open one to restore it and go on, or use Restore to put it back.'));
    if (found.some((s) => s.readonly)) listEl.appendChild(h('p', 'gf', 'A session marked "open elsewhere" is in use in another window or in the terminal. You can read it here, not write to it.'));
  }

  // ---- per chat: Notes (name, description, to-dos) and End, the same two things the terminal's resume menu has ----
  function act(label: string, title: string, fn: () => void, cls = ''): HTMLElement {
    const b = h('span', `sact ${cls}`.trim(), label);
    b.setAttribute('role', 'button');
    b.tabIndex = 0;
    b.title = title;
    b.addEventListener('click', (ev) => {
      ev.stopPropagation();
      fn();
    });
    b.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter' || ev.key === ' ') {
        ev.preventDefault();
        ev.stopPropagation();
        fn();
      }
    });
    return b;
  }

  function actions(s: SessionInfo): HTMLElement {
    const bar = h('div', 'sra');
    bar.dataset.for = s.id;
    if (archived) {
      bar.appendChild(act('Restore', 'Put this chat back into the list of recent sessions', () => {
        note = 'Restoring...';
        post({ type: 'sessionRestore', id: s.id });
        render();
      }, 'restore'));
      bar.appendChild(act('Notes', 'Name, description and to-dos of this chat', () => openNotes(s)));
      return bar;
    }
    if (confirmEnd === s.id) {
      bar.appendChild(h('span', 'ask', 'End this chat?'));
      bar.appendChild(act('Yes, end', 'End the chat', () => {
        confirmEnd = null;
        note = 'Ending...';
        post({ type: 'sessionEnd', id: s.id });
        render();
      }, 'yes'));
      bar.appendChild(act('No', 'Keep the chat', () => {
        confirmEnd = null;
        render();
      }));
      return bar;
    }
    bar.appendChild(act('Notes', 'Name, description and to-dos of this chat', () => openNotes(s)));
    // a chat that is over, or the one open in this window, cannot be ended from here
    if (s.status !== 'done' && s.status !== 'failed' && s.id !== current) {
      bar.appendChild(act('End', 'End this chat', () => {
        confirmEnd = s.id;
        render();
      }, 'end'));
    }
    return bar;
  }

  function openNotes(s: SessionInfo): void {
    notesFor = s;
    notes = null;
    notesMsg = '';
    notesErr = '';
    notesLoadErr = '';
    listEl.hidden = true;
    qEl.parentElement!.hidden = true;
    notesEl.hidden = false;
    titleEl.textContent = 'Notes';
    renderNotes();
    post({ type: 'notesGet', id: s.id });
  }

  function closeNotes(): void {
    notesFor = null;
    notesEl.hidden = true;
    listEl.hidden = false;
    qEl.parentElement!.hidden = false;
    titleEl.textContent = 'Sessions';
    render();
  }

  function field(label: string, key: 'title' | 'description', value: string, placeholder: string): HTMLElement {
    const wrap = h('label', 'nf');
    wrap.appendChild(h('span', 'nl', label));
    const inp = h('input');
    inp.type = 'text';
    inp.id = `n-${key}`;
    inp.value = value;
    inp.placeholder = placeholder;
    inp.maxLength = 200;
    inp.autocomplete = 'off';
    inp.spellcheck = false;
    inp.setAttribute('aria-label', label);
    inp.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter' && notesFor) {
        ev.preventDefault();
        notesMsg = 'Saving...';
        notesErr = '';
        setStatus();
        post({ type: 'notesName', id: notesFor.id, field: key, text: inp.value });
      } else if (ev.key === 'Escape') {
        ev.stopPropagation();
        ev.preventDefault();
        closeNotes();
      }
    });
    wrap.appendChild(inp);
    return wrap;
  }

  function setStatus(): void {
    const el = notesEl.querySelector('.nst');
    if (!el) return;
    el.textContent = notesErr || notesMsg;
    el.classList.toggle('bad', !!notesErr);
  }

  const MARK: Record<string, string> = { open: '○', doing: '◐', done: '●' };

  function leaf(x: NoteLeaf, indent: boolean): HTMLElement {
    const box = h('div', `nt-item${indent ? ' in' : ''}`);
    if (x.type === 'task') {
      const line = h('div', `nt-line st-${x.state}`);
      line.appendChild(h('span', 'nt-mark', MARK[x.state] ?? MARK.open));
      line.appendChild(h('span', 'nt-title', x.title));
      box.appendChild(line);
      if (x.info) box.appendChild(h('div', 'nt-sub', x.info));
      for (const r of x.remarks) box.appendChild(h('div', 'nt-sub', `${r.by}: ${r.text}`));
    } else {
      const line = h('div', 'nt-line note');
      line.appendChild(h('span', 'nt-mark', '▤'));
      line.appendChild(h('span', 'nt-title', x.title));
      box.appendChild(line);
      if (x.text) box.appendChild(h('div', 'nt-sub', x.text));
    }
    return box;
  }

  function renderNotes(): void {
    notesEl.textContent = '';
    if (!notesFor) return;
    const s = notesFor;
    if (notesLoadErr) {
      const e = h('div', 'callout bad', notesLoadErr);
      e.setAttribute('role', 'alert');
      notesEl.appendChild(e);
      return;
    }
    if (!notes) {
      notesEl.appendChild(h('p', 'none', 'Loading...'));
      return;
    }
    const head = h('div', 'card nh');
    head.appendChild(field('Name', 'title', notes.title, s.name));
    head.appendChild(field('Description', 'description', notes.description, 'what this chat is about'));
    notesEl.appendChild(head);
    const st = h('p', 'gf nst', notesErr || notesMsg || 'Enter saves. Empty = the name is made automatically again. The AI can change both too.');
    st.setAttribute('role', 'status');
    st.classList.toggle('bad', !!notesErr);
    notesEl.appendChild(st);
    if (s.readonly) notesEl.appendChild(h('p', 'gf', 'This chat is open in another window: you can read its to-dos here and change name and description; the to-dos are changed there.'));
    const tree = h('div', 'card ntree');
    if (!notes.items.length) tree.appendChild(h('p', 'none', 'No to-dos or notes in this chat yet.'));
    for (const x of notes.items) {
      if (x.type === 'folder') {
        const done = x.items.filter((i) => i.type === 'task' && i.state === 'done').length;
        const all = x.items.filter((i) => i.type === 'task').length;
        const f = h('div', 'nt-folder');
        f.appendChild(h('span', 'nt-mark', '▾'));
        f.appendChild(h('span', 'nt-title', x.title));
        if (all) f.appendChild(h('span', 'key', `${done}/${all} done`));
        tree.appendChild(f);
        for (const y of x.items) tree.appendChild(leaf(y, true));
      } else tree.appendChild(leaf(x, false));
    }
    notesEl.appendChild(tree);
  }

  // The switch Recent | Archived: the other list is asked for; a list of the wrong kind that arrives late is ignored (onList).
  function showKind(wantArchive: boolean): void {
    if (archived === wantArchive) return;
    archived = wantArchive;
    syncKind();
    note = '';
    confirmEnd = null;
    loaded = false;
    error = '';
    all = [];
    render();
    post({ type: archived ? 'archiveGet' : 'sessionsGet' });
  }

  function syncKind(): void {
    recentBtn.setAttribute('aria-pressed', String(!archived));
    archiveBtn.setAttribute('aria-pressed', String(archived));
  }

  function open(): void {
    root.hidden = false;
    archived = false;
    syncKind();
    onVisibility(true);
    if (notesFor) closeNotes();
    confirmEnd = null;
    note = '';
    loaded = false;
    error = '';
    opening = null;
    current = getCurrent();
    qEl.value = '';
    render();
    post({ type: 'sessionsGet' });
    qEl.focus({ preventScroll: true });
  }
  function close(): void {
    root.hidden = true;
    opening = null;
    if (notesFor) closeNotes();
    onVisibility(false);
  }

  // Back and Escape leave the notes first (to the list), then the list (to the chat).
  function back(): void {
    if (notesFor) closeNotes();
    else close();
  }

  qEl.addEventListener('input', render);
  recentBtn.addEventListener('click', () => showKind(false));
  archiveBtn.addEventListener('click', () => showKind(true));
  $('v-back').addEventListener('click', back);
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !root.hidden) {
      e.preventDefault();
      back();
    }
  });

  return {
    open,
    close,
    isOpen: () => !root.hidden,
    onList(list, err, cur, isArchive) {
      if (isArchive !== archived) return; // the answer to the other switch position
      all = list;
      loaded = true;
      error = err ?? '';
      current = cur;
      render();
    },
    onError(text) {
      opening = null;
      error = text || 'The session could not be opened.';
      render();
    },
    onNotes(n) {
      if (!notesFor || notesFor.id !== n.session) return;
      notes = n;
      notesLoadErr = '';
      renderNotes();
    },
    onNotesError(id, text) {
      if (!notesFor || notesFor.id !== id) return;
      notesLoadErr = text || 'The notes could not be read.';
      renderNotes();
    },
    onNotesSaved(id, ok, title, description, err) {
      if (!notesFor || notesFor.id !== id || !notes) return;
      if (!ok) {
        notesErr = err || 'Could not save.';
        notesMsg = '';
      } else {
        notes = { ...notes, title, description };
        notesErr = '';
        notesMsg = 'Saved.';
        for (const [key, val] of [['title', title], ['description', description]] as const) {
          const inp = notesEl.querySelector<HTMLInputElement>(`#n-${key}`);
          if (inp && document.activeElement !== inp) inp.value = val;
        }
      }
      setStatus();
    },
    onMoved(id, ok, text) {
      note = ok ? `${text || 'restored'}` : `Could not restore the chat: ${text}`;
      if (!root.hidden && !notesFor) render();
    },
    onEnded(id, ok, text) {
      note = ok ? `${text || 'ended'}` : `Could not end the chat: ${text}`;
      if (!root.hidden && !notesFor) render();
    },
  };
}
