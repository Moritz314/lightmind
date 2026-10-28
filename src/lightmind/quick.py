"""The quick chat, `lm q`: what it opens with.

A flag wins over the `quick:` section of config.md, which wins over the built-in value; a missing section, a missing key
and an empty key all mean the built-in value (an older tree has no `quick:` section until its owner adds one).  The
chat itself is an ordinary one: `cli` hands the result to `Orbit(direct=...)` as `lm open new` does.  It gets no
session name of its own: title and description come from the content (`titles.py`) like in every other chat; only `-n`
names it.

May import: files.
"""
from __future__ import annotations

from typing import Any, Mapping

from .files import Home, parse_size

BUILTIN: dict[str, str] = {"model": "opus", "effort": "high", "context": "500k", "mode": "bypass"}


def _config(home: Home) -> Mapping[str, Any]:
    sec = home.settings.get("quick")
    return sec if isinstance(sec, Mapping) else {}


def quick_settings(home: Home, model: str | None = None, effort: str | None = None,
                   context: str | int | None = None, mode: str | None = None,
                   agent: str | None = None) -> dict[str, Any]:
    """`{model, effort, context, mode}` of a quick chat: argument, else `quick:` of config.md, else the built-in value.
    `quick.permission_mode` counts as `quick.mode`.  With an `agent` the quick values do not apply (the agent's own model,
    effort and mode would be overridden): only the arguments are set, the rest is None and the usual order decides
    (agent, then defaults).  ValueError for a context that is no size."""
    cfg = _config(home)
    def pick(arg: Any, *keys: str) -> Any:
        for v in (arg, *([] if agent else [cfg.get(k) for k in keys])):
            if v is not None and str(v).strip():
                return v if not isinstance(v, str) else v.strip()
        return None if agent else BUILTIN[keys[0]]
    out = {"model": pick(model, "model"), "effort": pick(effort, "effort"),
           "context": pick(context, "context"), "mode": pick(mode, "mode", "permission_mode")}
    if out["context"] is not None:
        parse_size(out["context"])                   # a typo is said before a chat opens, not by the chat
    return out
