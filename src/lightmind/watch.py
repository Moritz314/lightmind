"""The automatic watch over child runs: the children book, the checks, the self-healing, the messages.

When a run starts a child run (`lm run --detach` with a parent) the core, without the model doing anything,
 (a) enters the child in `runs/<parent>/children.json` (id, title, start, status, restarts, last verdict) and
 (b) makes a watch trigger for the parent session in the state folder: `every <watch.every>` until `children-done`.
Every beat `cycle` looks at each open child, restarts the ones that died or stopped for a reason a restart cures (at most
`watch.max_restarts` times each, with a growing wait), tells the user about what went wrong, and wakes the parent session with ONE
message that lists all children and their verdicts.  A child that changes state is acted on at the next minute (`poll`), not
after the full interval.

A verdict says what the run is doing: ok | slow | stalled | off_track | dead | done | failed | paused | handover.  The facts of the
process (alive? status?) are the core's own and win; for a run that lives, the check tool of the tree answers whether it is on
course (`tools/watch/watch.py check --run <id> --json`: see INTERFACES.md), and when it is missing or fails the built-in script
check judges by process, last event and errors.  Everything a check says is untrusted text: cleaned and bounded before it
reaches a message, and it never decides an action by itself.

The restart counts live in the state folder (an agent cannot write there), the book in the run folder is for reading.

May import: files, runs, runtime, wake, timers, model.
"""
from __future__ import annotations

import hashlib
import json
import re
import shlex
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import runs, runtime, timers, wake
from .files import Home, write_atomic

BOOK = "children.json"
VERDICTS = ("ok", "slow", "stalled", "off_track", "dead", "done", "failed", "paused", "handover")
SUGGESTS = ("none", "nudge", "restart", "continue", "ask")
_METHOD = re.compile(r"[a-z][a-z0-9-]{0,15}")      # the method word an external check tool may give
CLOSED = ("done", "failed", "abandoned")           # states of a book entry that the watch no longer follows
PROBLEMS = ("dead", "failed", "paused", "handover", "off_track")
RESTARTABLE_PAUSE = re.compile(r"max.?turns|429|rate.?limit|overload|usage limit|session limit|resets?\b", re.I)
CHECK_TIMEOUT = 120
DEFAULT_EVERY, DEFAULT_RESTARTS, DEFAULT_STALL = "15m", 3, 30
RESTART_WAIT = 60                                   # seconds before the first restart; doubles with every one


def _setting(home: Home, dotted: str, default: Any = None) -> Any:
    return wake._setting(home, dotted, default)


def every(home: Home) -> str:
    v = str(_setting(home, "watch.every", DEFAULT_EVERY)).strip()
    v = v if v.lower().startswith("every") else f"every {v}"
    return v if timers.parse_when(v) and timers.parse_when(v)[1] >= timers.MIN_SECONDS else f"every {DEFAULT_EVERY}"


def max_restarts(home: Home) -> int:
    try:
        return max(int(_setting(home, "watch.max_restarts", DEFAULT_RESTARTS)), 0)
    except (TypeError, ValueError):
        return DEFAULT_RESTARTS


# ------------------------------------------------------------------ the book
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def book_file(home: Home, parent: str) -> Path:
    """The book the watch acts on lives in the state folder (no agent can write there); `runs/<parent>/children.json` is a copy
    for reading."""
    if not ID_RE.match(parent or ""):
        raise ValueError(f"bad run id {parent!r}")
    return home.path("state") / "watch" / f"{parent}.book.json"


def read_book(home: Home, parent: str) -> list[dict]:
    try:
        d = json.loads(book_file(home, parent).read_text("utf-8"))
    except (OSError, ValueError):
        return []
    rows = d.get("children") if isinstance(d, dict) else None
    return [r for r in rows if isinstance(r, dict) and r.get("id")] if isinstance(rows, list) else []


def _write_book(home: Home, parent: str, rows: list[dict]) -> None:
    text = json.dumps({"children": rows}, indent=1, ensure_ascii=False)
    write_atomic(book_file(home, parent), text)
    try:
        write_atomic(home.path("runs") / parent / BOOK, text)
    except OSError:
        pass


def _lock(home: Home, parent: str):
    d = home.path("state") / "watch"
    d.mkdir(parents=True, exist_ok=True)
    return runs._lock(d / f".{parent}.lock", wait=60.0)


def open_children(home: Home, parent: str) -> list[dict]:
    return [r for r in read_book(home, parent) if r.get("status") not in CLOSED]


def _state_file(home: Home, parent: str) -> Path:
    if not ID_RE.match(parent or ""):
        raise ValueError(f"bad run id {parent!r}")
    return home.path("state") / "watch" / f"{parent}.json"


def _load_state(home: Home, parent: str) -> dict:
    try:
        d = json.loads(_state_file(home, parent).read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _save_state(home: Home, parent: str, st: dict) -> None:
    write_atomic(_state_file(home, parent), json.dumps(st, indent=1))


def register_child(home: Home, parent: str, child: str, title: str = "") -> None:
    """A run started a child run: book entry and watch trigger.  Never raises (a problem here must not stop the start).
    Only a child whose own meta.json names this parent is entered, and only ids that are plain run ids."""
    try:
        if not (ID_RE.match(parent or "") and ID_RE.match(child or "")):
            return
        if not (home.path("runs") / parent / "meta.json").is_file() or _meta(home, child).get("parent") != parent:
            return
        now = time.time()
        with _lock(home, parent):
            rows = read_book(home, parent)
            if not any(r["id"] == child for r in rows):
                rows.append({"id": child, "title": wake.plain(title, 80),
                             "start": datetime.fromtimestamp(now).strftime("%Y-%m-%dT%H:%M:%S"),
                             "status": "running", "restarts": 0, "verdict": "", "verdict_at": "", "action": ""})
                _write_book(home, parent, rows)
            name = f"watch-{parent}"[:64]
            if not (timers.state_dir(home) / f"{name}.md").is_file():          # under the book lock: `retire` cannot slip in between
                timers.write_item(home, timers.Item(name=name, when=every(home), kind="watch", session=parent, owner=parent,
                                                    until=timers.CHILDREN_DONE, created=now, local=True))
    except Exception:                                  # noqa: BLE001
        pass


def retire(home: Home, parent: str) -> bool:
    """End the watch trigger of `parent` when no child is open any more.  Decided under the book lock, so a child that is
    registered at this moment is not left without a watch.  True when the trigger is gone."""
    with _lock(home, parent):
        if open_children(home, parent):
            return False
        timers.remove_item(home, f"watch-{parent}"[:64])
        return True


# ------------------------------------------------------------------ facts and checks
def _meta(home: Home, run_id: str) -> dict:
    try:
        return json.loads((home.path("runs") / run_id / "meta.json").read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def facts(home: Home, run_id: str) -> dict:
    """What the core knows without asking anybody: status, reason, alive.  A `running` run without a process is reaped here."""
    try:
        runs.reap_dead(home, run_id)
    except (FileNotFoundError, OSError, ValueError):
        pass
    m = _meta(home, run_id)
    if not m:
        return {"status": "gone", "reason": "run folder missing", "alive": False, "dead": True}
    status, reason = str(m.get("status", "")), str(m.get("exit_reason", ""))
    return {"status": status, "reason": reason, "alive": status == "running", "dead": runs.is_dead(status, reason),
            "start": m.get("start", "")}


def _last_event(home: Home, run_id: str) -> float:
    d = home.path("runs") / run_id
    best = 0.0
    for n in ("events.jsonl", "log.md", "meta.json"):
        try:
            best = max(best, (d / n).stat().st_mtime)
        except OSError:
            pass
    return best


def _recent_errors(home: Home, run_id: str, n: int = 40) -> int:
    f = home.path("runs") / run_id / "events.jsonl"
    try:
        with f.open("rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(fh.tell() - 60000, 0))
            tail = fh.read().decode("utf-8", "replace").splitlines()[-n:]
    except OSError:
        return 0
    bad = 0
    for line in tail:
        if '"kind": "error"' in line or '"ok": false' in line:
            bad += 1
    return bad


def script_check(home: Home, run_id: str, now: float | None = None) -> dict:
    """The built-in check: process, status, time since the last event, many errors in a row.  No model involved."""
    now = time.time() if now is None else now
    f = facts(home, run_id)
    ev = [f"status {f['status']}" + (f" ({f['reason']})" if f["reason"] else "")]
    v, why, sug = "ok", "the run is working", "none"
    if f["dead"]:
        v, why, sug = "dead", "its process is gone without an end", "restart"
    elif f["status"] == "done":
        v, why = "done", "finished"
    elif f["status"] == "failed":
        v, why = "failed", f["reason"] or "failed"
    elif f["status"] == "paused":
        v, why, sug = "paused", f["reason"] or "paused", "continue"
    elif f["status"] == "handover":
        v, why, sug = "handover", "its context is full: it stopped with a handover", "continue"
    elif f["status"] == "gone":
        v, why = "dead", "the run folder is gone"
    else:
        try:
            stall = max(int(_setting(home, "watch.stall_minutes", DEFAULT_STALL)), 1) * 60
        except (TypeError, ValueError):
            stall = DEFAULT_STALL * 60
        idle = now - _last_event(home, run_id)
        errs = _recent_errors(home, run_id)
        ev.append(f"last activity {int(idle // 60)} min ago")
        if idle > stall:
            v, why, sug = "stalled", f"no activity for {int(idle // 60)} minutes", "nudge"
        elif idle > stall / 2:
            v, why = "slow", f"quiet for {int(idle // 60)} minutes"
        if errs >= 15:
            v, why, sug = "off_track", f"{errs} errors among its last events", "ask"
            ev.append(f"{errs} errors in the last events")
    return {"run": run_id, "verdict": v, "reason": why, "evidence": ev, "method": "script", "suggest": sug}


def external_check(home: Home, run_id: str) -> dict | None:
    """The check tool of the tree (a model or a classifier first, script fallback inside it).  None when it is not there, fails, or answers badly."""
    script = home.root / "tools" / "watch" / "watch.py"
    given = str(_setting(home, "watch.checker", "") or "").strip()
    if given:
        argv = shlex.split(given)
    elif script.is_file():
        argv = (["uv", "run"] if shutil.which("uv") else [sys.executable]) + [str(script)]
    else:
        return None
    argv += ["check", "--run", run_id]
    brief = home.path("runs") / run_id / "brief.md"
    if brief.is_file():
        argv += ["--brief", str(brief)]
    argv.append("--json")
    try:
        p = subprocess.run(argv, cwd=str(home.root), capture_output=True, text=True, timeout=CHECK_TIMEOUT, env=home.runtime_env())
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode != 0:
        return None
    return parse_check(p.stdout, run_id)


def parse_check(text: str, run_id: str) -> dict | None:
    """The one JSON line of the interface, validated; None when it does not fit."""
    for line in reversed([x for x in (text or "").splitlines() if x.strip()]):
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if not isinstance(d, dict) or d.get("verdict") not in VERDICTS:
            return None
        ev = d.get("evidence") if isinstance(d.get("evidence"), list) else []
        return {"run": run_id, "verdict": d["verdict"], "reason": wake.plain(d.get("reason", ""), 300),
                "evidence": [wake.plain(x, 200) for x in ev[:5]],
                "method": _method(d.get("method")),
                "suggest": d["suggest"] if d.get("suggest") in SUGGESTS else "none"}
    return None


def _method(value: object) -> str:
    """How the check tool judged, as it says itself: one short lowercase word (it lands in a wake message), else `script`."""
    return value if isinstance(value, str) and _METHOD.fullmatch(value) else "script"


def check_child(home: Home, run_id: str, now: float | None = None) -> dict:
    """One verdict.  The core's facts decide the end states; only a run that lives is asked about its course."""
    own = script_check(home, run_id, now)
    if own["verdict"] not in ("ok", "slow", "stalled", "off_track"):
        return own
    ext = external_check(home, run_id)
    if ext is None:
        return own
    if ext["verdict"] in ("ok", "slow", "stalled", "off_track"):
        return ext
    return own | {"evidence": own["evidence"] + [f"check tool said {ext['verdict']}: {ext['reason']}"[:200]]}   # it cannot end a run that lives


# ------------------------------------------------------------------ heal
def _restart_text(why: str) -> str:
    entry = {"source": "Watch", "ts": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
             "text": f"Your run ended before the work was done ({why}). The watch restarted you in the same session. "
                     f"Look at your log and your own notes first, then continue where you stopped; do not start over."}
    return wake.frame([entry])


def restart_child(home: Home, run_id: str, why: str) -> bool:
    """Continue the child in its own session (same history; after a handover from the handover text), as its own process."""
    from .proc import popen_detached
    d = home.path("runs") / run_id
    argv = [runtime.python_for_new_process(home), "-m", "lightmind.cli", "--home", str(home.root), "--json", "run", "--continue", run_id, "-"]
    try:
        (d / "restart.md").write_text(_restart_text(why), "utf-8")
        with open(d / "restart.md", "rb") as fin, open(d / "stdout.txt", "ab") as fout, open(d / "stderr.txt", "ab") as ferr:
            popen_detached(argv, cwd=str(home.root), stdin=fin, stdout=fout, stderr=ferr)
    except OSError:
        return False
    return True


def _may_restart(v: dict, f: dict) -> str:
    """'' = no restart for this verdict, else the reason to give the child."""
    if f.get("status") == "gone":
        return ""                                       # no run folder: there is nothing to continue
    if v["verdict"] == "dead":
        return "its process died"
    if v["verdict"] == "handover":
        return "its context was full and it stopped with a handover"
    if v["verdict"] == "paused" and RESTARTABLE_PAUSE.search(f.get("reason", "")):
        return f"it paused ({f['reason']})"
    return ""


def _clock(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def _label(row: dict) -> str:
    t = (row.get("title") or "").strip()
    return f"{row['id']}" + (f" {t[:50]}" if t else "")


# ------------------------------------------------------------------ the cycle
def poll(home: Home, parent: str, now: float | None = None) -> bool:
    """Every minute, cheap (no model, no check tool): did an open child change to a state that needs an answer, or is a restart
    that was waiting for its time due?  Then the cycle runs now instead of at the next beat."""
    now = time.time() if now is None else now
    st = _load_state(home, parent)
    for row in open_children(home, parent):
        f = facts(home, row["id"])
        key = "dead" if f["dead"] else f["status"]
        c = st.get(row["id"], {})
        if key != "running" and now - c.get("restarted_at", 0) < RESTARTING:
            continue
        if key != "running" and key != row.get("seen", "running") and key in ("done", "failed", "dead", "paused", "handover", "gone"):
            return True
        if c.get("retry_at") and c["retry_at"] <= now and key != "running":
            return True
    return False


LIMIT_PAUSE = re.compile(r"429|rate.?limit|overload|usage limit|session limit|resets?\b", re.I)
LIMIT_WAIT = 600                                    # a rate limit does not lift in a minute: the first restart waits 10 minutes


def _wait(n: int, reason: str = "") -> int:
    """Seconds to wait before restart number n+1: 1, 2, 4 ... minutes (10, 20, 40 for a rate limit)."""
    return (LIMIT_WAIT if LIMIT_PAUSE.search(reason or "") else RESTART_WAIT) * (2 ** n)


RESTARTING = 90                                     # seconds after a restart during which the old end state is not judged again


def _heal(home: Home, row: dict, cs: dict, v: dict, f: dict, now: float, limit: int, events: list[tuple[str, str]]) -> str:
    """dead / paused / handover: restart when a restart can help, within the limit and after the wait.  Returns the action text."""
    verdict, why, n = v["verdict"], _may_restart(v, f), int(cs.get("restarts", 0))
    row["status"] = verdict
    if not why:
        row["status"] = "abandoned"                    # nothing a restart can cure: the user is told, the watch lets go
        events.append((f"{row['id']}:{verdict}:{f['reason']}", f"{_label(row)} is {verdict} ({f['reason'] or v['reason']}); not restarted."))
        return f"{verdict}, not restarted (needs a decision)"
    if n >= limit:
        row["status"] = "abandoned"
        events.append((f"{row['id']}:gaveup", f"{_label(row)} stopped again ({v['reason']}); gave up after {n} restarts."))
        return f"not restarted: {n} of {limit} restarts used"
    if not cs.get("retry_at"):
        cs["retry_at"] = now + _wait(n, f.get("reason", ""))     # first sight: wait, then restart (poll brings the cycle back when it is due)
        return f"restart {n + 1}/{limit} at {_clock(cs['retry_at'])}"
    if cs["retry_at"] > now:
        return f"restart {n + 1}/{limit} at {_clock(cs['retry_at'])}"
    cs["retry_at"] = None
    cs["restarts"] = n + 1                             # a start that fails counts too: the limit also ends a restart that never works
    row["restarts"] = n + 1
    if not restart_child(home, row["id"], why):
        cs["retry_at"] = now + _wait(n + 1)
        events.append((f"{row['id']}:restartfail:{n}", f"{_label(row)} stopped and could not be restarted ({n + 1}/{limit})."))
        return "restart failed to start"
    cs["restarted_at"] = now
    row["status"], row["seen"] = "running", "running"
    what = {"dead": "died", "handover": "ran out of context", "paused": "paused"}[verdict]
    events.append((f"{row['id']}:restart:{n + 1}", f"{_label(row)} {what}, restarted at {_clock(now)} ({n + 1}/{limit})."))
    return f"restarted ({n + 1}/{limit})"


def cycle(home: Home, parent: str, now: float | None = None) -> str:
    """One round: verdicts, healing, messages to the user, ONE wake message to the parent.  Returns a short word for the report.
    The checks (which can take a while) run before the book is locked; the book is read again under the lock, so a child
    registered meanwhile is kept."""
    now = time.time() if now is None else now
    st0 = _load_state(home, parent)
    results: dict[str, tuple[dict, dict] | None] = {}
    for row in open_children(home, parent):
        f = facts(home, row["id"])
        if not f["alive"] and now - st0.get(row["id"], {}).get("restarted_at", 0) < RESTARTING:
            results[row["id"]] = None                    # being restarted: not judged
        else:
            results[row["id"]] = (check_child(home, row["id"], now), f)
    limit = max_restarts(home)
    events: list[tuple[str, str]] = []                  # (dedupe key, text for the user)
    lines: list[str] = []
    with _lock(home, parent):
        st = _load_state(home, parent)
        rows = read_book(home, parent)
        for row in rows:
            if row.get("status") in CLOSED or row["id"] not in results:
                continue
            cs = st.setdefault(row["id"], {"restarts": 0})
            res = results[row["id"]]
            if res is None:
                lines.append(f"- {_label(row)}: restarting")
                continue
            v, f = res
            verdict = v["verdict"]
            row["verdict"], row["verdict_at"] = verdict, datetime.fromtimestamp(now).strftime("%Y-%m-%dT%H:%M:%S")
            row["seen"] = "dead" if f["dead"] else f["status"]
            act = ""
            if verdict in ("dead", "paused", "handover"):
                act = _heal(home, row, cs, v, f, now, limit, events)
            else:
                cs["retry_at"] = None
                row["status"] = verdict if verdict in ("done", "failed") else "running"
                if verdict == "failed":
                    events.append((f"{row['id']}:failed", f"{_label(row)} failed: {v['reason']}"))
                elif verdict == "off_track":
                    act = "no restart; look at it"
                    events.append((f"{row['id']}:off_track:{int(now // 21600)}", f"{_label(row)} may be off track: {v['reason']}"))
            row["action"] = act
            lines.append(f"- {_label(row)}: {verdict} ({v['method']}) - {v['reason']}" + (f" [{act}]" if act else ""))
        _write_book(home, parent, rows)
        _save_state(home, parent, st)
        rows_now = list(rows)
    fresh = _fresh(home, st, parent, events)
    if rows_now and not [r for r in rows_now if r.get("status") not in CLOSED]:
        n_ok = sum(1 for r in rows_now if r.get("status") == "done")
        summary = f"All {len(rows_now)} sub-runs are finished: {n_ok} done" + (f", {len(rows_now) - n_ok} not" if len(rows_now) > n_ok else "") + "."
        fresh += _fresh(home, st, parent, [(f"{parent}:alldone", summary)])
        lines.append(summary)
    if fresh:
        notify(home, " ".join(fresh), key=hashlib.sha1("|".join(fresh).encode()).hexdigest()[:16])
    if lines and (fresh or any(r.get("verdict") != "ok" for r in rows_now) or bool(_setting(home, "watch.wake_when_ok", False))):
        head = f"Watch over your sub-runs ({datetime.fromtimestamp(now).strftime('%H:%M')}):"
        word = wake.deliver(home, parent, head + "\n" + "\n".join(lines), "Watch", key=f"watch:{parent}")
        return f"watch:{word}"
    return "watch:quiet"


def _fresh(home: Home, st: dict, parent: str, events: list[tuple[str, str]]) -> list[str]:
    """The texts of events not told before (the same message never goes out twice)."""
    sent = set(st.setdefault("_sent", []))
    out = []
    for key, text in events:
        if key not in sent:
            sent.add(key)
            out.append(text)
    st["_sent"] = sorted(sent)[-200:]
    _save_state(home, parent, st)
    return out


# ------------------------------------------------------------------ messages to the user
def _run_cmd(argv: list[str], home: Home, text: str = "", stdin: bool = False) -> bool:
    try:
        p = subprocess.run(argv, cwd=str(home.root), input=text if stdin else None, text=True, capture_output=True,
                           timeout=60, env=home.runtime_env())
    except (OSError, subprocess.SubprocessError):
        return False
    return p.returncode == 0


def _channels(home: Home) -> list[str]:
    """The channels the watch's messages go to (`watch.notify`)."""
    return channels_of(home, "watch.notify", "both")


def channels_of(home: Home, key: str, default: str) -> list[str]:
    """The channels a message goes to: those the setting `key` (`default` when empty) names and the tree has the tool for."""
    mode = str(_setting(home, key, default) or default).strip().lower()
    if mode == "off":
        return []
    have_tg = (home.root / "tools" / "telegram").is_dir()
    have_ntfy = (home.root / "tools" / "ntfy" / "notify.py").is_file()
    if mode == "telegram":
        return ["telegram"] if have_tg else (["ntfy"] if have_ntfy else [])
    if mode == "ntfy":
        return ["ntfy"] if have_ntfy else []
    return [c for c, ok in (("telegram", have_tg), ("ntfy", have_ntfy)) if ok]


def send(home: Home, channel: str, text: str) -> bool:
    """One push on one channel.  The text goes as an argument list item or on stdin, never through a shell."""
    if channel == "ntfy":
        return _run_cmd(["uv", "run", "tools/ntfy/notify.py", "--title", "Lightmind", "--", text], home)
    base = shlex.split(str(_setting(home, "watch.telegram_command", "")) or "uv run tools/telegram/telegram.py send {text}")
    if any("{text}" in a for a in base):
        safe = text if not text.startswith("-") else "• " + text      # never read as an option (e.g. --file) by the send tool
        return _run_cmd([a.replace("{text}", safe) for a in base], home)
    return _run_cmd(base, home, text, stdin=True)


def notify(home: Home, text: str, key: str = "", where: str = "watch.notify", default: str = "both") -> bool:
    """Tell the user (a message to the owner, no question).  Short; the same `key` is never sent twice.  `where`: the
    setting that says on which channels (`default` when it is empty): the watch's own, or `workflow.notify` for a long workflow."""
    text = " ".join(wake.plain(text, 400).split())
    if not text:
        return False
    f = home.path("state") / "watch" / "notified.json"
    with runs._lock(f.parent / ".notified.lock") if f.parent.is_dir() else _nolock():
        try:
            done = json.loads(f.read_text("utf-8"))
        except (OSError, ValueError):
            done = []
        if key and key in done:
            return False
        ok = False
        for ch in (_channels(home) if where == "watch.notify" else channels_of(home, where, default)):
            ok = send(home, ch, text) or ok
        if ok and key:
            write_atomic(f, json.dumps((done + [key])[-300:]))
    return ok


class _nolock:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *a: Any) -> None:
        return None


# ------------------------------------------------------------------ the state block
def state_line(home: Home, parent: str, limit: int = 8) -> str:
    """The open children, one short line each, for the state block at the start of a turn ('' when there are none)."""
    rows = open_children(home, parent)
    if not rows:
        return ""
    out = [f"{r['id']} {' '.join(wake.plain(r.get('title') or '', 40).split())} - {r.get('status') if r.get('status') in ('running', 'paused', 'handover', 'dead') else '?'}"
           + (f", {r['verdict']}" if r.get("verdict") in VERDICTS else "")
           + (f", restarted {int(r['restarts'])}x" if str(r.get("restarts")).isdigit() and int(r["restarts"]) else "") for r in rows[:limit]]
    if len(rows) > limit:
        out.append(f"... and {len(rows) - limit} more")
    return "\n".join(out)
