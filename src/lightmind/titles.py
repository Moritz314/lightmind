"""Chat titles and descriptions: by hand (`/rename`, `/describe`) or automatically from the newest work of the chat.

A chat drifts away from its first purpose, so the automatic name never comes from the first message: one cheap model
call reads the last user messages, the open to-dos and the previous topics and answers `{title, topics[]}`.  The title
is the current topic (max 5 words), the description lists all topics newest first (`a · b · c`, max 100 characters).
It runs in a thread after the first finished turn, then every `sessions.title_every` turns (default: every turn) and when the chat ends; a
failure is one line in the log and never reaches the chat.  A value set by hand (`*_manual`) is never overwritten.

Stored in the run's meta.json (`model.Run`: title, description, title_manual, description_manual).
May import: model, todos (late: providers).
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from typing import Any, Callable

from . import todos
from .model import Error, Message, Request, Text

log = logging.getLogger("lightmind.titles")

DEFAULTS = {"auto_title": True, "title_model": "haiku", "title_every": 1}
TITLE_WORDS, TITLE_CHARS = 5, 50
TOPIC_WORDS, TOPIC_CHARS = 3, 30
DESC_CHARS = 100
SEP = " · "
HISTORY_MESSAGES, MESSAGE_CHARS, TODO_TITLES = 10, 300, 12
CALL_TIMEOUT = 60.0

SYSTEM = """You name a chat between a person and an AI assistant. A chat drifts: judge by the NEWEST work, not the first message.
Answer with one JSON object and nothing else: {"title": "...", "topics": ["...", "..."]}
- title: at most 5 words, about the CURRENT, most recent topic. No quotes, no final period.
- topics: the different topics of the whole chat, newest first, each 1-3 words, at most 6 topics.
- Keep the wording of the previous topics when they are still right, so names stay stable.
- Write in the language of the user's messages (mostly German)."""


# ------------------------------------------------------------------ settings and the update rule
def settings(home) -> dict[str, Any]:
    """`sessions:` of lightmind.md with the defaults filled in (a wrong value falls back to its default)."""
    raw = (getattr(home, "settings", None) or {}).get("sessions")
    cfg = {**DEFAULTS, **(raw if isinstance(raw, dict) else {})}
    try:
        cfg["title_every"] = max(1, int(cfg["title_every"]))
    except (TypeError, ValueError):
        cfg["title_every"] = DEFAULTS["title_every"]
    cfg["auto_title"] = cfg["auto_title"] is not False
    cfg["title_model"] = str(cfg["title_model"] or DEFAULTS["title_model"])
    return cfg


def due(done: int, titled_at: int, every: int) -> bool:
    """True after the first finished turn and then whenever `every` turns passed since the last naming."""
    return done >= 1 and (titled_at == 0 or done - titled_at >= every)


# ------------------------------------------------------------------ the model's answer
def _words(text: str, words: int, chars: int) -> str:
    cut = " ".join(str(text).replace("\n", " ").split()[:words]).strip(" \"'.,;:·-")
    return cut[:chars].rstrip(" \"'.,;:·-")


def parse_answer(text: str) -> tuple[str, list[str]] | None:
    """`(title, topics)` from the model's text, cut to the limits; None when it holds no usable title."""
    text = re.sub(r"```(?:json)?", "", text or "")
    start = text.find("{")
    if start < 0:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(text[start:])
    except ValueError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("title"), str):
        return None
    title = _words(data["title"], TITLE_WORDS, TITLE_CHARS)
    if not title:
        return None
    topics: list[str] = []
    for t in data.get("topics") if isinstance(data.get("topics"), list) else []:
        t = _words(t, TOPIC_WORDS, TOPIC_CHARS) if isinstance(t, str) else ""
        if t and t.lower() not in (x.lower() for x in topics):
            topics.append(t)
    return title, topics


def description_of(topics: list[str]) -> str:
    """The topics joined with ` · `, whole topics only (the newest come first, so the oldest fall off) up to 100 characters."""
    out = ""
    for t in topics:
        nxt = out + SEP + t if out else t
        if len(nxt) > DESC_CHARS:
            break
        out = nxt
    return out or (topics[0][:DESC_CHARS] if topics else "")


def clean_text(text: str) -> str:
    """A user message without the on_prompt context the core appended, on one line."""
    text = re.sub(r'\n*<context source="on_prompt">.*?</context>', "", text, flags=re.S)
    return " ".join(text.split())


# ------------------------------------------------------------------ what the model sees
def user_messages(s) -> list[str]:
    """The last user messages of the chat, shortened.  Read from events.jsonl: it still has what came before a handover."""
    out: list[str] = []
    try:
        with (s.run.path / "events.jsonl").open(encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                d = e.get("data") or {}
                if e.get("kind") == "message" and d.get("role") == "user" and not d.get("origin"):
                    if t := clean_text(str(d.get("text") or "")):
                        out.append(t[:MESSAGE_CHARS])
    except OSError:
        pass
    return out[-HISTORY_MESSAGES:]


def open_todos(s) -> list[str]:
    """Titles of the to-dos of the chat that are not done."""
    try:
        store = todos.Store(s.run.path)
        return [x.title[:60] for x in todos._walk(store.items)
                if isinstance(x, todos.Task) and x.state != "done"][:TODO_TITLES]
    except Exception:                                    # noqa: BLE001 - to-dos only help the naming
        return []


def previous_topics(r) -> list[str]:
    """The topics of the last naming, read back from an automatic description (a description by hand says nothing)."""
    return [] if r.description_manual or not r.description else [t for t in r.description.split(SEP) if t]


def build_input(messages: list[str], todo_titles: list[str], title: str, topics: list[str]) -> str:
    """The user part of the naming call."""
    parts = ["Last messages of the user (oldest first):\n" + "\n".join(f"- {m}" for m in messages)]
    if todo_titles:
        parts.append("Open to-dos of the chat:\n" + "\n".join(f"- {t}" for t in todo_titles))
    if title or topics:
        parts.append(f"Previous title: {title or '-'}\nPrevious topics (newest first): {', '.join(topics) or '-'}")
    return "\n\n".join(parts)


# ------------------------------------------------------------------ the model call
class _NoTools:
    """The dispatcher of the naming call: it offers no tool, so the model can only answer."""
    masker = None

    def schemas(self) -> list:
        return []


def ask_model(home, account: str, model: str, text: str) -> str:
    """One model call without tools through the chat's own account (its provider code); returns the answer text.
    Raises on an error event or after `CALL_TIMEOUT`.  Tests replace this function."""
    from .providers.base import make_provider, resolve_model

    async def go() -> str:
        provider = make_provider(home, account, _NoTools())
        req = Request(system=SYSTEM, messages=[Message("user", text)], model=resolve_model(home, account, model),
                      effort="low", max_turns=1)
        out: list[str] = []
        async for ev in provider.turn(req):
            if isinstance(ev, Error):
                raise RuntimeError(f"{ev.kind}: {ev.message}")
            if isinstance(ev, Text) and not ev.parent:
                out.append(ev.text)
        return "".join(out)

    return asyncio.run(asyncio.wait_for(go(), CALL_TIMEOUT))


# ------------------------------------------------------------------ the session side
def changed(s) -> None:
    """Tell whoever listens (`Session.on_title`) that title or description changed."""
    r = s.run.run
    for fn in list(s.on_title):
        try:
            fn(r.title, r.description)
        except Exception:                                # noqa: BLE001 - a listener never breaks the chat
            log.exception("title listener failed")


def set_manual(s, field: str, text: str) -> str:
    """`/rename` and `/describe`: text = set by hand and locked; empty = back to automatic (the next naming fills it).
    Returns the line to show."""
    text = s.run.set_name(field, text)                    # runs.RunDir: the one writer (adopts names set by other processes)
    changed(s)
    if not text:
        s._titled_at = 0                                 # the next finished turn names it again
        return f"{field}: automatic again (set with the next answer)"
    return f"{field}: {text}"


def after_turn(s) -> None:
    """A turn finished: name the chat in the background when it is due."""
    s._turns_done += 1
    if s.auto_title and due(s._turns_done, s._titled_at, settings(s.home)["title_every"]):
        start(s, daemon=True)


def at_close(s) -> None:
    """The chat ends: name it once more when turns came after the last naming.  The thread keeps the process alive until
    it is done (it is short), so the last name is saved."""
    if s.auto_title and s._turns_done > s._titled_at:
        start(s, daemon=False)


def start(s, daemon: bool) -> None:
    """Run `update` in a thread; one at a time."""
    with s._title_lock:
        if s._title_busy:
            return
        s._title_busy, s._titled_at = True, s._turns_done
    threading.Thread(target=_worker, args=(s,), daemon=daemon, name="lm-titles").start()


def _worker(s) -> None:
    try:
        update(s)
    except Exception as e:                               # noqa: BLE001 - a failure is a log line only
        log.warning("chat naming failed: %s: %s", type(e).__name__, e)
    finally:
        s._title_busy = False


def update(s, ask: Callable[..., str] | None = None) -> bool:
    """One automatic naming: ask the model, cut the answer, write what is not set by hand.  True when something changed."""
    r = s.run.run
    if r.title_manual and r.description_manual:
        return False
    cfg = settings(s.home)
    messages = user_messages(s)
    if not messages:
        return False
    topics = previous_topics(r)
    answer = (ask or ask_model)(s.home, s.account, cfg["title_model"],
                                build_input(messages, open_todos(s), r.title, topics))
    got = parse_answer(answer)
    if got is None:
        log.warning("chat naming: no usable answer: %r", (answer or "")[:200])
        return False
    title, new_topics = got
    before = (r.title, r.description)
    if not r.title_manual:                               # looked at again here: /rename may have come during the call
        r.title = title
    if not r.description_manual:
        r.description = description_of(new_topics)
    if (r.title, r.description) == before:
        return False
    s.run._meta()
    changed(s)
    return True
