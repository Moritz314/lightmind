"""Tool calls of the transcript, grouped: the plain-words summary of a run of calls and what the running one is doing.

Pure functions (no widgets): chat.py's `ToolHead` shows what they return."""
from __future__ import annotations

from collections.abc import Iterable

# category -> (tool names, singular, plural, what is going on while one runs); the order is the order of the list
CATEGORIES: dict[str, tuple[frozenset[str], str, str, str]] = {
    "read": (frozenset({"read", "read_file", "notebookread", "view"}), "file read", "files read", "reading a file"),
    "search": (frozenset({"glob", "grep", "toolsearch", "search", "find", "ls", "list_files"}), "search", "searches", "searching"),
    "command": (frozenset({"bash", "bashoutput", "killshell", "killbash", "monitor", "shell", "run_command"}),
                "command", "commands", "running a command"),
    "edit": (frozenset({"edit", "multiedit", "write", "notebookedit", "write_file", "edit_file"}), "edit", "edits", "editing a file"),
    "web": (frozenset({"webfetch", "websearch"}), "web lookup", "web lookups", "looking something up on the web"),
    "agent": (frozenset({"task", "agent"}), "sub-agent", "sub-agents", "a sub-agent is working"),
    "todo": (frozenset({"todowrite", "todoread"}), "to-do update", "to-do updates", "updating the to-dos"),
}
OTHER = ("other call", "other calls", "using a tool")


def category(name: str) -> str:
    """The group a tool belongs to ("other" when it is none of the known ones); a sub-agent card starts with ◆."""
    n = str(name).strip().lower()
    if n.startswith("◆"):
        return "agent"
    return next((k for k, v in CATEGORIES.items() if n in v[0]), "other")


def _label(cat: str, n: int) -> str:
    one, many = CATEGORIES[cat][1:3] if cat in CATEGORIES else OTHER[:2]
    return f"{n} {one if n == 1 else many}"


def summary(names: Iterable[str]) -> str:
    """"5 tool calls: 3 files read, 1 command, 1 edit" - counted by kind, in the order the kinds first appeared."""
    names = list(names)
    counts: dict[str, int] = {}
    for n in names:
        c = category(n)
        counts[c] = counts.get(c, 0) + 1
    total = len(names)
    return f"{total} tool call{'' if total == 1 else 's'}: " + ", ".join(_label(c, k) for c, k in counts.items())


def doing(name: str, target: str = "") -> str:
    """What a running call does, in words (never the raw command): "reading foo.py", "running a command"."""
    c = category(name)
    phrase = CATEGORIES[c][3] if c in CATEGORIES else OTHER[2]
    return phrase.replace("a file", target) if target and c in ("read", "edit") else phrase
