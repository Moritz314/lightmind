"""Hook runner (`on_start`, `on_prompt`, `on_end`), split out of loop.py.  No model call.
Imports `files, providers.base, tools`; `loop` re-exports `run_on_*`, `HookOutput` and the limits."""
from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .files import Home
from .providers.base import settings
from .tools import NEW_GROUP, kill_tree, resolve_shell, secret_masker

if TYPE_CHECKING:
    from .loop import Session

HOOK_TIMEOUT = 15              # seconds for one hook command; an entry may set `timeout:`
HOOK_CONTEXT_MAX = 8000        # characters of one hook's context kept; an entry may set `max:`


@dataclass
class HookOutput:
    """What a batch of hook commands produced."""
    lines: list[str] = field(default_factory=list)       # output of `show` entries (+ systemMessage), to display
    warnings: list[str] = field(default_factory=list)    # failed / timed-out hooks, "warning: ..."; never an exception
    context: list[str] = field(default_factory=list)     # texts of `context` entries, for the model


HOOK_EVENTS = {"on_start": "SessionStart", "on_prompt": "UserPromptSubmit", "on_end": "SessionEnd"}


def _num(value, default: float) -> float:
    """A positive number from a settings value; anything else (a typo in lightmind.md) is the default."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    return n if n > 0 else default


def _vars(home: Home, run_dir: Path, absolute: bool = False) -> dict[str, str]:
    """{key} placeholders: every `paths:` key (as written, or absolute for env values), plus the
    absolute `root` and `run`."""
    keys = {k: str(home.path(k)) if absolute else home.rel(k) for k in home.paths}
    return {**keys, "root": str(home.root), "run": str(run_dir)}


def _subst(text: str, vars_: dict[str, str]) -> str:
    """Replace {key} for known keys only; shell braces such as {print x} stay as they are."""
    return re.sub(r"\{(\w+)\}", lambda m: vars_.get(m.group(1), m.group(0)), text)


def _shell(cmd: str, cwd: Path, env: dict[str, str], timeout: float, stdin_text: str = "",
           shell: str = "bash") -> tuple[int, str, str]:
    """Run a command line in the configured shell (the dispatcher's: Git Bash on Windows, never
    cmd.exe), `stdin_text` on its stdin -> (exit code, stdout, stderr).  On timeout the whole
    process tree is killed (killing only the shell would leave its child holding the pipes) and
    the code is -1.  `cwd` is the tree root: a shell found there is not used.  A missing shell raises
    RuntimeError, a missing program is exit 127."""
    argv = [resolve_shell(shell, cwd), "-c", cmd]
    p = subprocess.Popen(argv, cwd=str(cwd), env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", **NEW_GROUP)
    try:
        out, err = p.communicate(stdin_text, timeout=timeout)
        return p.returncode, out or "", err or ""
    except subprocess.TimeoutExpired:
        kill_tree(p.pid)
        p.communicate()
        return -1, "", "timed out"


def _secret_masker(home: Home):
    """A Masker that knows every secret value of the tree: whatever a hook prints, a secret
    value cannot reach the model or the logs (`tools.secret_masker`, shared with the dispatcher)."""
    return secret_masker(home)


def _hook_text(stdout: str) -> tuple[str, str]:
    """(context, message) of a hook's stdout.  Claude Code format: a JSON object with
    hookSpecificOutput.additionalContext (the context) and systemMessage (shown to the user);
    anything else is plain text and is the context."""
    text = stdout.strip()
    if text.startswith("{"):
        try:
            data = json.loads(text)
        except ValueError:
            data = None
        if isinstance(data, dict):
            spec = data.get("hookSpecificOutput")
            ctx = spec.get("additionalContext") if isinstance(spec, dict) else None
            return (str(ctx).strip() if ctx else ""), (str(data["systemMessage"]).strip()
                                                       if data.get("systemMessage") else "")
    return text, ""


def _run_hooks(home: Home, session: "Session", key: str, prompt: str = "") -> HookOutput:
    """Run settings[key] (on_start | on_prompt | on_end), Claude Code hook compatible: each entry
    `{run: cmd, show: bool, context: bool, timeout: s, env: {NAME: value}}` (or a plain string)
    runs in the tree root and gets Claude Code's JSON on stdin (hook_event_name, session_id,
    cwd, transcript_path, prompt for on_prompt).  Plain stdout, or the additionalContext of JSON
    stdout, is the output: shown if `show`, collected if `context`.  A nonzero exit or a timeout
    is a warning, never an exception.  {key} placeholders are replaced in `run` (paths as
    written) and in `env` values (absolute paths).  No model call.

    Hooks are trusted configuration of the tree owner (lightmind.md changes only after the user
    confirms), so they run without the per-call rule gate.  Two safeguards stay: every output
    passes a Masker that knows all secrets of the tree, and an optional `session.hook_gate(event,
    command) -> reason | None` can veto a command (a reason, or an exception, skips it and is
    reported as a warning)."""
    out = HookOutput()
    entries = settings(home).get(key) or []
    if not entries:                                  # on_prompt runs for every message: stay cheap
        return out
    rel, absolute = _vars(home, session.run.path), _vars(home, session.run.path, True)
    log_ = session.run.path / "log.md"
    payload = {"hook_event_name": HOOK_EVENTS[key], "session_id": session.id, "cwd": str(home.root),
               "transcript_path": str(log_ if log_.is_file() else session.run.path / "events.jsonl")}
    if key == "on_prompt":
        payload["prompt"] = prompt
    elif key == "on_start":
        payload["source"] = "resume" if session.resumed else "startup"
    else:
        payload["reason"] = "other"
    stdin_text = json.dumps(payload)
    full = _secret_masker(home)
    mask = lambda t: session.dispatcher.masker.mask(full.mask(t))      # noqa: E731

    def warn(text: str) -> None:
        out.warnings.append(mask(text))

    for raw in entries:
        entry = {"run": raw} if isinstance(raw, str) else dict(raw or {})
        cmd = _subst(str(entry.get("run") or "").strip(), rel)
        if not cmd:
            continue
        short = mask(cmd if len(cmd) <= 60 else cmd[:57] + "...")
        gate = session.hook_gate
        if gate is not None:                   # fail closed: a reason, or a broken gate, skips the hook
            try:
                reason = gate(key, cmd)
            except Exception as e:             # noqa: BLE001
                reason = f"gate error: {type(e).__name__}: {e}"
            if reason:
                warn(f"warning: hook `{short}` skipped: {reason}")
                session.run.event(key, {"cmd": mask(cmd), "skipped": mask(str(reason))[:300]})
                continue
        extra = entry.get("env")
        env = {**home.runtime_env(), "LM_HOME": str(home.root), "LM_RUN": str(session.run.path),
               "LM_RESULTS": str(session.run.results_path()),
               "LM_DEPTH": str(session.run.run.depth + 1), "LM_PARENT": session.run.run.id, "LM_PARENT_SIG": _parent_sig(home, session.run.run.id),
               "CLAUDE_PROJECT_DIR": str(home.root), "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
               **{str(k): _subst(str(v), absolute) for k, v in (extra.items() if isinstance(extra, dict) else [])}}
        timeout = _num(entry.get("timeout"), HOOK_TIMEOUT)
        try:
            rc, raw_out, raw_err = _shell(cmd, home.root, env, timeout, stdin_text,
                                          str(settings(home).get("shell") or "bash"))
        except (RuntimeError, OSError) as e:           # no shell, not executable: a warning like any failure
            warn(f"warning: hook `{short}` could not start: {e}")
            continue
        # Parse the RAW output first (JSON escapes a secret differently from the plain value), then mask
        # what is logged and passed on, and only then cut it: a cut inside a secret would leak its start.
        # The log never holds the raw stdout.
        text, message = _hook_text(raw_out)
        text, message, stderr = mask(text), mask(message), mask(raw_err.strip())
        session.run.event(key, {"cmd": mask(cmd), "rc": rc, "output": text[:2000], "message": message[:500],
                                "error": stderr[:500]})
        if rc != 0:
            why = f"timed out after {timeout:g}s" if rc == -1 else f"exited {rc}"
            tail = (stderr or text).splitlines()[:1]
            warn(f"warning: hook `{short}` {why}" + (f": {tail[0]}" if tail and rc != -1 else ""))
            continue
        if message:
            out.lines += message.splitlines()
        if entry.get("show") and text:
            out.lines += text.splitlines()
        if entry.get("context") and text:
            cap = int(_num(entry.get("max"), HOOK_CONTEXT_MAX))
            out.context.append(text if len(text) <= cap else text[:cap] + " [...cut]")
    return out


def run_on_start(home: Home, session: "Session") -> list[str]:
    """SessionStart hooks (settings.on_start).  Returns the lines to show (warnings included);
    `context` outputs are kept in session.start_context for the volatile block.  Zero model
    calls.  Blocking: call it in a thread from async code."""
    out = _run_hooks(home, session, "on_start")
    session.start_context += out.context
    return out.lines + out.warnings


def run_on_prompt(home: Home, session: "Session", prompt: str) -> HookOutput:
    """UserPromptSubmit hooks (settings.on_prompt), run by Session.send before every user
    message.  The caller appends `.context` to that message."""
    return _run_hooks(home, session, "on_prompt", prompt)


def run_on_end(home: Home, session: "Session") -> list[str]:
    """SessionEnd hooks (settings.on_end), called by Session.close.  Returns the lines to show."""
    out = _run_hooks(home, session, "on_end")
    return out.lines + out.warnings


def _parent_sig(home, run_id: str) -> str:
    from . import wake
    try:
        return wake.sign_parent(home, run_id)
    except OSError:
        return ""
