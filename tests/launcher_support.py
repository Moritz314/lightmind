"""Helpers for the launcher tests: a stand-in runtime and a fake uv, so no test downloads anything.

The launchers (`system/lm.cmd`, `system/lm`) are the real ones from the shipped tree.  What they start
is a stand-in: a copy of cmd.exe (Windows) or a shell script that prints its environment (POSIX).  uv
is a small Python script that the launcher finds through LM_UV and that logs every call.

The POSIX launcher also runs on Windows when Git's `sh` and `cygpath` are there (it then sees MSYS
paths), so it is checked on a Windows-only machine too.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from lightmind.files import platform_tag

FAKE_UV_PY = '''
import json, os, shutil, sys
from pathlib import Path

args = sys.argv[1:]
log = os.environ.get("LM_FAKE_LOG")
fail = os.environ.get("LM_FAKE_FAIL", "")
if log:
    names = ("UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR", "UV_PYTHON_INSTALL_BIN", "UV_PYTHON_INSTALL_REGISTRY",
             "UV_TOOL_DIR", "UV_TOOL_BIN_DIR", "PLAYWRIGHT_BROWSERS_PATH", "LIGHTMIND_HOME", "UV_OFFLINE")
    with open(log, "a", encoding="utf-8") as f:
        f.write(json.dumps({"args": args, "env": {n: os.environ.get(n) for n in names}}) + "\\n")
if args[:1] == ["--version"]:
    print("uv 0.0.0-fake")
elif args[:2] == ["python", "install"]:
    sys.exit(3 if fail == "python" else 0)
elif args[:1] == ["venv"]:
    rt = Path(args[-1])
    if os.environ.get("LM_FAKE_LAYOUT") == "windows":
        (rt / "Scripts").mkdir(parents=True)
        shutil.copy(os.environ["COMSPEC"], rt / "Scripts" / "lm.exe")
        (rt / "Scripts" / "python.exe").write_bytes(b"")
    else:
        (rt / "bin").mkdir(parents=True)
        exe = rt / "bin" / "lm"
        exe.write_text("#!/bin/sh\\nenv\\nexit ${LM_FAKE_EXIT:-0}\\n", encoding="utf-8", newline="\\n")
        exe.chmod(0o755)
        (rt / "bin" / "python").write_text("", encoding="utf-8")
    sys.exit(3 if fail == "venv" else 0)
elif args[:2] == ["pip", "install"]:
    if "--offline" in args:
        sys.exit(0 if os.environ.get("LM_FAKE_OFFLINE_OK") == "1" else 2)
    sys.exit(3 if fail == "pip" else 0)
elif args[:1] == ["build"]:
    out = Path(args[args.index("--out-dir") + 1])
    out.mkdir(parents=True, exist_ok=True)
    (out / "lightmind-0.1.0-py3-none-any.whl").write_bytes(b"fake wheel")
else:
    sys.exit(9)
'''

CMD_UV = '@echo off\r\n"%LM_FAKE_PY%" "%LM_FAKE_SCRIPT%" %*\r\nexit /b %ERRORLEVEL%\r\n'
SH_UV = '#!/bin/sh\nexec "$LM_FAKE_PY" "$LM_FAKE_SCRIPT" "$@"\n'
SH_STAND_IN = "#!/bin/sh\nenv\nexit ${LM_FAKE_EXIT:-0}\n"
WHEEL = "lightmind-0.1.0-py3-none-any.whl"


def _cygpath(path: Path | str, flag: str) -> str:
    return subprocess.run(["cygpath", flag, str(path)], capture_output=True, text=True, check=True).stdout.strip()


def git_sh() -> str | None:
    """A POSIX sh that can run next to cygpath: the one of Git for Windows, or the system's on POSIX."""
    if os.name == "nt":
        sh = shutil.which("sh")
        return sh if sh and shutil.which("cygpath") else None
    return shutil.which("sh")


def same(a: str | Path, b: str | Path) -> bool:
    return os.path.normcase(os.path.realpath(str(a))) == os.path.normcase(os.path.realpath(str(b)))


@dataclass
class Launcher:
    """One launcher (`windows`: lm.cmd, `posix`: lm under sh) with what a test needs to drive it."""
    kind: str

    # -- paths
    @property
    def msys(self) -> bool:
        return self.kind == "posix" and os.name == "nt"

    def script(self, tree: Path) -> list[str]:
        if self.kind == "windows":
            return [str(tree / "system" / "lm.cmd")]
        return [git_sh(), self.seen(tree / "system" / "lm")]

    def seen(self, path: Path) -> str:
        """`path` as the launcher sees it (MSYS spelling for the POSIX launcher on Windows)."""
        return _cygpath(path, "-u") if self.msys else str(path)

    def is_path(self, logged: str, expected: Path) -> bool:
        """Is `logged` (what the launcher passed on, MSYS programs may turn it into a Windows path) `expected`?"""
        if self.msys:
            logged = _cygpath(logged, "-w")
        return same(logged.rstrip("/\\"), expected)

    def marker(self, root: Path) -> str:
        return "lm-root.txt"                     # a file with the tree path in it, never the path in a file name

    def marker_text(self, root: Path) -> str:
        return str(root) if self.kind == "windows" else self.seen(root)

    def marks(self, root: Path) -> bool:
        """Does the current runtime of `root` carry the marker of exactly this place?"""
        f = self.runtime_dir(root) / self.marker(root)
        if not f.is_file():
            return False
        text = f.read_bytes().decode("oem" if self.kind == "windows" else "utf-8", errors="replace")
        return text.strip() == self.marker_text(root)

    def tag(self) -> str:
        if self.msys:
            return "linux-x86_64"                # uname under Git's sh says MINGW: the launcher falls through to linux
        return "-".join(platform_tag())

    def rts(self, tree: Path) -> Path:
        """`system/runtimes/<os>-<arch>`: the folder of the builds."""
        return tree / "system" / "runtimes" / self.tag()

    def pointer(self, tree: Path) -> Path:
        d = self.rts(tree)
        return d.with_name(d.name + ".current")

    def legacy_dir(self, tree: Path) -> Path:
        return tree / "system" / f"runtime-{self.tag()}"

    def builds(self, tree: Path) -> list[str]:
        """Names of the build folders that exist (r1, r2, ... and leftovers such as r1.drop)."""
        d = self.rts(tree)
        return sorted(p.name for p in d.iterdir() if p.is_dir()) if d.is_dir() else []

    def pointed(self, tree: Path) -> str:
        f = self.pointer(tree)
        return f.read_text(encoding="utf-8").strip() if f.is_file() else ""

    def runtime_dir(self, tree: Path) -> Path:
        """The runtime a start uses: the build the pointer names, else the legacy folder (which may not exist)."""
        name = self.pointed(tree)
        return self.rts(tree) / name if name else self.legacy_dir(tree)

    def runtime_exe(self, tree: Path, rt: Path | None = None) -> Path:
        rt = rt or self.runtime_dir(tree)
        return rt / "Scripts" / "lm.exe" if self.kind == "windows" else rt / "bin" / "lm"

    def set_stamp(self, tree: Path, token: str) -> None:
        """What `lm update` does to ask for a new runtime."""
        f = tree / "system" / "state" / "runtime-stamp.txt"
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(token, encoding="ascii")

    def uv_name(self) -> str:
        return "uv.exe" if self.kind == "windows" else "uv"

    # -- the stand-in runtime
    def fake_runtime(self, tree: Path, *, marker: bool = True, stamp: str = "", legacy: bool = False,
                     name: str = "r1", point: bool = True) -> Path:
        """A stand-in build `name` (the runtime of earlier launchers with `legacy`), made current by the pointer when
        `point`.  Returns its lm program."""
        rt = self.legacy_dir(tree) if legacy else self.rts(tree) / name
        exe = self.runtime_exe(tree, rt)
        exe.parent.mkdir(parents=True)
        if self.kind == "windows":
            shutil.copy(os.environ["COMSPEC"], exe)
        else:
            exe.write_text(SH_STAND_IN, encoding="utf-8", newline="\n")
            exe.chmod(0o755)
            if not legacy:
                (rt / "lm-use.lock").write_bytes(b"")
        if marker:
            (rt / self.marker(tree)).write_bytes(self.marker_text(tree).encode("oem" if self.kind == "windows" else "utf-8"))
        if not legacy:
            (rt / "lm-stamp.txt").write_text(stamp, encoding="ascii")
            if point:
                self.pointer(tree).parent.mkdir(parents=True, exist_ok=True)
                self.pointer(tree).write_text(name, encoding="ascii")
        return exe

    def env_args(self) -> list[str]:
        """Arguments that make the stand-in runtime print its environment."""
        return ["/c", "set"] if self.kind == "windows" else []

    def exit_args(self, code: int) -> tuple[list[str], dict[str, str]]:
        return (["/c", f"exit {code}"], {}) if self.kind == "windows" else ([], {"LM_FAKE_EXIT": str(code)})

    # -- the fake uv
    def fake_uv(self, folder: Path) -> dict[str, str]:
        """Write the fake uv into `folder`; returns the environment that makes a launcher use it."""
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "fake_uv.py").write_text(FAKE_UV_PY, encoding="utf-8")
        if self.kind == "windows":
            program = folder / "uv.cmd"
            program.write_bytes(CMD_UV.encode("ascii"))
        else:
            program = folder / "uv-fake"
            program.write_bytes(SH_UV.encode("ascii"))
            program.chmod(0o755)
        return {"LM_UV": self.seen(program), "LM_FAKE_PY": sys.executable, "LM_FAKE_SCRIPT": str(folder / "fake_uv.py"),
                "LM_FAKE_LOG": str(folder / "calls.jsonl"),
                "LM_FAKE_LAYOUT": "windows" if self.kind == "windows" else "posix"}

    # -- running
    def run(self, tree: Path, *args: str, env: dict[str, str] | None = None, cwd: Path | None = None):
        full = {k: v for k, v in os.environ.items() if not k.startswith(("LM_", "LIGHTMIND", "UV_"))}
        full.update(env or {})
        return subprocess.run([*self.script(tree), *args], capture_output=True, text=True, env=full, cwd=cwd,
                              encoding="utf-8", errors="replace", timeout=120)


def calls(env: dict[str, str]) -> list[dict]:
    """The calls the fake uv logged so far."""
    log = Path(env["LM_FAKE_LOG"])
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()] if log.is_file() else []


def verbs(env: dict[str, str]) -> list[str]:
    """What the fake uv was asked to do, in order: build, python install, venv, pip install, pip-offline."""
    out = []
    for c in calls(env):
        a = c["args"]
        out.append("pip-offline" if a[:2] == ["pip", "install"] and "--offline" in a else
                   " ".join(a[:2]) if a[:1] in (["pip"], ["python"]) else a[0])
    return out


def environ_of(stdout: str) -> dict[str, str]:
    """`set` / `env` output as a dict with upper-case names (Windows spells `Path` as it likes)."""
    pairs = (ln.split("=", 1) for ln in stdout.splitlines() if "=" in ln and not ln.startswith(("=", " ")))
    return {k.upper(): v for k, v in pairs}


def tree_in(tmp_path: Path, name: str = "my tree (x86)") -> Path:
    """A fresh tree made by init_tree in a folder whose name has spaces and parentheses: the usual way
    to break a batch file."""
    from lightmind.init import init_tree
    root = tmp_path / name
    init_tree(root)
    return root
