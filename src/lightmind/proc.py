"""Starting processes that must outlive the process (or service) that starts them.

`popen_detached` is what `lm run --detach`, `lm wake` and the watch use.  On Linux with a systemd user manager the
command runs in its own transient scope (`systemd-run --user --scope`): restarting the starting service (a unit with
`KillMode=control-group`, such as a Telegram bot) then does not kill it.  Without systemd it gets a session of its own
(`setsid`); on Windows a hidden console in a new process group.

May import: nothing of the package (leaf module).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys

_scope_ok: bool | None = None


def scope_available() -> bool:
    """Can a transient user scope be made here?  Tried once per process (a failing scope would otherwise be a command that never starts)."""
    global _scope_ok
    if _scope_ok is None:
        _scope_ok = False
        exe = shutil.which("systemd-run")
        runtime = os.environ.get("XDG_RUNTIME_DIR", "")
        if os.environ.get("LM_NO_SCOPE") != "1" and os.name == "posix" and sys.platform.startswith("linux") and exe and runtime and os.path.isdir(runtime):
            try:
                _scope_ok = subprocess.run([exe, "--user", "--scope", "--quiet", "true"], stdin=subprocess.DEVNULL,
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=8).returncode == 0
            except Exception:                        # noqa: BLE001 - any trouble means: no scope
                _scope_ok = False
    return _scope_ok


def detached_argv(argv: list[str], unit: str = "") -> list[str]:
    """`argv`, wrapped to run in its own scope when systemd can do that."""
    if not scope_available():
        return list(argv)
    pre = [shutil.which("systemd-run") or "systemd-run", "--user", "--scope", "--quiet", "--collect"]
    if unit:
        pre.append(f"--unit=lm-{unit}")
    return pre + ["--"] + list(argv)


def popen_detached(argv: list[str], *, cwd: str, stdin=None, stdout=None, stderr=None, env: dict | None = None,
                   unit: str = "") -> subprocess.Popen:
    """Start `argv` so that it survives the end of this process and of the service this process belongs to."""
    # CREATE_NO_WINDOW, not DETACHED_PROCESS: a detached worker has no console, so every git, bash or python it
    # starts opened a visible window of its own; a hidden console is inherited by all of them
    flags: dict = ({"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
                   if os.name == "nt" else {"start_new_session": True})
    return subprocess.Popen(detached_argv(argv, unit), stdin=stdin if stdin is not None else subprocess.DEVNULL,
                            stdout=stdout if stdout is not None else subprocess.DEVNULL,
                            stderr=stderr if stderr is not None else subprocess.DEVNULL,
                            cwd=cwd, env=env, close_fds=True, **flags)
