"""The chat screen: transcript, slash commands, the status line, and the turn that runs in its own task.

Esc during a turn cancels that task.  The cancellation travels down through Session.send, the loop and the
provider, whose `finally` blocks end the CLI process (2 to 3 s); the task catches it, so the UI stays alive
and says "stopping" meanwhile.  All of the generator's steps and its closing happen in that one task, which
is what the SDK's task groups need.

With `/rc` the chat opens a Remote Control link (rc.py): what happens here is mirrored to it, and what comes in from it
(a message, an answer, a stop) goes through the same code as the keyboard (`submit_line`), on this loop, one at a time."""
from __future__ import annotations

import asyncio
import re
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path

from rich.cells import cell_len
from rich.markdown import CodeBlock, Heading, Markdown, Paragraph, TableDataElement
from rich.segment import Segment
from rich.style import Style
from rich.syntax import Syntax
from rich.table import Table
from rich.text import Text
from textual import events
from textual.actions import SkipAction
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.strip import Strip
from textual.widget import Widget
from textual.widgets import Input, Static, TextArea

from .. import core
from .. import interject as ij
from .. import protocol
from .. import rc
from .. import resume
from .. import runs
from .. import termtitle
from .. import todos
from .. import ultracode
from .. import offer, upload
from .. import model as m
from .. import slash as sl
from ..context import skill_message
from ..providers.base import Interjected, Notice, SubagentEnd, SubagentStart, ToolDone, WorkflowEvent
from . import acts, data, toolgroup
from .chatinput import ChatInput
from .notes import Names, NotesPane
from .statusbar import StatusBar
from .subs import SubBar, SubScreen, one_line
from .screens import AccountsScreen, DashScreen, Pick, ResumeScreen, SetupScreen
from .theme import AMBER, BG, CYAN, DIM, MAG, MINT, NEUTRAL, PANEL, TXT, VIOLET, WORD, keyword_on, word_spans

SPIN = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
NOTICE = {"hook": (CYAN, "⌁"), "warning": (AMBER, "!"), "switch": (MAG, "⇄"), "handover": (MAG, "⇄"),
          "limit": (AMBER, "!"), "note": (DIM, "·"), "error": (MAG, "✗")}
BUILTIN = tuple(n for n, _ in data.BUILTIN_SLASH)
NOTES_SPLIT_MIN = 120                      # from this many columns `ui.notes_layout: split` shows the chat and Notes & To-dos side by side
NOTES_WIDTH = (40, 56)                     # the panel beside the chat: a third of the width, between these
LIMITS_EVERY = 30.0                        # seconds between reads of the account's 5h / 7d state file
KEYS = ""          # no key hint line under the chat
READONLY = "read only: this chat is open in another window (process {pid}), nothing can be sent or changed here"
READONLY_OK = ("quit", "accounts", "settings", "config", "archive")      # the built-in commands that change nothing in the chat (`archive` says by itself that a read only chat is not archived)
ARCHIVE_ARG = re.compile(r"(?:^|\s)/archive[ \t]+(?:(restore)[ \t]+)?(\S*)$", re.IGNORECASE)     # the word being typed after `/archive ` (and after `/archive restore `)
ARCHIVE_SUBS = (("list", "show the archived chats"), ("restore", "bring an archived chat back into the resume list"))
ULTRACODE_SUBS = (("on", "effort max + every substantial task as a workflow, for this chat"),
                  ("off", "back to the effort the chat had before"), ("status", "the state and the settings that apply"))
SELECTED = Style(color="#ffffff", bgcolor="#3a4a9a")
POPUP_ROWS = 12
BYPASS_CONFIRM = 2.0                                     # seconds: the second Shift+Tab that really switches to bypass must come within
SUB_TOOLS = ("Task", "Agent")                            # the CLI's tools that start a sub-agent
COL_MIN, COL_MAX, ARGS_MAX = 3, 12, 1500                 # column 1: width of "You" up to 12 characters; a tool line shows up to 1500 of its argument
SEGMENTS = {"model": "model", "mode": "mode", "account": "account", "effort": "effort",
            "ctx_bar": "context", "ctx": "context", "ctx_used": "context", "ctx_limit": "context"}      # status placeholder -> its segment
PRIMARY = ("command", "path", "name", "question", "action", "ref")      # the argument worth showing first
RC_WORD = re.compile(r"/(rc|remote-control)(?:\s+(.*))?", re.IGNORECASE | re.DOTALL)       # `/rc <name>`: the rest of the line is the name


class _Mark:
    """Mixin for the markdown elements that collect running text: the word `ultracode` gets its colour (WORD) as the text
    arrives.  Plain text only: inline code stays as it is, and a code block is another element.  Only styles are added."""
    field = "text"                                               # the Text the element collects into (a table cell: `content`)
    mark = True                                                  # False: `workflow.keyword` is off, no colour

    @classmethod
    def create(cls, markdown, token):
        element = super().create(markdown, token)
        element.mark = getattr(markdown, "mark", True)
        return element

    def on_text(self, context, text):
        buf = getattr(self, self.field, None)
        start = len(buf) if buf is not None else 0
        super().on_text(context, text)
        if not (self.mark and isinstance(text, str) and "ultracode" in text.lower()):
            return                                               # the usual chunk: nothing to look for
        code = context.console.get_style("markdown.code", default="none")
        now = context.current_style
        if code and now + code == now:                           # the chunk carries the inline-code style already
            return
        buf = getattr(self, self.field)
        base = max(0, start - 1)                                 # search the new chunk plus one char of look-behind only: a whole
        for a, b in ultracode.spans(buf.plain[base:]):           # long paragraph per chunk would cost quadratic time
            if a + base >= start:                                # only the word of this chunk (earlier chunks are done)
                buf.stylize(WORD, a + base, b + base)


class _Head(_Mark, Heading):
    LEVEL_ALIGN: dict = {}                                       # left-aligned headings, no boxes


class _Para(_Mark, Paragraph):
    """A paragraph; list items and quotes hold paragraphs too."""


class _Cell(_Mark, TableDataElement):
    field = "content"


class _Code(CodeBlock):
    def __rich_console__(self, console, options):
        yield Syntax(str(self.text).rstrip(), self.lexer_name, theme=self.theme, word_wrap=True, padding=1, background_color=PANEL)


class Md(Markdown):
    elements = {**Markdown.elements, "heading_open": _Head, "paragraph_open": _Para, "td_open": _Cell, "th_open": _Cell,
                "fence": _Code, "code_block": _Code}

    def __init__(self, markup: str, mark: bool = True, **kwargs):
        super().__init__(markup, **kwargs)
        self.mark = mark                                         # colour the word `ultracode` (off with `workflow.keyword: false`)


def short_args(args: dict) -> str:
    """What a tool line shows after its name: the main argument whole (it wraps under column 3), the others cut short."""
    if not args:
        return ""
    key = next((k for k in PRIMARY if k in args), next(iter(args)))
    main = str(args[key]).strip()
    main = main[:ARGS_MAX - 1] + "…" if len(main) > ARGS_MAX else main
    rest = [f"{k}={' '.join(str(v).split())[:30]}" for k, v in args.items() if k != key]
    return " ".join([main, *rest]).strip()


def human(n: int) -> str:
    return f"{n / 1e6:g}m" if n >= 1_000_000 else f"{n / 1000:.0f}k" if n >= 1000 else str(n)


def speaker_name(name) -> str:
    """The name in column 1: one line, at most COL_MAX characters (longer ones end in an ellipsis)."""
    name = data.clean(" ".join(str(name or "").split()))
    return (name if len(name) <= COL_MAX else name[:COL_MAX - 1] + "…") or "lm"


def turn_line(seconds: float, cost: float, est: bool, pct: float) -> str:
    """The pale line at the end of a turn: how long it took, what it cost (left out when nothing was charged), the context."""
    s = round(seconds)
    parts = [f"{s} s" if s < 60 else f"{s // 60} m {s % 60:02d} s"]
    if cost > 0:
        parts.append((f"${cost:.2f}" if cost >= 0.01 else f"${cost:.3f}") + ("~" if est else ""))
    parts.append(f"{pct:.0f} %")
    return " · ".join(parts)


def origin(ev) -> str:
    """The sub-agent an event comes from: the `agent` field of the event (none of today's events carries one yet)."""
    name = getattr(ev, "agent", "")
    return speaker_name(name) if name and isinstance(name, str) else ""


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - cell_len(text))


def _row(width: int, first, body) -> Table:
    """One block of the transcript: column 1 (the speaker and ' > ', `width` + 3 cells, nothing else ever) and column 2."""
    g = Table.grid(padding=0)
    g.add_column(width=width + 3, no_wrap=True)
    g.add_column(ratio=1)
    g.add_row(first, body)
    return g


OFFER_ROOT: list[Path] = []                                 # the tree root, set by the chat: `[[file: ...]]` lines become download links


def agent_grid(text: str, width: int = COL_MIN, speaker: str = "lm", mark: bool = True) -> Table:
    if OFFER_ROOT:
        text = offer.linkify(text, OFFER_ROOT[0])
    return _row(width, Text(_pad(speaker, width) + " > ", f"bold {CYAN}"), Md(text, mark))


def tool_grid(width: int, speaker: str, name: str, args: str, result: tuple[bool, str] | None) -> Table:
    """`[lm]` in column 1, the tool name and ' > ' in column 2, the arguments and the result from column 3 on."""
    inner = Table.grid(padding=0)
    inner.add_column(no_wrap=True)
    inner.add_column(ratio=1)
    inner.add_row(Text(name + " > ", f"bold {AMBER}"), Text(args, DIM))
    if result is not None:
        t = Text()
        t.append("✓ " if result[0] else "✗ ", MINT if result[0] else MAG)
        t.append(result[1], DIM)
        inner.add_row("", t)
    return _row(width, Text(f"[{speaker}]", DIM), inner)


def system_clip(text: str) -> bool:
    """Windows fallback for terminals without OSC 52: `clip.exe` (UTF-16 with a BOM keeps every letter)."""
    if sys.platform != "win32":
        return False
    try:
        subprocess.run(["clip.exe"], input=(chr(0xFEFF) + text).encode("utf-16-le"), check=True, timeout=5,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:                                        # noqa: BLE001 - OSC 52 may still have worked
        return False


class Msgs(VerticalScroll, can_focus=False):
    """The scroll box of the transcript; it never takes the focus from the input (a click starts a selection)."""


class Msg(Static):
    """One block of the transcript; `plain` is its text.  Its Rich renderable (a grid, markdown) is no Textual
    Content, so the built-in selection finds no text in it: selected text and highlight are made here, from the
    rendered lines."""

    def _rows(self) -> list[str]:
        return [Widget.render_line(self, y).text.rstrip() for y in range(self.size.height)]

    def _fill(self) -> tuple[int, int]:
        """(cells of indent filler at the start of a line, first line it starts on): column 1 is empty on every line
        of a notice and on the follow-up lines of the rest, a tool's arguments are indented up to column 3."""
        w = self.width + 3
        if self.kind in ("user", "agent"):
            return w, 1
        if self.kind == "tool":
            return w + cell_len(self.text) + 3, 1
        return w, 0

    def get_selection(self, selection):
        rows = self._rows()
        n, first = self._fill()
        marked = [("\x00" * n + r[n:]) if y >= first and r[:n].isspace() and len(r) > n - 1 else r for y, r in enumerate(rows)]
        if self.kind == "stats":                              # right-aligned: all the blanks in front are filler
            marked = ["\x00" * (len(r) - len(r.lstrip(" "))) + r.lstrip(" ") for r in rows]
        if not any(rows):
            return None
        return selection.extract(chr(10).join(marked)).replace("\x00", ""), chr(10)       # the filler is cut out, offsets stay right

    def render_line(self, y: int) -> Strip:
        strip = super().render_line(y)
        sel = self.text_selection
        span = sel.get_span(y) if sel is not None else None
        if span is not None:
            a, b = span[0], (strip.cell_length if span[1] < 0 else span[1])
            if b > a:
                mid = Strip(Segment.apply_style(list(strip.crop(a, b)), post_style=SELECTED))
                strip = Strip.join([strip.crop(0, a), mid, strip.crop(b)])
        return strip.apply_offsets(0, y)                 # the position of every character: what the mouse selection works from

    def __init__(self, kind: str, speaker: str, text: str, extra: str = "", width: int = COL_MIN):
        self.kind, self.speaker, self.text, self.extra, self.width = kind, speaker, text, extra, width
        self.result: tuple[bool, str] | None = None
        self.queued = False                                  # a user block that waits for the running turn to end
        self.tag = ""                                        # an interjection: "waiting for the next step" / "read by the agent"
        self.group: ToolHead | None = None                   # a tool line: the collapsed block it belongs to
        self.pending = False                                 # a tool line whose result has not come yet
        self.target = ""                                     # a tool line: the file it works on (the running step names it)
        super().__init__(self.build(), classes="m")

    def build(self):
        if self.kind == "user":
            label = "queued" if self.queued else self.tag
            body = Text(self.text, DIM if self.queued else TXT)
            for start, end in word_spans(self, self.text):
                body.stylize(WORD, start, end)
            if label:
                body.append("  " + label, f"italic {DIM}")
            return _row(self.width, Text(_pad(self.speaker, self.width) + " > ", f"bold {VIOLET}"), body)
        if self.kind == "agent":
            return agent_grid(self.text, self.width, self.speaker, "ultracode" in self.text.lower() and keyword_on(self))
        if self.kind == "tool":
            return tool_grid(self.width, self.speaker, self.text, self.extra, self.result)
        if self.kind == "stats":
            g = Table.grid(padding=0, expand=True)                  # the whole width, so the text can sit at its right edge
            g.add_column(width=self.width + 3)
            g.add_column(ratio=1, justify="right")
            g.add_row("", Text(self.text, DIM))
            return g
        col, sym = NOTICE.get(self.kind, NOTICE["note"])
        return _row(self.width, "", Text(f"{sym} {self.text}", col))

    @property
    def plain(self) -> str:
        """The words of the block without any column filler."""
        if self.kind == "tool":
            r = f"  {'✓' if self.result[0] else '✗'} {self.result[1]}" if self.result else ""
            return f"{self.text} > {self.extra}{r}"
        if self.kind in ("user", "agent", "stats", "tools"):
            return self.text
        return f"{NOTICE.get(self.kind, NOTICE['note'])[1]} {self.text}"

    def draw(self):
        self.update(self.build())

    def set_width(self, width: int):
        if width != self.width:
            self.width = width
            self.draw()

    def set_agent(self, text: str):
        """Re-render an agent message whose text has grown (streamed deltas)."""
        self.text = text
        self.draw()

    def done(self, ok: bool, first: str):
        self.result = (ok, data.clean(first))
        self.pending = False
        self.draw()
        if self.group is not None:
            self.group.sync()


class ToolHead(Msg, can_focus=True):
    """The one line that stands for a run of tool calls: "▸ 5 tool calls: 3 files read, 1 command, 1 edit".  Collapsed
    (the default) the calls themselves are hidden - except a failed one; Enter, Space or a click opens them."""
    BINDINGS = [Binding("enter,space", "toggle", "open or close the tool calls", show=False),
                Binding("up", "step(-1)", "previous tool block", show=False), Binding("down", "step(1)", "next tool block", show=False)]

    def __init__(self, speaker: str, width: int, open_: bool):
        self.members: list[Msg] = []
        self.open = open_
        super().__init__("tools", speaker, "", "", width)

    def build(self):
        return _row(self.width, Text(f"[{self.speaker}]", DIM), self.line())

    def build_text(self) -> str:
        return self.line().plain

    def line(self) -> Text:
        names = [w.text for w in self.members]
        t = Text("▾ " if self.open else "▸ ", f"bold {AMBER}")
        t.append(toolgroup.summary(names) if names else "tool calls", DIM)
        failed = sum(1 for w in self.members if w.result is not None and not w.result[0])
        if failed:
            t.append(f"  ·  {failed} failed", MAG)
        now = [w for w in self.members if w.pending]
        if now:
            w = now[-1]
            t.append("  ·  " + toolgroup.doing(w.text, w.target), AMBER)
        return t

    @property
    def plain(self) -> str:
        return toolgroup.summary(w.text for w in self.members) if self.members else "tool calls"

    def add_member(self, w: Msg):
        w.group = self
        self.members.append(w)
        w.display = self.open
        self.sync()

    def sync(self):
        """A call came or finished: the summary is drawn again, a failed call stays in view."""
        for w in self.members:
            w.display = self.open or (w.result is not None and not w.result[0])
        self.draw()

    def set_open(self, open_: bool):
        self.open = open_
        self.sync()

    def action_step(self, d: int):
        heads = list(self.screen.query(ToolHead))
        if self in heads and 0 <= (i := heads.index(self) + d) < len(heads):
            heads[i].focus()
            heads[i].scroll_visible()

    def action_toggle(self):
        self.set_open(not self.open)

    def on_click(self, e: events.Click):
        e.stop()
        self.action_toggle()
        with suppress(Exception):
            self.screen.query_one(ChatInput).focus()


class TextAskModal(ModalScreen):
    """The `ask` tool of the model wants a free answer while Remote Control is on: Enter sends the text, Esc gives none."""
    BINDINGS = [("escape", "none", "no answer")]
    DEFAULT_CSS = f"""
    TextAskModal {{ background: #000000 60%; align: center bottom; }}
    #tbox {{ width: 90; max-width: 100%; height: auto; max-height: 90%; background: {PANEL}; border: heavy {CYAN}; padding: 1 2; }}
    #tin {{ background: #000000 40%; border: none; height: 1; padding: 0 1; }}
    """

    def __init__(self, question: str):
        super().__init__()
        self.question = data.clean(question)

    def compose(self):
        yield Vertical(Static(Text(self.question)), Input(placeholder="answer  ·  Enter sends  ·  Esc: no answer", id="tin"), id="tbox")

    def on_mount(self):
        self.query_one(Input).focus()

    def on_input_submitted(self, e: Input.Submitted):
        e.stop()
        self.dismiss(e.value)

    def action_none(self):
        self.dismiss("")


class ChatScreen(Screen):
    BINDINGS = [("escape", "back", "back"), ("ctrl+c", "copy", "copy"), ("up", "pop_move(-1)", "command list: up"), ("down", "pop_move(1)", "command list: down"),
                ("tab", "pop_pick", "complete"), ("ctrl+o", "subs", "sub-agents"), Binding("ctrl+k", "stop_subs", "stop background sub-agents", priority=True), ("ctrl+n", "notes", "notes & to-dos"),
                Binding("ctrl+t", "tools_toggle", "open or close all tool calls", priority=True),
                Binding("shift+tab", "cycle_mode", "next mode (auto, plan, bypass)", priority=True),
                Binding("f2", "status_focus", "status line: choose model, mode, ...", priority=True),
                Binding("f3", "tools_focus", "pick a tool block (Enter opens, Up and Down move)", priority=True), ("pageup", "scroll(-1)", "scroll the chat up"), ("pagedown", "scroll(1)", "scroll the chat down")]

    def __init__(self, session, resumed: bool = False):
        super().__init__()
        self.s, self.resumed = session, resumed
        session.bg.on_message = self.bg_message               # a sub-agent that worked in the background is done: its result is a message
        session.bg.on_event = self.bg_event                   # the line under the input shows the ones still working
        self.job = self.start_task = None
        self.ready = asyncio.Event()                         # set when the on_start hooks are done
        self.stopping = self.leaving = False
        self.tools: dict[str, Msg] = {}                      # tool call id -> its line, to add the result
        self.topic = "new chat"
        self.hint_shown = False
        self.pop: list[tuple[str, str]] = []                 # the slash popup: matching (name, description)
        self.pop_at: tuple[int, int] | None = None           # the popup completes a word after /archive: where that word stands (None: a slash word)
        self.arc_choices: list[tuple[str, str]] | None = None   # archived chats (id, name and time) for /archive restore, read in a worker
        self.arc_at = 0.0                                    # when they were read
        self.pi = 0
        self.cat = None                                      # (time, Catalog): the words of the input line, kept for a second while typing
        self.bad = False                                     # a command of the input line failed
        self.reloading = False                               # /reload waits for the end of the running turn
        self.status_spans: list[tuple[str, int, int]] = []
        self.sig = core.signature(session.home)              # what /reload compares with
        self.seeing = False                                  # the line above the input shows what the slash words typed do
        self.cur: Msg | None = None                          # the agent message the streamed text goes into
        self.buf = ""
        self.last_render = 0.0
        self.col = COL_MIN                                   # width of column 1 (the longest speaker name so far)
        self.turn_start = 0.0                                # when the running turn began (its closing line shows the time)
        self.queue: list[tuple[str, sl.Parsed, Msg]] = []    # messages typed during a turn: (line, parse, its block)
        self.flushing = False
        self.open_ij: dict[int, Msg] = {}                    # interjections the running turn has not read yet: id -> its block
        self.armed = 0.0                                     # when the first Shift+Tab toward bypass was pressed
        self.tools_open = False                              # Ctrl+T: the runs of tool calls are shown whole (new ones too)
        self.group: ToolHead | None = None                   # the run of tool calls that the next call joins
        self.notes_open = False                              # Ctrl+N: Notes & To-dos beside the chat (split) or instead of it (switch)
        self.picking = False                                 # the file dialog of /upload is open
        self.upload_dir: Path | None = None                  # the folder of the last file chosen in this chat: the dialog opens there
        self.rc: rc.Link | None = None                       # the Remote Control link while /rc is on
        self.rc_prev: tuple | None = None                    # the asker and questioner the session had before the link wrapped them
        self.rc_n = 0                                        # numbers the queued messages the link tells about (q1, q2 ...)
        self.bad_text = ""                                   # why the last command failed (a device is told)
        self.rc_task: asyncio.Task | None = None
        self.rc_wanted: str | None = None                    # set by `lm open --rc NAME`: that opens the link, remote.auto stays out
        self.rc_lock = asyncio.Lock()                        # one link opens at a time (/rc, --rc and remote.auto)
        self.shown = asyncio.Event()                         # set when the screen is up (`lm open --rc` waits for it)

    def compose(self):
        yield Horizontal(
            Vertical(Static(id="head"), Msgs(id="msgs"), Static(id="pop"), Static(id="hint"),
                     ChatInput(placeholder="message  ·  / for commands", id="inp"), SubBar(id="subs"), StatusBar(id="status"), id="box"),
            NotesPane(todos.Store(self.s.run.path), readonly=self.s.readonly, id="notes", names=Names(
                lambda: (self.s.run.run.title, self.s.run.run.description),
                lambda field, text: self.s.rename(text) if field == "title" else self.s.describe(text))), id="body")

    # ------------------------------------------------------------- Notes & To-dos
    def notes_mode(self) -> str:
        """`ui.notes_layout`: split (default) | switch; under 120 columns always switch."""
        try:
            v = str((self.home.settings.get("ui") or {}).get("notes_layout", "split")).strip().lower()
        except Exception:                                    # noqa: BLE001 - a half-edited config.md must not stop the chat
            v = "split"
        return "split" if v != "switch" and self.size.width >= NOTES_SPLIT_MIN else "switch"

    def apply_notes_layout(self):
        pane, box = self.query_one(NotesPane), self.query_one("#box")
        if not self.notes_open:
            pane.display, box.display = False, True
        elif self.notes_mode() == "split":
            pane.styles.width = max(NOTES_WIDTH[0], min(NOTES_WIDTH[1], self.size.width // 3))
            pane.display, box.display = True, True
        else:
            pane.styles.width = "100%"
            pane.display, box.display = True, False

    def on_resize(self, e: events.Resize):
        with suppress(Exception):
            self.apply_notes_layout()

    def notes_focused(self) -> bool:
        f = self.focused
        return f is not None and self.query_one(NotesPane) in f.ancestors_with_self

    def action_notes(self):
        pane = self.query_one(NotesPane)
        self.notes_open = not self.notes_open
        self.apply_notes_layout()
        if self.notes_open:
            pane.focus_main()
        else:
            pane.shutdown()
            self.query_one(ChatInput).focus()

    @property
    def me(self) -> str:
        return speaker_name(self.s.info.agent or "lm")

    @property
    def busy(self) -> bool:
        return self.job is not None and not self.job.done()

    async def on_mount(self):
        app = self.app
        app.chat, self.home = self, app.home
        OFFER_ROOT[:] = [self.home.root]
        if app.anim:
            box = self.query_one("#box")
            box.styles.opacity = 0.0
            box.styles.animate("opacity", 1.0, duration=0.5)
        self.set_interval(0.12, self.spin)
        self.set_interval(1.0, self.poll_stop)
        self.set_interval(termtitle.KEEP_EVERY, self.keep_tab_title)   # claude.exe renames the shared console at will
        self.set_interval(LIMITS_EVERY, self.refresh_limits)            # runs of other chats of the account move the 5h / 7d figures
        self.tab_name = runs.session_names(self.home).get(self.s.id, "")
        self.s.enable_titles()                                       # a person's chat is named by itself
        self.s.on_title.append(lambda title, description: self.title_changed(title))
        self.show_title(self.s.title)
        await self.replay()
        self.refresh_ui()
        self.refresh_subs()
        inp = self.query_one(ChatInput)
        inp.focus()
        self.watch(inp, "selection", self.moved, init=False)          # the cursor of the input is its selection
        if self.s.readonly:                                  # nothing was started here, so no on_start hooks either
            inp.placeholder = "read only  ·  open in another window"
            self.ready.set()
        else:
            self.start_task = asyncio.create_task(self.start())
            self.rc_task = asyncio.create_task(self.rc_auto())     # kept so the task is not collected midway
        self.shown.set()

    async def start(self):
        """The on_start hooks, in a thread: their lines come first, and the first message waits for them."""
        try:
            lines, warning = await core.start_hooks(self.home, self.s)
            for line in lines:
                await self.add("hook", line)
            if warning:
                await self.add("warning", warning)
        finally:
            self.ready.set()

    def read_only_text(self) -> str:
        return READONLY.format(pid=self.s.readonly_by)

    async def replay(self):
        msgs = self.s.messages
        if not msgs:
            if self.s.readonly:
                await self.add("note", self.read_only_text())
            return
        first = next((data.clean_user(x.text) for x in msgs if x.role == "user"), "")
        self.topic = data.clean(" ".join(first.split())[:40]) or self.topic
        if len(msgs) > 40:
            await self.add("note", f"{len(msgs) - 40} earlier messages not shown")
        for x in msgs[-40:]:
            if x.role == "user" and x.origin == ij.HARNESS:   # a note of the harness to the model: not shown
                continue
            if x.role == "user" and x.origin == ij.ORIGIN:    # the user's words, fed into a running turn
                for words in ij.parse(x.text):
                    w = await self.add("user", words)
                    w.tag = "interjection"
                    w.draw()
            elif x.role == "user" and x.origin:               # a sub-agent's result: a note, not words of the user
                await self.add("note", "sub-agent result: " + one_line(x.text.split("untrusted>", 1)[-1], 90))
            elif x.role == "user":
                await self.add("user", data.clean_user(x.text))
            elif x.role == "assistant":
                if x.text.strip():
                    await self.add("agent", x.text)
                for c in x.tool_calls:
                    await self.add("tool", c.name, short_args(c.args))        # (no result is known: nothing is running)
            elif x.text.strip():
                await self.add("note", x.text.strip().splitlines()[0][:100])
        await self.add("note", self.read_only_text() if self.s.readonly else "resumed: go on below")

    # ------------------------------------------------------------- transcript
    async def add(self, kind: str, text: str, extra: str = "", who: str = "", pending: bool = False) -> Msg:
        """`who`: the sub-agent that speaks (column 1) instead of this chat's own agent."""
        text, extra = data.clean(text), data.clean(extra)
        speaker = (who or "You") if kind == "user" else (who or self.me) if kind in ("agent", "tool") else ""
        if len(speaker) > self.col:                          # a longer name: column 1 grows for the whole transcript
            self.col = len(speaker)
            for old in self.query(Msg):
                old.set_width(self.col)
        w = Msg(kind, speaker, text, extra, self.col)
        w.pending = pending
        box = self.query_one("#msgs", VerticalScroll)
        if kind == "user" and box.children:
            w.styles.margin = (1, 0, 0, 0)                   # a blank line before every You block, not before the first
        at_end = box.scroll_y >= box.max_scroll_y - 1        # a reader who scrolled up is not pulled down
        grouped = kind == "tool" and not text.startswith("◆")        # a sub-agent card is an answer to read, not technical output
        if grouped:
            last = box.children[-1] if box.children else None
            if self.group is None or last is None or not (last is self.group or last.group is self.group) \
                    or self.group.speaker != speaker:
                self.group = ToolHead(speaker, self.col, self.tools_open)     # no run in progress: this call starts a new block
                await box.mount(self.group)
            self.group.add_member(w)
        await box.mount(w)
        if grouped:
            self.group.sync()
        if at_end or kind == "user":
            box.scroll_end(animate=False)
        return w

    def flush_text(self, end: bool = False):
        """Render the pending streamed text into the current agent message; `end` closes the message."""
        if self.cur is not None and self.cur.plain != data.clean(self.buf):
            box = self.query_one("#msgs", VerticalScroll)
            at_end = box.scroll_y >= box.max_scroll_y - 1
            self.cur.set_agent(data.clean(self.buf))
            if at_end:
                box.scroll_end(animate=False)
            self.last_render = time.monotonic()
        if end:
            self.cur, self.buf = None, ""

    def refresh_subs(self):
        with suppress(Exception):
            self.query_one(SubBar).show(self.s.subs.current(), self.s.subs.current_flows())

    def open_sub(self, sid: str):
        self.app.push_screen(SubScreen(self.s.subs, sid, self.me))

    async def action_stop_subs(self):
        gone = self.s.bg.cancel_all()
        await self.add("note", f"stopped: {', '.join(gone)}" if gone else "no sub-agent is working in the background")

    def action_subs(self):
        """Ctrl+O: the sub-agent of the line under the input, or a list when there are several."""
        subs = self.s.subs.current() or list(self.s.subs.subs.values())
        if len(subs) == 1:
            return self.open_sub(subs[0].id)
        if subs:
            opts = [(x.id, x.name, one_line(x.task, 34) or ("running" if not x.done else "done"), True) for x in subs]
            self.app.push_screen(Pick("Sub-agents", opts, subs[-1].id), lambda v: v is not None and self.open_sub(v))

    async def show(self, ev):
        if isinstance(ev, Interjected):
            self.flush_text(end=True)
            if ev.parent:                                    # a message the agent passed on to a sub-agent
                await self.add("note", f"{ev.agent or 'sub-agent'} read the message passed on to it")
            elif w := self.open_ij.pop(ev.id, None):
                w.tag = "interjection · read by the agent"
                w.draw()
                self.refresh_ui()
            return
        if getattr(ev, "parent", ""):
            return                                           # an inner step of a sub-agent: its own screen shows it (Ctrl+O)
        if isinstance(ev, (SubagentStart, SubagentEnd)):
            self.refresh_subs()
            return
        if isinstance(ev, WorkflowEvent):                    # its phases show in the line under the input
            self.refresh_subs()
            if ev.kind in ("start", "end"):
                self.flush_text(end=True)
                phases = f" · phases: {', '.join(ev.phases)}" if ev.phases else ""
                text = (f"workflow {ev.name}: {ev.text}{phases}" if ev.kind == "start"
                        else f"workflow {ev.name} {'ended' if ev.ok else 'failed'}: {ev.text}")
                await self.add("note", text[:600])
            return
        if isinstance(ev, m.Text):
            # API adapters stream one Text per token delta: they all belong to ONE message, whitespace included.
            who = origin(ev) or self.me
            if self.cur is not None and self.cur.speaker != who:
                self.flush_text(end=True)                    # another speaker: a message of its own
            self.buf += ev.text
            if self.cur is None:
                if not self.buf.strip():
                    return                                   # leading whitespace waits for the first real text
                self.cur = await self.add("agent", self.buf, who=origin(ev))
                self.last_render = time.monotonic()
            elif time.monotonic() - self.last_render >= 0.05:
                self.flush_text()
            return
        self.flush_text(end=True)
        if isinstance(ev, m.ToolCall):
            if ev.name in SUB_TOOLS:                         # the card of a sub-agent: its name, its task in short, then the result
                name = next((v for k in ("subagent_type", "name", "description")
                             if isinstance(v := ev.args.get(k), str) and v.strip()), "agent")
                self.tools[ev.id] = await self.add("tool", "◆ " + one_line(name, 40), one_line(str(ev.args.get("prompt", ""))), who=origin(ev), pending=True)
            else:
                self.tools[ev.id] = w = await self.add("tool", ev.name, short_args(ev.args), who=origin(ev), pending=True)
                path = next((str(v) for k in ("file_path", "path") if isinstance(v := ev.args.get(k), str) and v.strip()), "")
                w.target = data.clean(path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1])[:40]
        elif isinstance(ev, ToolDone):
            first = next((ln.strip() for ln in data.clean(ev.text).splitlines() if ln.strip()), "")
            w = self.tools.pop(ev.id, None)
            if w:
                w.done(ev.ok, first)
            else:
                await self.add("note", f"{ev.name} {'ok' if ev.ok else 'failed'}: {first}"[:120])
        elif isinstance(ev, Notice):
            await self.add(ev.kind if ev.kind in NOTICE else "note", ev.text)
        elif isinstance(ev, m.Error):
            await self.add("error", f"{ev.kind}: {ev.message}")
        elif isinstance(ev, (m.Usage, m.RateLimit)):
            self.refresh_ui()

    # ----------------------------------------------------------------- status
    def title_changed(self, title: str) -> None:
        """The title changed: by the naming model (another thread) or by /rename (the app's own thread)."""
        try:
            self.app.call_from_thread(self.show_title, title)
        except RuntimeError:                                         # called on the app's thread: no hop needed
            self.show_title(title)

    def show_title(self, title: str) -> None:
        """The chat title as tab/window title of the app (the terminal shows it); empty = the plain program name."""
        title = data.clean(title)
        self.app.title = f"{title} · Lightmind" if title else "Lightmind"
        if self.rc is not None:
            self.rc.title(title, self.s.info.description)
        self.tab_text = termtitle.tab_text(title, getattr(self, "tab_name", ""))
        termtitle.set_title(self.tab_text, self.tab_write())

    def tab_write(self):
        """Where the OSC 0 title goes: the Textual driver (it owns the terminal), None when there is none yet."""
        w = getattr(getattr(self.app, "_driver", None), "write", None)
        return w if callable(w) else (lambda _s: None)

    def keep_tab_title(self) -> None:
        """Set the tab title again when something else (claude.exe) changed it."""
        termtitle.keep(getattr(self, "tab_text", "") or termtitle.tab_text(), self.tab_write())

    def refresh_limits(self) -> None:
        """The 5h / 7d figures of the account again from its state file (cheap), redrawn when they moved."""
        before = dict(self.s.limits)
        self.s.refresh_limits()
        if self.s.limits != before:
            self.refresh_ui()

    def status_text(self) -> Text:
        i, ms = self.s.info, data.modes(self.home)
        mo, pct = ms.get(i.mode), i.ctx_pct * 100
        n = max(0, min(10, round(pct / 10)))
        tpl = str(data.ui(self.home).get("statusline") or data.DEFAULT_STATUS)
        lim = lambda k: f"{i.limits[k] * 100:.0f}%" if k in i.limits else "-"      # noqa: E731
        lcol = lambda k: MINT if i.limits.get(k, 0) * 100 < 60 else AMBER if i.limits[k] * 100 < 85 else MAG      # noqa: E731
        vals = {"server": (i.server, CYAN), "model": (i.model, TXT), "account": (i.account, TXT), "effort": (i.effort, TXT),
                "mode": (mo.label if mo else i.mode, f"bold {mo.color if mo else TXT}"),
                "ctx_bar": ("█" * n + "░" * (10 - n), MINT if pct < 60 else AMBER if pct < 85 else MAG),
                "ctx": (f"{pct:.0f}%", TXT), "ctx_used": (human(i.ctx_used), TXT), "ctx_limit": (human(i.ctx_limit), TXT),
                "cost": (f"{i.cost_usd:.2f}" + ("~" if i.cost_est else ""), AMBER), "agent": (i.agent or "-", TXT),
                "project": (i.project or "-", TXT), "id": (data.short_id(i.id), DIM), "status": (i.status, TXT),
                "five_hour": (lim("five_hour"), lcol("five_hour") if "five_hour" in i.limits else TXT),
                "seven_day": (lim("seven_day"), lcol("seven_day") if "seven_day" in i.limits else TXT)}
        if "{tools}" in tpl:
            vals["tools"] = (str(len([i for i in data.items(self.home) if i.name not in self.s.state.tools_removed])), TXT)
        t, pos, spans = Text(" "), 0, []
        for mt in re.finditer(r"\{(\w+)\}", tpl):
            t.append(tpl[pos:mt.start()], "#aab1d8")
            v = vals.get(mt[1])
            a = len(t.plain)
            t.append(data.clean(v[0]) if v else mt[0], v[1] if v else DIM)             # an unknown placeholder stays as written
            if mt[1] in SEGMENTS and v:
                spans.append((SEGMENTS[mt[1]], a, len(t.plain)))
            pos = mt.end()
        t.append(tpl[pos:], "#aab1d8")
        self.status_spans = spans
        if self.open_ij:
            t.append(f" · {len(self.open_ij)} interjected", AMBER)
        if self.queue:
            t.append(f" · {len(self.queue)} queued", AMBER)
        if self.rc is not None:
            t.append(" · RC", f"bold {MINT}")
        return t

    def refresh_ui(self):
        i = self.s.info
        mo = data.modes(self.home).get(i.mode)
        h = Text()
        h.append(" ◉ ", f"bold {CYAN}")
        h.append(i.server, f"bold {TXT}")
        h.append(f"   ⌁ {data.short_id(i.id)} · {self.topic} · agent {i.agent or '-'}\n", DIM)
        h.append(f" orbit › {i.server} › {i.agent or 'chat'}  ·  effort {i.effort}  ·  ctx {human(i.ctx_limit)}", DIM)
        head = self.query_one("#head", Static)
        head.update(h)
        head.styles.border_bottom = ("heavy", NEUTRAL)
        st = self.query_one("#status", StatusBar)
        st.show(self.status_text(), self.status_spans)
        st.styles.background = BG                             # no tile: the plain app background in every mode, the mode shows in the text only
        st.styles.border_top = ("heavy", NEUTRAL)
        self.rc_sync()

    def spin(self):
        """The line above the input: the keys, or what the turn is doing."""
        hint = self.query_one("#hint", Static)
        if self.busy:
            ch = SPIN[int(time.monotonic() * 8) % 10]
            hint.update(Text(f"  {ch} " + ("stopping … (a few seconds)" if self.stopping else "working … (Esc stops)"),
                             style=MAG if self.stopping else AMBER))
            self.hint_shown = False
        elif not self.hint_shown:
            hint.update(Text("  " + self.read_only_text(), style=AMBER) if self.s.readonly else Text(KEYS, style=DIM))
            self.hint_shown = True

    # ------------------------------------------------------------------- turn
    def on_text_area_changed(self, e: TextArea.Changed):
        self.suggest(e.text_area.value, e.text_area.cursor_position)
        self.seen(e.text_area.value)

    def suggest(self, value: str, cursor: int):
        """The popup: the commands that fit the slash word under the cursor, anywhere in the text; none without one."""
        self.pop_at = None
        if (arg := self.archive_arg(value, cursor)) is not None:             # `/archive ` offers list, restore and the archived chats
            found, self.pop_at = arg
        elif (arg := self.ultracode_arg(value, cursor)) is not None:         # `/ultracode ` offers on, off and status
            found, self.pop_at = arg
        else:
            w = sl.word_at(value, cursor)
            q = value[w[0] + 1:w[1]].lower() if w else ""
            found = [c for c in data.slash_commands(self.home) if c[0].startswith(q)] if w else []
            found.sort(key=lambda c: c[0] != q)                              # an exact match first
        self.pop, self.pi = found, self.pi if found == self.pop else 0
        self.draw_pop()

    def archive_arg(self, value: str, cursor: int) -> tuple[list[tuple[str, str]], tuple[int, int]] | None:
        """The options for the word after `/archive ` or `/archive restore ` under the cursor, and where that word stands
        (start, end); None when the cursor is not in such a word.  The archived chats are the ones that fit what was typed."""
        m = ARCHIVE_ARG.search(value[:cursor])
        if m is None:
            return None
        end = cursor
        while end < len(value) and not value[end].isspace():
            end += 1
        typed = m.group(2).lower()
        if m.group(1):
            options = [c for c in self.archive_choices() if not typed or c[0].lower().startswith(typed) or typed in c[1].lower()]
        else:
            options = [c for c in ARCHIVE_SUBS if c[0].startswith(typed)]
        return options[:POPUP_ROWS], (m.start(2), end)

    def ultracode_arg(self, value: str, cursor: int) -> tuple[list[tuple[str, str]], tuple[int, int]] | None:
        """The words that fit what is typed after `/ultracode ` under the cursor, and where that word stands (start, end);
        None when the cursor is not in such a word."""
        m = sl.ULTRACODE_ARG.search(value[:cursor])
        if m is None:
            return None
        end = cursor
        while end < len(value) and not value[end].isspace():
            end += 1
        return [c for c in ULTRACODE_SUBS if c[0].startswith(m.group(1).lower())], (m.start(1), end)

    def archive_choices(self) -> list[tuple[str, str]]:
        """The archived chats (id, name and time).  A disk scan, so it runs in a worker and is kept for 10 seconds; the popup
        fills in when the worker is done (`load_archive_choices`)."""
        now = time.monotonic()
        if self.arc_choices is None or now - self.arc_at > 10:
            self.arc_at = now
            self.app.run_worker(self.load_archive_choices())
        return self.arc_choices or []

    async def load_archive_choices(self):
        self.arc_choices = await asyncio.to_thread(resume.archived_choices, self.home)
        with suppress(Exception):                                            # (the input may be gone when the screen is)
            inp = self.query_one(ChatInput)
            if self.pop_at is not None:
                self.suggest(inp.value, inp.cursor_position)

    def moved(self):
        """The cursor moved: an open popup follows it to another slash word or goes (a closed one stays closed)."""
        with suppress(Exception):                                            # (the input may be gone when the screen is)
            inp = self.query_one(ChatInput)
            if self.pop:
                self.suggest(inp.value, inp.cursor_position)

    def draw_pop(self):
        box = self.query_one("#pop", Static)
        box.display = bool(self.pop)
        if not self.pop:
            return
        top = max(0, self.pi - POPUP_ROWS + 1)
        w = max(len(n) for n, _ in self.pop) + 3
        t = Text()
        for j, (name, desc) in list(enumerate(self.pop))[top:top + POPUP_ROWS]:
            sel = j == self.pi
            lead = "" if self.pop_at is not None else "/"                    # a word after /archive has no slash
            t.append(f" ▌ {lead}{name:<{w}}" if sel else f"   {lead}{name:<{w}}", f"bold {BG} on {CYAN}" if sel else TXT)
            t.append(f"  {desc}" + chr(10), CYAN if sel else DIM)
        t.append("↑↓ choose · Tab or ⏎ complete · Esc close", DIM)
        box.update(t)

    def action_pop_move(self, d: int):
        if not self.pop:
            raise SkipAction()
        self.pi = (self.pi + d) % len(self.pop)
        self.draw_pop()

    def action_pop_pick(self):
        if not self.pop:
            raise SkipAction()
        inp = self.query_one(ChatInput)
        if self.pop_at is not None:                          # a word after /archive: only that word is replaced
            a, b = self.pop_at
            name, rest = self.pop[self.pi][0], inp.value[b:]
            more = name == "restore"                         # the chat to bring back comes next
            inp.value = inp.value[:a] + name + (" " if more and not rest[:1].isspace() else "") + rest
            inp.cursor_position = a + len(name) + (1 if more else 0)
            return
        w = sl.word_at(inp.value, inp.cursor_position)
        if w is None:                                        # the cursor has left the word
            self.pop = []
            return self.draw_pop()
        inp.value, inp.cursor_position = sl.complete(inp.value, *w, self.pop[self.pi][0])       # only that word is replaced

    # ------------------------------------------------------- tool calls, status line, mode
    def action_tools_toggle(self):
        """Ctrl+T: every run of tool calls open, or every run closed (the next one follows)."""
        self.tools_open = not self.tools_open
        box = self.query_one("#msgs", Msgs)
        at_end = box.scroll_y >= box.max_scroll_y - 1
        for head in self.query(ToolHead):
            head.set_open(self.tools_open)
        if at_end:
            self.call_after_refresh(box.scroll_end, animate=False)
        self.app.notify("tool calls: " + ("all open" if self.tools_open else "all closed"), timeout=1.5)

    def action_status_focus(self):
        """F2: the status line takes the keys (Left and Right choose, Enter opens, Esc gives them back)."""
        self.query_one(StatusBar).focus()

    def action_tools_focus(self):
        """F3: the last block of tool calls takes the keys (Up and Down go to the others, Enter opens, Esc gives them back)."""
        if heads := list(self.query(ToolHead)):
            heads[-1].focus()
            heads[-1].scroll_visible()

    async def on_status_bar_chosen(self, e: StatusBar.Chosen):
        e.stop()
        if e.key == "context":
            i = self.s.info
            self.query_one(ChatInput).focus()
            return await self.add("note", f"context: {human(i.ctx_used)} of {human(i.ctx_limit)} used ({i.ctx_pct * 100:.0f} %)")
        if self.s.readonly:
            self.query_one(ChatInput).focus()
            return await self.add("note", self.read_only_text())
        fn = {"model": self.cmd_model, "mode": self.cmd_mode, "account": self.cmd_account, "effort": self.cmd_effort}.get(e.key)
        if fn is not None:
            await fn("")

    async def action_cycle_mode(self):
        """Shift+Tab: the next mode in `order` of the mode files (auto, plan, bypass); the status line follows at once."""
        if self.notes_focused() or self.pop:
            raise SkipAction()                                # Notes use the key themselves; the popup keeps Tab
        if self.s.readonly:
            return await self.add("note", self.read_only_text())
        found = data.modes(self.home)
        ms = [k for k, m in found.items() if m.order is not None] or list(found)     # a mode file without `order` is not in the cycle
        if not ms:
            return await self.add("error", f"mode: {data.modes_error(self.home)}")
        nxt = ms[(ms.index(self.s.mode) + 1) % len(ms)] if self.s.mode in ms else ms[0]
        never = found[nxt].never_asks
        if never and time.monotonic() - self.armed > BYPASS_CONFIRM:        # it never asks: not by a slip of the finger
            self.armed = time.monotonic()
            return self.app.notify(f"Shift+Tab again to switch to {nxt} (it never asks)", severity="warning", timeout=BYPASS_CONFIRM)
        self.armed = 0.0
        r = core.command(self.home, self.s, "mode", nxt)
        if not r.ok:
            return await self.add("error", r.text)
        self.refresh_ui()
        self.app.notify(r.text, timeout=1.5)
        if never:
            await self.add("warning", f"mode: {nxt}, nothing is asked any more")

    def action_scroll(self, d: int):
        box = self.query_one("#msgs", Msgs)
        box.scroll_page_down() if d > 0 else box.scroll_page_up()

    # ------------------------------------------------------------------- copy
    def copy(self, text: str):
        self.app.copy_to_clipboard(text)                      # OSC 52 (Windows Terminal, most others)
        if sys.platform == "win32":
            self.app.run_worker(asyncio.to_thread(system_clip, text))      # and clip.exe where OSC 52 is not understood
        self.app.notify(f"copied {len(text)} characters", timeout=1.5)

    def on_text_selected(self, e: events.TextSelected):
        if text := self.get_selected_text():
            self.copy(text)
        self.query_one(ChatInput).focus()

    def action_copy(self):
        if not (text := self.get_selected_text()):
            raise SkipAction()                                # Ctrl+C without a selection keeps its meaning
        self.copy(text)

    async def on_chat_input_submitted(self, e: ChatInput.Submitted):
        if self.pop and self.pop_at is not None and e.value[slice(*self.pop_at)] != self.pop[self.pi][0]:
            return self.action_pop_pick()                     # Enter on a popup after /archive: complete, the next Enter runs it
        w = sl.word_at(e.value, e.input.cursor_position)
        if self.pop and w and e.value[w[0]:w[1]].lower() != "/" + self.pop[self.pi][0]:
            return self.action_pop_pick()                     # Enter on a popup: complete, the next Enter runs it
        if self.pop:                                          # the cursor is off the word: the popup goes
            self.pop = []
            self.draw_pop()
        txt = e.value.strip()
        if not txt:
            e.input.value = ""
            if self.queue and not self.busy:                  # after a failed turn: Enter sends what waits
                await self.flush_queue()
            return
        await self.submit_line(txt, "", e.input)

    async def submit_line(self, txt: str, source: str = "", box: ChatInput | None = None) -> dict:
        """One line of the user, from the keyboard (`source` empty, `box` the input line, which is cleared or kept) or from a
        device on the Remote Control link (`source` its name).  Returns what a device is told: `{}`, or the interjection
        (`{"interject": id, "state": "pending"|"queued"}`).  What a keyboard answers with a note, a device gets as
        `rc.Refused`."""
        async def refuse(kind: str, text: str) -> dict:
            await self.add(kind, text)
            if source:
                raise rc.Refused(text)
            return {}

        def clear():
            if box is not None:
                box.value = ""
        if (m_ := RC_WORD.fullmatch(txt)) is not None:        # /rc <name> and /remote-control <name>: the name is the rest of the line
            if source:
                raise rc.Refused(f"/{m_[1].lower()} is not available from a device")
            clear()
            return await self.cmd_rc(m_[2] or "")
        # The slash words of the line (slash.py): commands run in order, skills load for this turn, the rest is sent.
        p = sl.parse(txt, self.catalog(fresh=True)) if sl.has_slash(txt) else sl.Parsed(text=txt)
        if p.error:                                           # nothing runs or is sent, the line stays to be corrected
            return await refuse("note" if p.unknown else "error", p.error)
        if source and (no := next((a.name for a in [*p.acts, *([p.then] if p.then else [])] if a.name in core.RC_DENY), "")):
            raise rc.Refused(f"/{no} is not available from a device")
        if any(a.kind == "insert" for a in p.acts):           # /upload: the files go into the line, nothing is sent
            return await self.upload_from(box)
        send = bool(p.text or p.skills)
        if send and self.s.readonly:
            clear()
            return await refuse("note", self.read_only_text())
        if send and self.busy and p.then:
            return await refuse("note", "still on the last message: Esc stops it")        # the line stays
        if send and self.busy and not (self.queue or p.skills or p.acts) and (it := self.s.interject(p.text)) is not None:
            clear()                                           # into the running turn: read at its next step, the agent answers
            self.flush_text(end=True)                         # what the agent says next starts a block below this one
            w = await self.say_user(txt, source)
            w.tag = "interjection · read at the next step"
            w.draw()
            self.open_ij[it.id] = w
            w.rc_id = f"i{it.id}"
            self.rc_tell("interject", id=w.rc_id, state="pending", text=txt)
            self.refresh_ui()
            return {"interject": w.rc_id, "state": "pending"}
        if send and (self.busy or self.queue):                # it waits for the turn (or goes after those that wait)
            clear()
            w = await self.say_user(txt, source)
            w.queued = True
            w.draw()
            self.rc_n += 1
            w.rc_id = f"q{self.rc_n}"
            self.queue.append((txt, p, w))
            self.rc_tell("interject", id=w.rc_id, state="queued", text=txt)
            self.refresh_ui()
            if not self.busy:
                await self.flush_queue()
            return {"interject": w.rc_id, "state": "queued"}
        clear()
        self.bad_text = ""
        if not await self.run_acts(p.acts):
            if box is not None:
                box.value = txt                               # a command failed: nothing is sent, the line stays
            if source:
                raise rc.Refused(self.bad_text or "a command failed")
            return {}
        if not send:
            if p.then:
                await self.run_acts([p.then])
            return {}
        await self.say_user(p.text, source)
        if self.topic == "new chat":
            self.topic = data.clean(" ".join(p.text.split())[:40])
        self.launch(sl.message(self.home, p, data.skill_commands(self.home)))
        if p.then:                                            # a command that leaves the chat stood last: after the answer
            self.job.add_done_callback(lambda t, a=p.then: self.after(t, a))
        return {}

    async def say_user(self, text: str, source: str = "") -> Msg:
        """A block "You" in the transcript (from a device: "You · <device>") and the same line on the link."""
        w = await self.add("user", text, who=speaker_name(f"You · {source}") if source else "")
        if self.rc is not None:
            self.rc.user_text(text, source)
        return w

    def catalog(self, fresh: bool = False) -> sl.Catalog:
        """The words an input line may hold: kept for a second while typing, built anew when a line is sent."""
        if not fresh and self.cat and time.monotonic() - self.cat[0] < 1:
            return self.cat[1]
        h = self.home
        cat = sl.Catalog(BUILTIN, data.skill_commands(h), [n for n, _ in data.slash_rows(h)],
                         {"effort": sl.Take(data.efforts), "model": sl.Take(lambda: sl.models(h, self.s.account), sl.model_ok)})
        self.cat = time.monotonic(), cat
        return cat

    def seen(self, value: str):
        """The line above the input says what the slash words typed so far will do; without any it shows the keys."""
        p = sl.parse(value, self.catalog()) if sl.has_slash(value) and not self.busy else None
        line = sl.summary(p) if p and not p.error else ""
        if line:
            self.query_one("#hint", Static).update(Text(line, style=CYAN))
            self.seeing = self.hint_shown = True              # spin() leaves the line alone while it shows this
        elif self.seeing:
            self.seeing = self.hint_shown = False             # spin() puts the keys back

    async def run_acts(self, acts) -> bool:
        """Run commands in order, each as if it were typed alone; False when one failed (the rest does not run)."""
        for a in acts:
            self.bad = False
            try:
                await self.slash(a.line)
            except Exception as e:                            # noqa: BLE001 - a bug in a command must not take the UI down
                self.bad_text = f"/{a.name}: {type(e).__name__}: {e}"
                await self.add("error", self.bad_text)
                return False
            if self.bad:
                return False
        return True

    def after(self, task, act):
        """The command that leaves the chat and stood last runs once the answer is in (not after Esc or an error)."""
        if not (task.cancelled() or task.exception() or self.leaving) and task.result():
            self.app.run_worker(self.run_acts([act]))

    def bg_event(self, ev):
        if self.rc is not None:
            self.rc.events(ev, bg=True)
        self.refresh_subs()
        if isinstance(ev, Notice):                           # e.g. results wait because of spawn.auto_turns
            self.app.run_worker(self.add("note", ev.text))

    def bg_message(self, text: str):
        """A background sub-agent is done: its result is a turn of its own when the chat is free, else it follows the turn."""
        if not self.leaving and not self.s.closed:
            self.app.run_worker(self.bg_flush())

    async def bg_flush(self):
        """Send the framed results to the chat (never as a message of the user: it shows as a note, not as "You").  What the user
        typed goes first; at most `spawn.auto_turns` such turns in a row."""
        if self.busy or self.queue or self.flushing or self.leaving or self.s.closed or self.s.readonly:
            return
        text = self.s.bg.take()
        if text is None:
            return
        await self.add("note", "sub-agent result arrived: " + one_line(text.split("untrusted>", 1)[-1], 90))
        if self.rc is not None:
            self.rc.raw(protocol.sub_messages(self.s.id, f"t{self.rc.turn_n + 1}", text))
        self.launch(text, origin="subagent")

    def launch(self, text: str, origin: str = ""):
        self.stopping = False
        self.job = asyncio.create_task(self.turn(text, origin))
        self.job.add_done_callback(self.drained)
        self.rc_sync()

    def drained(self, task):
        """A turn is over: the messages that waited go out as one (after an error or Esc they keep waiting)."""
        if self.leaving:
            return
        self.rc_sync()
        if self.reloading and not self.s.closed:               # before the waiting messages and sub-agent results go out
            self.reloading = False
            r, self.sig = core.reload(self.home, self.s, self.sig)
            self.app.run_worker(self.show_reload(r))
        if not self.queue:
            if not task.cancelled() and task.exception() is None and task.result():
                self.app.run_worker(self.bg_flush())          # results that came in while the turn ran
            return
        if not task.cancelled() and task.exception() is None and task.result():
            self.app.run_worker(self.flush_queue())
        else:
            self.app.run_worker(self.add("note", "queued messages wait: Enter sends them, Esc takes them back"))

    async def take_back(self):
        """The waiting messages go back into the input line (joined by line breaks) and out of the transcript."""
        q, self.queue = self.queue, []
        inp = self.query_one(ChatInput)
        cur = inp.value
        inp.value = "\n".join(t for t, _, _ in q) + ("\n" + cur if cur.strip() else "")
        for _, _, w in q:
            self.rc_tell("interject", id=getattr(w, "rc_id", ""), state="dropped", text=w.text)
            await w.remove()
        self.refresh_ui()

    async def flush_queue(self):
        """Run the commands of the waiting messages in order, then send all their texts as one message."""
        if not self.queue or self.busy or self.leaving or self.flushing:
            return
        self.flushing = True
        try:
            for _, p, _ in list(self.queue):
                acts, p.acts = p.acts, []
                if not await self.run_acts(acts):
                    return await self.take_back()             # nothing is sent, the text is back in the input
            if self.busy:                                      # a command started a turn itself: the rest goes after it
                return
            q, self.queue = self.queue, []
            ps = [p for _, p, _ in q]
            one = sl.Parsed(text="\n\n".join(p.text for p in ps if p.text),
                            skills=list(dict.fromkeys(n for p in ps for n in p.skills)))
            for t, p, w in q:
                w.queued, w.text = False, data.clean(p.text or t)
                w.draw()
            if self.topic == "new chat":
                self.topic = data.clean(" ".join((one.text or q[0][0]).split())[:40])
            self.refresh_ui()
            self.launch(sl.message(self.home, one, data.skill_commands(self.home)))
            for _, _, w in q:
                self.rc_tell("interject", id=getattr(w, "rc_id", ""), state="sent", text="")
        finally:
            self.flushing = False

    async def turn(self, text: str, origin: str = "") -> bool:
        agen, ok, cost0, status, errored = None, False, 0.0, "done", False
        try:
            await self.ready.wait()
            self.turn_start, cost0 = time.monotonic(), self.s.info.cost_usd
            if self.rc is not None:
                self.rc.turn_start()
            agen = self.s.send(text, origin=origin) if origin else self.s.send(text)
            async for ev in agen:
                if self.rc is not None:
                    errored |= self.rc.events(ev)
                await self.show(ev)
            ok = True
        except asyncio.CancelledError:                      # Esc: the provider has been closed on the way up
            status = "cancelled"
            if (me := asyncio.current_task()) is not None:
                me.uncancel()
            with suppress(Exception):
                await self.add("note", "stopped")
        except Exception as e:                              # noqa: BLE001 - NotLoggedIn and friends: the session stays usable
            status = "error"
            with suppress(Exception):
                await self.add("error", str(e) or type(e).__name__)
        finally:
            with suppress(Exception):                       # (the screen may be gone when the app quits)
                self.flush_text(end=True)
                if agen is not None:
                    await agen.aclose()
            with suppress(Exception):
                if self.rc is not None and self.rc.turn_id:
                    i = self.s.info
                    lr = self.s.last_result
                    failed = errored or (lr is not None and lr.status == "failed")
                    self.rc.turn_end("error" if status == "done" and failed else status, i.cost_usd - cost0, i.ctx_pct)
            with suppress(Exception):
                if ok:                                      # the pale closing line: time, cost, context
                    i = self.s.info
                    await self.add("stats", turn_line(time.monotonic() - self.turn_start, i.cost_usd - cost0, i.cost_est, i.ctx_pct * 100))
            with suppress(Exception):
                for head in self.query(ToolHead):            # a stopped turn leaves calls without a result: none is "running" any more
                    for w in head.members:
                        w.pending = False
                    head.sync()
                self.adopt_leftover()
                self.refresh_ui()
                self.keep_tab_title()
        return ok

    def adopt_leftover(self, withdraw: bool = False):
        """Interjections the turn never read (it was stopped or failed, or they came in at its very end) are queued messages
        again, in front of the ones that waited, so nothing the user wrote is lost.  `withdraw`: also the ones still open."""
        items = self.s.take_leftover() + (self.s.inbox.withdraw() if withdraw else [])
        front = []
        for it in items:
            if (w := self.open_ij.pop(it.id, None)) is not None:
                w.queued, w.tag = True, ""
                w.draw()
                self.rc_tell("interject", id=getattr(w, "rc_id", f"i{it.id}"), state="queued", text=w.text)
                front.append((w.text, sl.Parsed(text=w.text), w))
        for w in self.open_ij.values():                       # read after all (the event was not shown): settle the label
            w.tag = "interjection · read by the agent"
            w.draw()
        self.open_ij.clear()
        self.queue[:0] = front

    async def finish(self):
        """Stop a running turn, close the session (on_end hooks run in a thread) and let the app know."""
        self.s.bg.on_message = None                          # nothing new starts; the sub-agents still working are cancelled by close()
        if self.busy:
            self.job.cancel()
            await asyncio.wait({self.job}, timeout=10)
        await self.rc_stop("ended")
        with suppress(Exception):
            self.query_one(NotesPane).shutdown()             # the notes field is saved on the way out
        lines = await asyncio.to_thread(self.s.close)
        self.app.exit_lines += lines
        self.app.chat = None

    async def poll_stop(self):
        """The resume menu asked to end this chat (`runs.request_stop`): close it as if Esc had been pressed."""
        if self.leaving or self.s.readonly or not runs.take_stop(self.s.run.path):
            return
        self.app.notify("this chat was ended from the resume menu")
        self.leaving = True
        await self.finish()
        await self.app.back()

    async def action_back(self):
        if isinstance(self.focused, (StatusBar, ToolHead)):    # Esc gives the keys back to the input first
            self.query_one(ChatInput).focus()
            return
        if self.notes_open and (self.notes_focused() or self.notes_mode() == "switch"):
            if not self.query_one(NotesPane).escape():
                self.action_notes()
            return
        if self.pop:
            self.pop = []
            return self.draw_pop()
        if self.queue or self.open_ij:                        # Esc takes the waiting messages back (and stops the turn)
            self.adopt_leftover(withdraw=True)
            await self.take_back()
            if not self.busy:
                return
        if self.busy:
            if not self.stopping:
                self.stopping = True
                self.job.cancel()
        elif not self.leaving:
            self.leaving = True
            await self.finish()
            await self.app.back()

    # ----------------------------------------------------------------- slash
    async def slash(self, line: str):
        name, _, arg = line[1:].partition(" ")
        name, arg = name.lower(), arg.strip()
        action = dict(data.slash_rows(self.home)).get(name)         # a row of ui.slash wins over a built-in
        if action:
            return await self.go(action, name)
        if self.s.readonly and name not in READONLY_OK:             # a command or a skill would change the chat
            return await self.add("note", self.read_only_text())
        fn = getattr(self, "cmd_" + name.replace("-", "_"), None) if name in BUILTIN else None
        if fn is not None:
            return await fn(arg)
        sk = data.skill_commands(self.home).get(name)
        if sk is None:
            return await self.add("note", f"unknown command /{name}")
        await self.use_skill(line, sk, arg)

    async def use_skill(self, line: str, skill, request: str):
        """`/<skill> <request>`: the model gets an instruction to use the skill plus the request as a user message."""
        if self.s.readonly:
            return await self.add("note", self.read_only_text())
        if self.busy:
            return await self.add("note", "still on the last message: Esc stops it")
        await self.add("user", line)
        if self.topic == "new chat":
            self.topic = data.clean(" ".join(skill.name.split())[:40])
        self.launch(skill_message(self.home, skill, request))

    async def cmd_skill(self, arg: str):
        """/skill lists the skills; `/skill <name> <request>` uses one (also for a name that is a built-in command)."""
        cmds = data.skill_commands(self.home)
        name, _, request = arg.partition(" ")
        if name:
            sk = cmds.get(name.lower()) or cmds.get("skill:" + name.lower())
            if sk is None:
                return await self.add("error", f"skill: no skill named {name!r}")
            return await self.use_skill(f"/skill {arg}", sk, request)
        lines = [f"/{c}  {' '.join(s.description.split())[:90]}" for c, s in cmds.items()]
        await self.add("note", "skills (always on; type /<name> <request> to use one):\n" + "\n".join(lines) if lines else "no skills in this tree yet")

    async def go(self, action: str, name: str = ""):
        app = self.app
        if action == "accounts":
            app.push_screen(AccountsScreen())
        elif action == "dash":
            app.push_screen(DashScreen())
        elif action == "settings":
            from .settings import SettingsScreen
            app.push_screen(SettingsScreen())
        elif action == "quit":
            await app.action_quit()
        elif action in ("new_chat", "resume", "archive"):
            await self.finish()
            app.switch_screen(SetupScreen(self.s.server) if action == "new_chat" else ResumeScreen(archived=action == "archive"))
        else:
            await self.add("note", f'/{name}: no action "{action}" (use {", ".join(data.ACTIONS)})')

    def pick(self, title, options, current, fn, multi=False):
        async def apply(v):
            try:
                await fn(v)
            except Exception as e:                          # noqa: BLE001
                await self.add("error", f"{title}: {type(e).__name__}: {e}")
        def done(v):
            if v is not None:
                self.app.run_worker(apply(v))
            if isinstance(self.focused, StatusBar):          # chosen (or given up) from the status line: the keys go back to the input
                self.query_one(ChatInput).focus()
        self.app.push_screen(Pick(title, options, current, multi), done)

    async def report(self, r: core.Result) -> bool:
        """Show what a command of the core said: a note, or its error (then the input line stays, see run_acts)."""
        if not r.ok:
            await self.add("error", r.text)
            self.bad = True
            return False
        self.refresh_ui()
        await self.add("note", r.text)
        return True

    async def cmd_mode(self, arg: str):
        ms = data.modes(self.home)
        if not arg and not ms:
            await self.add("error", f"mode: {data.modes_error(self.home)}")
            self.bad = True
            return
        if not arg:
            return self.pick("mode", [(k, x.label, x.text, True) for k, x in ms.items()], self.s.mode, self.cmd_mode)
        await self.report(core.command(self.home, self.s, "mode", arg))

    async def cmd_model(self, arg: str):
        opts = data.model_options(self.home, self.s.account)
        if not arg:
            return self.pick("model", [(n, n, note, True) for n, note in opts], self.s.model, self.cmd_model)
        await self.report(core.command(self.home, self.s, "model", arg))

    async def cmd_account(self, arg: str):
        accounts = data.safe(lambda: self.home.load_providers().accounts, {})
        if not arg:
            return self.pick("account", [(n, n, data.account_note(self.home, n, a), True)
                                         for n, a in accounts.items()], self.s.account, self.cmd_account)
        await self.report(core.command(self.home, self.s, "account", arg))

    async def cmd_effort(self, arg: str):
        known = data.efforts()
        if not arg:
            return self.pick("effort", [(e, e, data.EFFORT_TEXT.get(e, ""), True) for e in known], self.s.effort, self.cmd_effort)
        await self.report(core.command(self.home, self.s, "effort", arg))

    async def cmd_ultracode(self, arg: str):
        """/ultracode [on|off|status]: the level for this chat (core.ultracode_command); alone it switches it on."""
        await self.report(core.command(self.home, self.s, "ultracode", arg))

    async def upload_from(self, box: ChatInput):
        """Enter on a line that holds /upload or /datei: the word leaves the line and the files chosen take its place."""
        if self.s.readonly:
            return await self.add("note", self.read_only_text())
        text, at = sl.take_out(box.value)
        box.value = text
        box.cursor_position = at
        self.app.run_worker(self.attach())                    # a worker of its own: the dialog must not hold up the screen's messages

    async def attach(self):
        """Open the file dialog (in a worker thread, the UI keeps running), copy the files into the run folder and put one
        `[Anhang: path]` line per file into the input at the cursor.  Cancel changes nothing."""
        if self.picking:
            return await self.add("note", "Der Datei-Dialog ist schon offen.")
        self.picking = True
        try:
            await self.attach_picked()
        except Exception as e:                                # noqa: BLE001 - a bug here must not take the UI down
            await self.add("error", f"/upload: {type(e).__name__}: {e}")
        finally:
            self.picking = False

    async def attach_picked(self):
        """The dialog, the copy and the lines in the input (see `attach`)."""
        got = await asyncio.to_thread(upload.pick, upload.start_folder(self.upload_dir))
        if got.notice:
            return await self.add("note", got.notice)
        if not got.files:
            return
        self.upload_dir = got.files[0].parent
        done = await asyncio.to_thread(upload.attach, got.files, self.s.run.path, self.home.root)
        for note in done.notes:
            await self.add("note", note)
        if not done.lines:
            return
        box = self.query_one(ChatInput)
        box.value, at = upload.insert_lines(box.value, box.cursor_position, done.lines)
        box.cursor_position = at
        box.focus()
        await self.add("note", upload.confirmation(len(done.lines)))

    async def cmd_upload(self, arg: str):
        """/upload (alias /datei): pick files and attach them to the next message."""
        self.app.run_worker(self.attach())

    async def cmd_datei(self, arg: str):
        """/datei: same as /upload."""
        await self.cmd_upload(arg)

    async def cmd_rename(self, arg: str):
        """/rename <title>: the chat's title by hand; alone: automatic again."""
        await self.report(core.command(self.home, self.s, "rename", arg))

    async def cmd_describe(self, arg: str):
        """/describe <text>: the chat's description by hand; alone: automatic again."""
        await self.report(core.command(self.home, self.s, "describe", arg))

    async def cmd_tools(self, arg: str):
        its = data.items(self.home)
        on = {i.name for i in its} - set(self.s.state.tools_removed)
        if not arg:
            return self.pick("tools", [(i.name, i.name, i.kind, True) for i in its], on, self.apply_tools, multi=True)
        await self.report(core.tools_change(self.home, self.s, arg))

    async def apply_tools(self, on):
        await self.report(core.apply_tools(self.home, self.s, on))

    def which(self, arg: str, kind: str) -> str | None:
        """The account of /login and /key: the one named, else the one of this chat (else the first that needs it).
        The text typed is never echoed: a key pasted here by mistake must not land in the transcript."""
        names = list(data.safe(lambda: self.home.load_providers().accounts, {}))
        if arg:
            return arg if arg in names else None
        return self.s.account if self.s.account in names else next((n for n in names if acts.needs(self.home, n) == kind), None)

    async def cmd_settings(self, arg: str):
        """/settings (alias /config): the settings screen, with this chat waiting below it."""
        await self.go("settings")

    async def cmd_config(self, arg: str):
        await self.go("settings")

    async def cmd_login(self, arg: str):
        name = self.which(arg.strip(), "login")
        if name is None:
            return await self.add("error", "login: unknown account")
        ok, msg = acts.login(self.app, name)
        await self.add("note" if ok else "error", msg)
        self.refresh_ui()

    async def cmd_key(self, arg: str):
        name = self.which(arg.strip(), "key")
        if name is None:
            return await self.add("error", "key: unknown account")
        acts.ask_key(self.app, name)

    async def cmd_server(self, arg: str):
        name = arg.strip()
        if not name:
            return await self.add("note", f"server: this chat runs on {self.s.server}; `/server <name>` opens the permanent session on another server")
        if name == self.s.server:
            return await self.add("note", f"server: this chat already runs on {name}")
        ok, msg = acts.remote(self.app, name)
        await self.add("note" if ok else "error", msg)

    async def cmd_accounts(self, arg: str):
        await self.go("accounts")

    async def cmd_reload(self, arg: str):
        """/reload: read lightmind.md, config.md, permissions.md, the agent, skills and accounts again; same history.  While
        a turn runs it waits for the end of it (`drained` does it first, before anything that waited goes out)."""
        if self.busy:
            self.reloading = True
            return await self.add("note", "reload: waits for the end of this answer")
        r, self.sig = await asyncio.to_thread(core.reload, self.home, self.s, self.sig)
        await self.show_reload(r)

    async def show_reload(self, r):
        self.cat = None
        if not r.ok:
            self.bad = True
        await self.add("note" if r.ok else "error", r.text)
        self.refresh_ui()

    async def cmd_clear(self, arg: str):
        await self.go("new_chat")

    async def cmd_new(self, arg: str):
        await self.go("new_chat")

    async def cmd_resume(self, arg: str):
        await self.go("resume")

    async def cmd_archive(self, arg: str):
        """/archive: close this chat, mark it archived and go back to the start menu (Ctrl+Q does it and quits).
        `/archive list` shows the archive, `/archive restore <chat>` puts a chat back into the resume list.  Any other word
        archives nothing: a typo must not close the chat."""
        word, _, ref = arg.strip().partition(" ")
        word, ref = word.lower(), ref.strip()
        if not word:
            return await self.archive(leave=True)
        if word == "list" and not ref:
            return await self.add("note", await asyncio.to_thread(resume.archive_text, self.home))
        if word == "restore" and ref:
            try:
                run_id, say = await asyncio.to_thread(resume.restore, self.home, ref)
            except ValueError as e:
                await self.add("error", f"/archive restore: {e}")
                self.bad = True
                return
            self.arc_choices = None
            return await self.add("note", f"{run_id}: {say}" + (" - it is in the resume list again (/resume, then Enter)" if say == "restored" else ""))
        await self.add("error", "/archive archives this chat; /archive list shows the archive; /archive restore <chat> brings one back")
        self.bad = True

    async def archive(self, leave: bool = False, by: str = "tui") -> str:
        """Stop the turn, close the chat (its link too) and mark it archived (`runs.archive`; the folder stays where it is).
        `leave`: go back to the start menu (the app ends when this chat was opened directly), else the caller quits the app.
        Returns the line that tells where it went.  A chat that is only read cannot be archived from here."""
        if self.s.readonly:
            self.bad = True
            await self.add("note", self.read_only_text())
            return ""
        if self.leaving:
            return ""
        self.leaving = True
        sid, title = self.s.id, data.clean(self.s.title or self.topic)
        await self.finish()
        try:
            await asyncio.to_thread(runs.archive, self.home, sid, by)
            line = f"archived: {title} - restore with: lm archive restore {sid}"
        except ValueError as e:                               # the chat is closed by now; the reason is said
            line = f"could not archive {sid}: {e}"
        if not leave or self.app.direct is not None:
            self.app.exit_lines.append(line)
        if leave:
            self.app.notify(line, timeout=6)
            await self.app.back()
        return line

    # ----------------------------------------------------------------- Remote Control
    async def cmd_remote_control(self, arg: str):
        """/remote-control: the long name of /rc (the dispatcher turns the hyphen into an underscore)."""
        await self.cmd_rc(arg)

    async def cmd_rc(self, arg: str):
        """/rc [name]: open the Remote Control link of this chat (a phone can read and write it); /rc off closes it."""
        arg = arg.strip()
        if self.s.readonly:
            self.bad = True
            return await self.add("note", self.read_only_text())
        if arg.lower() == "off":
            if self.rc is None:
                return await self.add("note", "Remote Control is not on in this chat")
            return await self.rc_stop("off")
        if not rc.enabled(self.home):
            self.bad = True
            return await self.add("error", "Remote Control is off in config.md (remote: enabled: true)")
        if self.rc is not None:
            return await self.add("note", f"Remote Control is already on as {self.rc.name}; /rc off ends it")
        name = rc.clean_text(arg, 80) or rc.clean_text(self.s.title, 80) or data.short_id(self.s.id)
        try:
            await self.rc_open(name, auto=False)
        except Exception as e:                                # noqa: BLE001 - a socket that cannot open is said, the chat goes on
            self.bad = True
            return await self.add("error", f"Remote Control: {type(e).__name__}: {e}")

    async def rc_open(self, name: str, auto: bool):
        """Open the link under `name` and wire the chat to it.  `auto` (from `remote.auto`) says so in the link file so the
        phone is not pushed, and keeps the notes quiet.  Raises when the socket cannot open."""
        async with self.rc_lock:
            if self.rc is not None:                          # the other opener was first
                return
            await self._rc_open(name, auto)

    async def _rc_open(self, name: str, auto: bool):
        link = await rc.Link.start(self.home, self.s, name, "tui", handler=self.rc_inbound, on_close=self.rc_closed, auto=auto)
        self.rc = link
        self.rc_prev = (self.s.asker, self.s.questioner)     # questions go to the phone too, whoever answers first wins
        self.s.asker, self.s.questioner = self.rc_asker, self.rc_questioner
        if self.busy:                                         # joined in the middle of a turn
            link.turn_start()
        link.sync(self.busy, len(self.queue))
        self.refresh_ui()
        await self.add("note", f"Remote Control on: {name}")
        self.set_timer(rc.NODE_WAIT, lambda: self.rc_fallback(link))

    async def rc_auto(self):
        """`remote.auto`: open the link like a bare `/rc` once the chat is up.  Whatever goes wrong is a quiet note at most;
        the chat is never marked bad for it."""
        try:
            if self.s.readonly or self.rc is not None or self.rc_wanted is not None or not rc.auto_enabled(self.home):
                return
            await self.rc_open(rc.clean_text(self.s.title, 80) or data.short_id(self.s.id), auto=True)
        except Exception as e:                                # noqa: BLE001 - a failed auto start must not disturb the chat
            with suppress(Exception):
                await self.add("note", f"Remote Control did not start by itself: {type(e).__name__}: {e}")

    async def rc_fallback(self, link: rc.Link):
        """Nobody connected to the link in time (no gateway, no node): say so and send the sentence by Telegram or ntfy."""
        if link is not self.rc or link.closed or link.ever_connected or link.auto:      # auto: no gateway is no news, no message
            return
        await self.add("note", "Remote Control is on, but no gateway is connected")
        from .. import watch
        with suppress(Exception):
            await asyncio.to_thread(watch.notify, self.home, rc.PUSH_TEXT.format(name=link.name), f"rc-{link.s.id}")

    async def rc_stop(self, reason: str):
        """Close the link (if there is one); `rc_closed` does the rest."""
        if self.rc is not None:
            await self.rc.close(reason)

    def rc_closed(self, reason: str):
        """The link has ended (asked to, the phone said so, or the chat ends): the session asks only here again."""
        if self.rc_prev is not None:
            self.s.asker, self.s.questioner = self.rc_prev
            self.rc_prev = None
        self.rc = None
        if reason != "ended":
            with suppress(Exception):
                self.refresh_ui()
                self.app.run_worker(self.add("note", "Remote Control off"))

    def rc_sync(self):
        """The link learns what changed: model, effort, mode, account (`status`) and whether a turn runs or messages wait (`busy`)."""
        if self.rc is not None and not self.rc.closed:
            self.rc.sync(self.busy, len(self.queue))

    def rc_tell(self, type_: str, **fields):
        if self.rc is not None:
            self.rc.emit(type_, **fields)

    async def rc_asker(self, question, check) -> str:
        """The asker of the rules while the link is on: the modal on the keyboard and the question on the phone, first answer wins."""
        prev = self.rc_prev[0] if self.rc_prev else None
        local = prev(question, check) if prev is not None else None
        if self.rc is None or self.rc.closed:
            return await local if local is not None else "no"
        return await self.rc.ask("permission", str(question), str(getattr(check, "arg", "") or ""), ["yes", "always", "no"], "no", local)

    async def rc_questioner(self, question) -> str:
        """The `ask` tool while the link is on: a text field on the keyboard and the question on the phone."""
        prev = self.rc_prev[1] if self.rc_prev else None
        local = prev(question) if prev is not None else self.ask_text(str(question))
        if self.rc is None or self.rc.closed:
            return await local
        return await self.rc.ask("ask", str(question), "", [], "(no answer: nobody answered in time)", local)

    async def ask_text(self, question: str) -> str:
        """A free answer on the keyboard (the `ask` tool has no other asker in the terminal)."""
        done = asyncio.get_running_loop().create_future()
        modal = TextAskModal(question)
        self.app.push_screen(modal, lambda r: done.done() or done.set_result(r or ""))
        try:
            return await done
        finally:
            if self.app.screen is modal:
                modal.dismiss(None)

    async def rc_inbound(self, msg: dict) -> dict | None:
        """A request of the link that is not answered there: send, cancel, session.set, session.archive.  Runs on this loop,
        one at a time.  `rc.Refused` tells the device why not."""
        t, src = msg["type"], msg.get("from") or "phone"
        if self.leaving or self.s.closed:
            raise rc.Refused("this chat is closing")
        if t == "send":
            text = msg.get("text")
            if not isinstance(text, str) or not text.strip():
                raise rc.Refused("send: `text` is a string that is not empty")
            if text.strip().lower() == "/archive":
                return self.rc_archive(src)
            return await self.submit_line(text.strip(), src)
        if t == "cancel":
            if self.busy and not self.stopping:
                self.stopping = True
                self.job.cancel()
            return {}
        if t == "session.archive":
            return self.rc_archive(src)
        if t == "session.set":
            kw = {k: msg[k].strip() for k in ("model", "effort", "mode") if isinstance(msg.get(k), str) and msg[k].strip()}
            if not kw:
                raise rc.Refused("session.set: give a model, an effort or a mode")
            said = []
            for k, v in kw.items():
                r = await asyncio.to_thread(core.command, self.home, self.s, k, v)
                if not r.ok:
                    raise rc.Refused(r.text)
                said.append(r.text)
            self.refresh_ui()
            await self.add("note", f"{src}: " + " · ".join(said))
            return {"status": self.rc.status_fields() if self.rc is not None else {}}
        raise rc.Refused(f"unknown type {t[:60]!r}")

    def rc_archive(self, src: str) -> dict:
        """A device asked to archive: it happens in a worker, so the link can still deliver the answer first."""
        self.app.run_worker(self.archive(leave=True, by="phone"))
        return {"text": "archiving"}

    async def cmd_quit(self, arg: str):
        await self.go("quit")

    async def cmd_exit(self, arg: str):
        await self.go("quit")
