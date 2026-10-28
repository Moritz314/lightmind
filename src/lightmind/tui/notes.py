"""To-dos beside (or instead of) the chat: only the look and the keys, the data is `lightmind.todos`.

A small file tree: the root `To-dos` with loose tasks, notes and folders; a folder holds tasks and notes.  Keys with
the tree focused: Ctrl+Up / Ctrl+Down (and plain Up / Down) move the selection, Enter opens and closes a folder, a
task (info, annotations) or a note (text), space moves a task on (open, in progress, done), `t` new task, `n` new
note, `f` new folder (all in the selected folder, else in the root), `a` annotation to a task, `e` edit, Delete removes
(with a question).  A line of one-line input appears for names and texts."""
from __future__ import annotations

import textwrap

from rich.style import Style
from rich.text import Text
from textual import events
from textual.binding import Binding
from textual.containers import Vertical
from textual.message import Message
from textual.widget import Widget
from textual.widgets import Input, Static, TextArea

from .. import todos
from .theme import AMBER, CYAN, DIM, MINT, TXT

SELECTED = Style(color="#ffffff", bgcolor="#3a4a9a")
HELP = "ctrl+↑↓ move · enter open · space state · t task · n note · f folder · a remark · e edit · del delete"
HELP_RO = "read only: this chat is open in another window"
SYMBOL = {"open": ("○", DIM), "doing": ("◐", AMBER), "done": ("●", MINT)}
ROOT = "root"
MULTI = ("note_text", "text", "remark")          # these are asked in the multi-line editor
HELP_EDIT = "{t}: ctrl+s save · enter new line · esc cancel"
NAME, DESC = "@name", "@desc"                      # the two lines at the top of the list: name and description of the chat
PROMPT = {"name": "name of this chat (empty = automatic again)", "desc": "description of this chat (empty = automatic again)",
          "task": "new task", "note": "title of the new note", "note_text": "text of the note ({t})",
          "folder": "name of the new folder", "remark": "annotation for {t}", "title": "title ({t})",
          "text": "text ({t})"}


class Names:
    """Name and description of a chat as the top of the list shows them: `get()` -> (title, description), `set(field, text)`
    -> the line to show (field is "title" or "description"; raises when the chat may not be changed)."""

    def __init__(self, get, set):
        self.get, self.set = get, set


class Prompt(Message):
    """A key of the tree wants a line of text from the user.  `what` is a key of PROMPT, `target` the item it is for
    (or the folder id for new things, "" = root), `value` what the line starts with, `extra` carries the title of a
    new note on its way to the text question."""

    def __init__(self, what: str, target: str, value: str = "", extra: str = ""):
        super().__init__()
        self.what, self.target, self.value, self.extra = what, target, value, extra


class EditArea(TextArea):
    """The multi-line editor for note texts and annotations: Ctrl+S saves, Esc cancels."""
    BINDINGS = [Binding("ctrl+s", "save", "save", show=False, priority=True),
                Binding("escape", "cancel", "cancel", show=False, priority=True)]

    class Saved(Message):
        def __init__(self, text: str):
            super().__init__()
            self.text = text

    def action_cancel(self) -> None:
        self.screen.query_one(NotesPane).escape()

    def action_save(self) -> None:
        self.post_message(self.Saved(self.text))


class TodoView(Widget, can_focus=True):
    """The tree."""
    BINDINGS = [Binding(k, "move(-1)", "up", show=False) for k in ("ctrl+up", "up", "alt+up")] + \
               [Binding(k, "move(1)", "down", show=False) for k in ("ctrl+down", "down", "alt+down")] + \
               [Binding("enter", "fold", "open", show=False), Binding("space", "state", "state", show=False),
                Binding("t", "new('task')", "task", show=False), Binding("n", "new('note')", "note", show=False),
                Binding("f", "new('folder')", "folder", show=False), Binding("a", "annotate", "remark", show=False),
                Binding("e", "edit", "edit", show=False), Binding("delete", "delete", "delete", show=False),
                Binding("y", "yes", "yes", show=False)]

    def __init__(self, store: todos.Store, readonly: bool = False, names: Names | None = None, **kw):
        super().__init__(**kw)
        self.store, self.readonly, self.names = store, readonly, names
        self.seen_names: tuple = ()
        self.cur = ROOT                                 # id of the selected row
        self.open: set[str] = set()                     # tasks and notes that are unfolded
        self.shut: set[str] = set()                     # folders (and the root) that are folded
        self.confirm = ""                               # id waiting for the "delete?" answer
        self.rows: list[tuple[str, str, Text]] = []     # (kind, id, text); id is "" for lines that cannot be selected

    # ------------------------------------------------------------ rows
    def width_now(self) -> int:
        return max(20, self.size.width or 40)

    def _wrap(self, text: str, prefix: str, style: str, first: str = "") -> list[Text]:
        w = max(10, self.width_now() - len(prefix))
        lines: list[str] = []
        for para in text.strip().split("\n"):             # line breaks of the text stay, long lines wrap
            lines += textwrap.wrap(para.rstrip(), w) or [""]
        return [Text(((first or prefix) if i == 0 else prefix) + line, style) for i, line in enumerate(lines or [""])]

    def _item_rows(self, x, indent: str) -> list[tuple[str, str, Text]]:
        head = Text(no_wrap=True, overflow="ellipsis")
        pad = indent + "    "
        if isinstance(x, todos.Task):
            sym, col = SYMBOL[x.state]
            done = x.state == "done"
            head.append(f"{indent}{sym} ", col)
            head.append(x.title, "dim strike" if done else TXT)
            extra = len(x.remarks) + bool(x.info)
            if extra:
                head.append(f"  {'▾' if x.id in self.open else '▸'}{extra}", DIM)
                if any(n.by == todos.OWNER and not n.seen for n in x.remarks):
                    head.append(" *", AMBER)
            rows = [("task", x.id, head)]
            if x.id in self.open:
                if x.info:
                    rows += [("line", "", t) for t in self._wrap(x.info, pad, CYAN, pad + "i ")]
                for n in x.remarks:
                    rows += [("line", "", t) for t in self._wrap(f"{todos.clock(n.ts)}  {n.text}", pad, AMBER, pad + "> ")]
                if not x.info and not x.remarks:
                    rows.append(("line", "", Text(pad + "no info yet", DIM)))
            return rows
        head.append(f"{indent}≡ ", CYAN)
        head.append(x.title, TXT)
        if x.text:
            head.append(f"  {'▾' if x.id in self.open else '▸'}", DIM)
        rows = [("note", x.id, head)]
        if x.id in self.open:
            rows += [("line", "", t) for t in self._wrap(x.text or "(empty)", pad, TXT if x.text else DIM)]
        return rows

    def build(self) -> list[tuple[str, str, Text]]:
        s = self.store
        tasks = [t for t in todos._walk(s.items) if isinstance(t, todos.Task)]
        h = Text(no_wrap=True, overflow="ellipsis")
        h.append("▸ " if ROOT in self.shut else "▾ ", DIM)
        h.append("To-dos", f"bold {TXT}")
        if tasks:
            h.append(f"  {sum(t.state == 'done' for t in tasks)}/{len(tasks)}", DIM)
        rows: list[tuple[str, str, Text]] = [("root", ROOT, h)]
        if self.names is not None:
            self.seen_names = tuple(self.names.get())
            title, desc = self.seen_names
            nm, ds = Text(no_wrap=True, overflow="ellipsis"), Text(no_wrap=True, overflow="ellipsis")
            nm.append(" name         ", DIM)
            nm.append(title or "(automatic: set with the first answer)", f"bold {TXT}" if title else DIM)
            ds.append(" description  ", DIM)
            ds.append(desc or "(automatic)", TXT if desc else DIM)
            rows = [("name", NAME, nm), ("desc", DESC, ds), ("line", "", Text(""))] + rows
        if ROOT in self.shut:
            return rows
        if not s.items:
            rows.append(("line", "", Text("  empty: t adds a task, n a note, f a folder", DIM)))
        for x in s.items:
            if isinstance(x, todos.Folder):
                d, n = x.count()
                fh = Text(no_wrap=True, overflow="ellipsis")
                fh.append("  " + ("▸ " if x.id in self.shut else "▾ "), DIM)
                fh.append(x.title, f"bold {TXT}")
                fh.append(f"  {d}/{n}" if n else "", DIM)
                rows.append(("folder", x.id, fh))
                if x.id not in self.shut:
                    for i in x.items:
                        rows += self._item_rows(i, "    ")
            else:
                rows += self._item_rows(x, "  ")
        return rows

    def selectable(self) -> list[str]:
        return [i for _, i, _ in self.rows if i]

    def refresh_rows(self) -> None:
        self.rows = self.build()
        ids = self.selectable()
        if self.cur not in ids:
            self.cur = ids[0] if ids else ROOT
        self.refresh()

    def render(self) -> Text:
        h = max(1, self.size.height)
        sel = next((i for i, (_, x, _) in enumerate(self.rows) if x and x == self.cur), 0)
        top = max(0, min(sel - h // 2, len(self.rows) - h))
        out = Text(no_wrap=True, overflow="ellipsis")
        for _, ident, t in self.rows[top:top + h]:
            line = t.copy()
            if ident and ident == self.cur and self.has_focus:
                line.stylize(SELECTED)
            out.append_text(line)
            out.append("\n")
        return out

    def on_focus(self) -> None:
        self.refresh()

    def on_blur(self) -> None:
        self.refresh()

    # ------------------------------------------------------------ keys
    def kind_of(self, ident: str) -> str:
        return next((k for k, i, _ in self.rows if i == ident), "")

    def container(self) -> str:
        """The folder a new item goes into: the selected folder, the folder of the selected item, else the root ("")."""
        k = self.kind_of(self.cur)
        if k == "folder":
            return self.cur
        if k in ("task", "note"):
            p = self.store.parent(self.cur)
            return p.id if p else ""
        return ""

    def set_help(self, text: str) -> None:
        try:
            self.screen.query_one("#np-help", Static).update(Text(text, DIM))
        except Exception:
            pass

    def clear_confirm(self) -> None:
        if self.confirm:
            self.confirm = ""
            self.set_help(HELP_RO if self.readonly else HELP)

    def action_move(self, d: int) -> None:
        self.clear_confirm()
        ids = self.selectable()
        if not ids:
            return
        i = ids.index(self.cur) if self.cur in ids else 0
        self.cur = ids[max(0, min(len(ids) - 1, i + d))]
        self.refresh()

    def edit_names(self) -> bool:
        """Enter / e on the name or description line: ask for the new text.  False = the cursor is elsewhere."""
        if self.cur not in (NAME, DESC) or self.names is None:
            return False
        title, desc = self.names.get()
        self.post_message(Prompt("name" if self.cur == NAME else "desc", self.cur, title if self.cur == NAME else desc))
        return True

    def action_fold(self) -> None:
        self.clear_confirm()
        if self.edit_names():
            return
        k = self.kind_of(self.cur)
        if k in ("folder", "root"):
            self.shut.symmetric_difference_update({self.cur})
        elif k in ("task", "note"):
            self.open.symmetric_difference_update({self.cur})
        self.refresh_rows()

    def _edit(self, fn, *a, **kw):
        if self.readonly:
            return self.app.notify("read only: this chat is open in another window", severity="warning")
        try:
            r = fn(*a, **kw)
        except (KeyError, ValueError) as e:
            self.app.notify(str(e.args[0] if e.args else e), severity="warning")
            return None
        self.refresh_rows()
        return r

    def action_state(self) -> None:
        self.clear_confirm()
        if self.kind_of(self.cur) == "task":
            self._edit(self.store.cycle, self.cur)

    def action_new(self, what: str) -> None:
        self.clear_confirm()
        self.post_message(Prompt(what, self.container()))

    def action_annotate(self) -> None:
        self.clear_confirm()
        if self.kind_of(self.cur) == "task":
            self.post_message(Prompt("remark", self.cur))

    def action_edit(self) -> None:
        self.clear_confirm()
        if self.edit_names():
            return
        k = self.kind_of(self.cur)
        if k in ("task", "note", "folder"):
            self.post_message(Prompt("title", self.cur, self.store.find(self.cur).title))

    def action_delete(self) -> None:
        self.clear_confirm()
        k = self.kind_of(self.cur)
        if k not in ("task", "note", "folder") or self.readonly:
            return
        x = self.store.find(self.cur)
        more = f" and its {len(x.items)} items" if k == "folder" and x.items else ""
        self.confirm = self.cur
        self.set_help(f"delete '{todos._one(x.title, 30)}'{more}?  y = yes, any other key = no")

    def action_yes(self) -> None:
        ident, self.confirm = self.confirm, ""
        self.set_help(HELP_RO if self.readonly else HELP)
        if ident:
            ids = self.selectable()
            before = ids[max(0, ids.index(ident) - 1)] if ident in ids else ROOT     # the row above takes over
            if self._edit(self.store.delete, ident) is not None:
                self.cur = before
                self.refresh_rows()

    def on_key(self, e: events.Key) -> None:
        if self.confirm and e.key not in ("y", "delete"):
            self.clear_confirm()                         # any other key answers no (and still does its own job)


class NotesPane(Vertical):
    """The panel: head line, the tree, one input line (names, texts), the key line."""

    def __init__(self, store: todos.Store, readonly: bool = False, names: Names | None = None, **kw):
        super().__init__(**kw)
        self.store, self.readonly, self.names = store, readonly, names
        self.asking: Prompt | None = None

    def compose(self):
        yield Static(id="np-head")
        yield TodoView(self.store, self.readonly, self.names, id="np-todos")
        yield Input(placeholder="", id="np-in")
        yield EditArea("", id="np-edit", soft_wrap=True, show_line_numbers=False, highlight_cursor_line=False,
                       tab_behavior="indent")
        yield Static(Text(HELP_RO if self.readonly else HELP, DIM), id="np-help")

    def on_mount(self) -> None:
        self.store.reload()
        self.query_one("#np-head", Static).update(Text(" To-dos", f"bold {CYAN}"))
        self.query_one("#np-in", Input).display = False
        self.query_one(EditArea).display = False
        self.query_one(TodoView).refresh_rows()
        self.set_interval(0.8, self.poll)

    def poll(self) -> None:
        """The session's tool may have written: show it."""
        view = self.query_one(TodoView)
        if self.store.changed_on_disk():
            self.store.reload()
            view.refresh_rows()
        elif self.names is not None and tuple(self.names.get()) != view.seen_names:
            view.refresh_rows()                          # renamed by the AI, /rename or another process

    # ------------------------------------------------------------ the input line
    def on_prompt(self, e: Prompt) -> None:
        e.stop()
        if self.readonly and e.what not in ("name", "desc"):      # the name goes to the disk even when the chat is held elsewhere
            return self.app.notify("read only: this chat is open in another window", severity="warning")
        self.asking = e
        t = e.extra if e.what == "note_text" else (self.store.find(e.target).title if e.what in ("remark", "title", "text") else "")
        t = todos._text(t, 30)
        label = PROMPT[e.what].format(t=t)
        if e.what in MULTI:
            area = self.query_one(EditArea)
            area.load_text(e.value)
            area.move_cursor(area.document.end)
            area.display = True
            self.query_one("#np-help", Static).update(Text(HELP_EDIT.format(t=label), DIM))
            area.focus()
            return
        inp = self.query_one("#np-in", Input)
        inp.placeholder = label
        inp.value = e.value
        inp.cursor_position = len(e.value)
        inp.display = True
        inp.focus()

    def on_input_submitted(self, e: Input.Submitted) -> None:
        e.stop()
        self.submit(e.value.strip())

    def on_text_area_changed(self, e: TextArea.Changed) -> None:
        e.stop()                                        # not the chat input: no slash popup for the editor

    def on_edit_area_saved(self, e: EditArea.Saved) -> None:
        e.stop()
        self.submit(e.text.strip())

    def submit(self, text: str) -> None:
        ask = self.asking
        self.end_prompt()
        if ask is None or (not text and ask.what not in ("note_text", "name", "desc")):
            return
        view, st = self.query_one(TodoView), self.store
        folder = ask.target or None
        new = None
        if ask.what == "task":
            new = view._edit(st.add_task, text, "", folder)
        elif ask.what == "folder":
            new = view._edit(st.add_folder, text)
        elif ask.what == "note":
            self.post_message(Prompt("note_text", ask.target, extra=text))      # next: its text
        elif ask.what == "note_text":
            new = view._edit(st.add_note, ask.extra, text, folder)
        elif ask.what in ("name", "desc"):
            try:
                self.app.notify(self.names.set("title" if ask.what == "name" else "description", text))
            except Exception as err:                    # noqa: BLE001 - a chat that may not be changed says why
                self.app.notify(str(err) or type(err).__name__, severity="warning")
            view.refresh_rows()
        elif ask.what == "remark":
            view._edit(st.annotate, ask.target, text)
        elif ask.what == "title":
            x = st.find(ask.target)
            view._edit(st.update, ask.target, title=text)
            if isinstance(x, todos.Note):
                self.post_message(Prompt("text", ask.target, x.text))           # a note: the text next
        elif ask.what == "text":
            view._edit(st.update, ask.target, text=text)
        if new is not None:
            view.cur = new.id
            if folder:
                view.shut.discard(folder)
            view.refresh_rows()

    def end_prompt(self) -> None:
        self.asking = None
        inp = self.query_one("#np-in", Input)
        inp.display = False
        self.query_one(EditArea).display = False
        self.query_one("#np-help", Static).update(Text(HELP_RO if self.readonly else HELP, DIM))
        self.query_one(TodoView).focus()

    def escape(self) -> bool:
        """Esc inside the panel: an open input line (or a delete question) closes first.  False = nothing of the panel
        was open (the screen goes on)."""
        if self.asking is not None:
            self.end_prompt()
            return True
        v = self.query_one(TodoView)
        if v.confirm:
            v.clear_confirm()
            return True
        return False

    def focus_main(self) -> None:
        self.query_one(TodoView).focus()

    def shutdown(self) -> None:
        """Nothing is buffered any more: every change is saved at once."""
