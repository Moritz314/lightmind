"""Small helpers that several modules used to carry their own copy of.  Imports nothing of the core."""
from __future__ import annotations

from typing import Any, Mapping


def to_int(v: Any, default: int = 0) -> int:
    """`int(v)`, or `default` when it is not a number."""
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def setting(home: Any, dotted: str, default: Any = None) -> Any:
    """`a.b` from `home.settings` (a flat 'a.b' key or nested maps); `default` when absent."""
    cur: Any = getattr(home, "settings", None) or {}
    if isinstance(cur, Mapping) and dotted in cur:
        return cur[dotted]
    for part in dotted.split("."):
        if not isinstance(cur, Mapping) or part not in cur:
            return default
        cur = cur[part]
    return cur


def brief(args: dict, limit: int = 120) -> str:
    """One short line naming what a tool call is about: its most telling string argument."""
    for k in ("command", "path", "name", "question", "action", "ref"):
        if isinstance(args.get(k), str):
            v = args[k]
            break
    else:
        v = next((x for x in args.values() if isinstance(x, str)), "")
    v = " ".join(v.split())
    return v if len(v) <= limit else v[:limit - 3] + "..."
