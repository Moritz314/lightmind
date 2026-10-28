"""Slash words anywhere in a message: the one parser of the Orbit chat and `lm chat` (plan `plan/PLAN-SLASH.md`).

A slash word starts with `/` at the start of the input or after whitespace and stands outside code (`...` and
``` blocks), so `a/b`, `https://x` and `C:/x` are none.  The `Catalog` of the surface says what a word is:

    /arsenal       a skill: stays in the text and is loaded for this turn (several, each once, in order)
    /effort high   a command that changes a setting: it runs, it and its one word are cut out of the text
    /clear         a command that ends or leaves the chat: alone or last; with text before it, it runs after the answer
    /upload        a command that puts something into the input (a file dialog): anywhere, takes no word, the surface
                   cuts the word out of the line and inserts there, nothing is sent
    /tools -pdf    a command that takes the rest of the line: only first
    //word         the plain text /word
    /unknown       plain text (only an input of nothing but one unknown word is refused: a typo of a command)

`parse` runs nothing and reads no tree.  The surface runs `Parsed.acts` in order, sends `Parsed.text` (`message` puts
the skills in front), then runs `Parsed.then`.  With `Parsed.error` set it does none of that, shows the error and keeps
the input.  May import: context (late, for the text of a skill).  Nothing else.
"""
from __future__ import annotations

import re
import sys
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .files import Home

COMMANDS = ("mode", "model", "account", "effort", "context", "ultracode")     # change a setting of the chat: anywhere, with one word
#                                                                 (`context` counts once a surface has it; a new built-in
#                                                                 that is in none of these lists is one that leaves the chat)
LINE = ("tools", "skill", "rename", "describe")                                        # take the rest of the line: only first
INSERT = ("upload", "datei")                                     # put text into the input (the files of a dialog): nothing is sent
WORD = ("server", "login", "key", "archive")                     # of the commands that leave the chat, those with a name (`archive`: list | restore <chat>)
EFFORTS = ("low", "medium", "high", "xhigh", "max")              # claude_sdk.EFFORTS; its own list wins once it is loaded
ULTRACODE_WORDS = ("on", "off", "status")                         # what `/ultracode` takes (alone it means on)
ULTRACODE_ARG = re.compile(r"(?:^|\s)/ultracode[ \t]+(\w*)$", re.I)     # the cursor is in the word after /ultracode: completion offers ULTRACODE_WORDS
PUNCT = ",.;:!?)"                                                # may follow a skill word: "with /arsenal, please"

_NAME = re.compile(r"\w[\w:.-]*[,.;:!?)]*")      # what `//word` may be: the shape of a command, so `//host/share` stays
_LONE = re.compile(r"/[\w:.-]*")                 # an input that is one word shaped like a command
_HAS = re.compile(r"(?:^|\s)/")
_FENCE = re.compile(r" {0,3}(`{3,})[^`]*")       # a line that opens a fenced block ...
_SHUT = re.compile(r" {0,3}(`{3,})[ \t]*")       # ... and one that closes it


@dataclass
class Take:
    """The word after a command.  `known()` lists what it takes (empty: any word); `loose(word, prose)` lets an
    unlisted word through, `prose` being whether more than slash words follow it."""
    known: Callable[[], Sequence[str]] = tuple
    loose: Callable[[str, bool], bool] = lambda word, prose: False

    def check(self, word: str, prose: bool) -> str | None:
        """The word as the command knows it (a hit ignores case), None when it is refused."""
        names = self.known()
        if not names:
            return word
        return next((n for n in names if n.lower() == word.lower()), word if self.loose(word, prose) else None)


@dataclass
class Catalog:
    """What the words of one surface are: its own `builtin` commands, the `skills` (command names), the `rows`
    (more commands that end, leave or switch the chat: `ui.slash`; they win over a built-in) and how `takes` checks
    the word of a command."""
    builtin: Sequence[str] = ()
    skills: Collection[str] = ()
    rows: Collection[str] = ()
    takes: dict[str, Take] = field(default_factory=dict)

    def kind(self, name: str) -> str:
        """command | line | insert | session | skill, "" for a word that is none of them."""
        if name in self.rows:
            return "session"
        if name in self.builtin:
            return ("command" if name in COMMANDS else "line" if name in LINE else "insert" if name in INSERT
                    else "session")
        return "skill" if name in self.skills else ""


@dataclass(frozen=True)
class Act:
    name: str
    arg: str = ""
    kind: str = "command"                                   # command | session | line | insert

    @property
    def line(self) -> str:
        return f"/{self.name} {self.arg}".rstrip()


@dataclass
class Parsed:
    text: str = ""                                          # what is sent: the input without the commands, `//` undone
    acts: list[Act] = field(default_factory=list)           # the commands that run first, as they stand
    skills: list[str] = field(default_factory=list)         # the skills named, once each, as they stand
    then: Act | None = None                                 # a command that leaves the chat and stood last: after the answer
    error: str = ""                                         # not empty: nothing runs or is sent, the input stays
    unknown: bool = False                                   # ... and it is only an unknown command: a note, no failure


# ------------------------------------------------------------------ where the words are
def has_slash(text: str) -> bool:
    """Is there a word that starts with `/`?  (Cheap: the typed line is only parsed when there is.)"""
    return _HAS.search(text) is not None


def code_spans(text: str) -> list[tuple[int, int]]:
    """Where the code is: fenced blocks (an open one runs to the end) and inline spans whose backtick runs match in
    length.  Backticks without a partner are plain text."""
    spans, pos, fence = [], 0, None
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        if fence is None:
            if m := _FENCE.fullmatch(body):
                fence = (pos, len(m[1]))
        elif (m := _SHUT.fullmatch(body)) and len(m[1]) >= fence[1]:
            spans.append((fence[0], pos + len(line)))
            fence = None
        pos += len(line)
    if fence:
        spans.append((fence[0], len(text)))
    free, last = [], 0
    for a, b in sorted(spans):
        free.append((last, a))
        last = b
    free.append((last, len(text)))
    for a, b in free:
        runs = [m.span() for m in re.finditer(r"`+", text[a:b])]
        k = 0
        while k < len(runs):
            n = runs[k][1] - runs[k][0]
            j = next((j for j in range(k + 1, len(runs)) if runs[j][1] - runs[j][0] == n), None)
            if j is None:
                k += 1
            else:
                spans.append((a + runs[k][0], a + runs[j][1]))
                k = j + 1
    return sorted(spans)


def _in(spans, pos: int) -> bool:
    return any(a <= pos < b for a, b in spans)


def word_at(text: str, pos: int) -> tuple[int, int] | None:
    """The slash word the cursor is in or at the end of, as (start, end): the word starts with a single `/`, at the
    start of the text or after whitespace, and stands outside code.  What the suggestions are made from."""
    pos = max(0, min(pos, len(text)))
    a = pos
    while a > 0 and not text[a - 1].isspace():
        a -= 1
    b = pos
    while b < len(text) and not text[b].isspace():
        b += 1
    if a == b or text[a] != "/" or text[a:a + 2] == "//" or _in(code_spans(text), a):
        return None
    return a, b


def complete(text: str, a: int, b: int, name: str) -> tuple[str, int]:
    """`text` with the word [a, b) replaced by `/name` (and a blank behind it at the end of the text), and where
    the cursor goes: behind the blank, ready for the next word."""
    tail = text[b:]
    gap = " " if not tail else ""
    return text[:a] + "/" + name + gap + tail, a + 1 + len(name) + (1 if gap or tail[:1] in (" ", "\t") else 0)


# ------------------------------------------------------------------ the parser
def _cut(text: str, a: int, b: int) -> str:
    """`text` without [a, b) and the blanks behind it; a command that ends its line takes the blanks before it too,
    and a line that held nothing else goes with its newline."""
    while b < len(text) and text[b] in " \t":
        b += 1
    if b == len(text) or text[b] == "\n":
        while a > 0 and text[a - 1] in " \t":
            a -= 1
        if (a == 0 or text[a - 1] == "\n") and b < len(text):
            b += 1
    return text[:a] + text[b:]


def _fail(error: str) -> Parsed:
    return Parsed(error=error)                                           # nothing of what was found before runs


def parse(text: str, cat: Catalog) -> Parsed:
    """The slash words of one input (see the module doc)."""
    p = Parsed()
    text = text.replace("\r\n", "\n")
    orig = text.strip()
    code = code_spans(text)
    words = [m.span() for m in re.finditer(r"\S+", text)]
    cuts: list[tuple[int, int, bool]] = []      # what leaves the text; True: with the blanks around it, False: one character
    asked: list[tuple[int, str, int]] = []      # commands with a word: (index in p.acts, name, where the word ends)
    i = 0
    while i < len(words):
        a, b = words[i]
        i += 1
        word = text[a:b]
        if word[0] != "/" or _in(code, a):
            continue
        if word.startswith("//"):                                       # `//word` is the plain text /word
            if _NAME.fullmatch(word[2:]):
                cuts.append((a, a + 1, False))
            continue
        name = word[1:].lower()
        kind = cat.kind(name)
        if not kind and (core := name.rstrip(PUNCT)) != name and cat.kind(core) == "skill":
            name, kind = core, "skill"                                  # "with /arsenal, please"
        if kind == "skill":
            if name not in p.skills:
                p.skills.append(name)
        elif kind == "line":
            if text[:a].strip():
                return _fail(f"/{name} takes the rest of the line, so it must come first (//{name} writes it as text)")
            p.acts.append(Act(name, text[b:].strip(), "line"))
            return p                                                    # nothing after it is a slash word
        elif kind:                                                      # a command: a word of its own may follow
            arg, end = "", b
            if i < len(words) and (kind == "command" or name in WORD):
                x, y = words[i]
                if "\n" not in text[b:x] and text[x] != "/" and not _in(code, x):
                    arg, end, i = text[x:y], y, i + 1
                    if name == "archive" and arg.lower() == "restore" and i < len(words):      # the chat to bring back is the second word
                        x, y = words[i]
                        if "\n" not in text[end:x] and text[x] != "/" and not _in(code, x):
                            arg, end, i = f"{arg} {text[x:y]}", y, i + 1
            if kind == "session":
                if text[end:].strip():
                    return _fail(f"/{name} ends or leaves the chat, so it goes last or alone; nothing was sent "
                                    f"(//{name} writes it as text)")
                cuts.append((a, end, True))
                p.then = Act(name, arg, "session")
                break
            cuts.append((a, end, True))
            if arg:
                asked.append((len(p.acts), name, end))
            p.acts.append(Act(name, arg, "insert" if kind == "insert" else "command"))
    for k, name, end in asked:                  # now it is known what is a command and what is prose
        prose = any(not text[x:y].startswith("/") and not any(s <= x < e for s, e, _ in cuts) for x, y in words if x >= end)
        take = cat.takes.get(name) or Take()
        got = take.check(p.acts[k].arg, prose)
        if got is None:
            return _fail(f"{name}: unknown {p.acts[k].arg!r}; use {', '.join(take.known())}")
        p.acts[k] = Act(name, got)
    if not cuts and not p.skills and _LONE.fullmatch(orig):             # one word and nothing else: a typo of a command
        p.error, p.unknown = f"unknown command {orig}", True
        return p
    for a, b, tidy in sorted(cuts, reverse=True):
        text = _cut(text, a, b) if tidy else text[:a] + text[a + 1:]
    p.text = text.strip()
    return p


def take_out(text: str, names: Sequence[str] = INSERT) -> tuple[str, int]:
    """`text` without the words `/upload` and `/datei` (any of `names`, outside code) and where the first of them stood,
    which is where a surface puts what the command brings.  Without such a word the text and its end."""
    code = code_spans(text)
    found = [m.span() for m in re.finditer(r"\S+", text)
             if m[0][0] == "/" and m[0][1:].lower() in names and not _in(code, m.start())]
    if not found:
        return text, len(text)
    first = found[0][0]
    for a, b in reversed(found):
        text = _cut(text, a, b)
    pos = min(first, len(text))
    while pos > 0 and text[pos - 1] in " \t" and (pos == len(text) or text[pos] == "\n"):
        pos -= 1                                                    # `_cut` took the blanks before a word that ended its line
    return text, pos


def summary(p: Parsed) -> str:
    """What a parse found, in one line: `model opus · effort high · skills: arsenal · then clear`.  The word of a
    command that leaves the chat is not shown (`/key` may have one that is secret)."""
    parts = [f"{a.name} {a.arg}".strip() if a.kind == "command" else a.name for a in p.acts]
    if p.skills:
        parts.append("skills: " + ", ".join(p.skills))
    if p.then:
        parts.append("then " + p.then.name)
    return " · ".join(parts)


def message(home: Home, p: Parsed, skills: Mapping[str, object]) -> str:
    """The user message of a parse: for each skill the instruction to use it (`context.skill_message`), then the text."""
    from .context import skill_message
    return "\n\n".join(filter(None, [*(skill_message(home, skills[n]) for n in p.skills), p.text]))


# ------------------------------------------------------------------ what the commands accept
def efforts() -> tuple[str, ...]:
    """What `/effort` takes: claude_sdk's own list once that module is loaded (importing the SDK takes seconds)."""
    known = tuple(getattr(sys.modules.get("lightmind.providers.claude_sdk"), "EFFORTS", EFFORTS))
    return known + (("ultracode",) if "ultracode" not in known else ())      # no provider knows it: max plus workflows (ultracode.py)


def models(home: Home, account: str) -> list[str]:
    """The names `/model` knows for an account: its models, its default, its tiers and what the tiers point at."""
    try:
        a = home.load_providers().accounts.get(account)
    except Exception:                                        # noqa: BLE001 - a half-edited providers file: any word then
        a = None
    if a is None:
        return []
    return [n for n in dict.fromkeys([*a.models, a.default_model, *a.tiers, *a.tiers.values()]) if n]


def model_ok(word: str, prose: bool) -> bool:
    """An unlisted word is still a model when nothing but slash words follows it (as `/model x` always was) or when it
    looks like an id (`claude-opus-4-7`, `gpt-5`, `llama3:8b`); the prose that follows a bare `/model` is not."""
    return not prose or re.search(r"[\d._:/-]", word) is not None
