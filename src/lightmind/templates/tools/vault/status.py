# /// script
# requires-python = ">=3.9"
# dependencies = []
# ///
"""Vault status: update the vault repo and print ONE line.  Standard library only.

    uv run status.py VAULT_DIR

Prints e.g. `Vault connected · 368 notes · main · clean`.  Never raises and always exits 0: a
problem is stated in the same single line, which always starts with `Vault`.
"""
import os
import subprocess
import sys
from pathlib import Path

SKIP = {".git", ".obsidian"}


def git(vault: Path, *args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}          # never wait for a password
    return subprocess.run(["git", "-C", str(vault), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, env=env)


def count_notes(vault: Path) -> int:
    n = 0
    for _, dirs, files in os.walk(vault):
        dirs[:] = [d for d in dirs if d not in SKIP]
        n += sum(1 for f in files if f.lower().endswith(".md"))
    return n


def own_repo(vault: Path) -> bool:
    """True when the vault folder is itself the top of a git repo (not a folder inside another repo)."""
    r = git(vault, "rev-parse", "--show-toplevel")
    return r.returncode == 0 and os.path.normcase(os.path.realpath(r.stdout.strip())) == \
        os.path.normcase(os.path.realpath(vault))


def line(vault: Path) -> str:
    if not vault.is_dir():
        return f"Vault not found: {vault}"
    try:
        if not own_repo(vault):
            n = count_notes(vault)
            return f"Vault present · {n} {'note' if n == 1 else 'notes'} · no git repo of its own"
        parts = ["Vault connected"]
        remote = git(vault, "rev-parse", "--abbrev-ref", "@{upstream}").returncode == 0
        failed = False
        if remote:
            try:
                failed = git(vault, "pull", "--ff-only", "--quiet", timeout=60).returncode != 0
            except subprocess.TimeoutExpired:
                failed = True
        n = count_notes(vault)
        branch = (git(vault, "branch", "--show-current").stdout.strip()
                  or git(vault, "rev-parse", "--short", "HEAD").stdout.strip() or "no commit")
        dirty = bool(git(vault, "status", "--porcelain").stdout.strip())
        parts += [f"{n} {'note' if n == 1 else 'notes'}", branch, "changed" if dirty else "clean"]
        if not remote:
            parts.append("no remote")
        if failed:
            parts.append("pull failed (offline?)")
        return " · ".join(parts)
    except FileNotFoundError:
        return "Vault: git is not installed"
    except (OSError, subprocess.SubprocessError) as e:
        return f"Vault: status not readable ({e.__class__.__name__})"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", newline="\n")      # UTF-8 and LF on every system
    if len(sys.argv) < 2:
        print("Vault: no folder given (status.py <folder>)")
        return 0
    print(line(Path(sys.argv[1]).expanduser()).replace("\n", " "))
    return 0


if __name__ == "__main__":
    sys.exit(main())
