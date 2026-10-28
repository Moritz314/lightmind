"""End-to-end install: a fresh tree, the real launcher, a real uv, a real Python download and the real
packages.  Takes minutes and needs the network, so the default suite skips it:

    uv run --extra dev pytest -m install tests/test_install.py -s

What it proves: the first start builds the runtime inside the tree (uv copied from PATH, Python 3.12,
venv, lightmind with its terminal UI), the second start does not rebuild anything, a moved tree is
rebuilt offline from the wheel it kept, and nothing is written outside the tree (user registry, ~/.local/bin).
"""
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from launcher_support import Launcher
from lightmind import __version__
from lightmind.init import INSTALL_SOURCE, init_tree

pytestmark = pytest.mark.install


def size_of(folder: Path) -> tuple[float, float]:
    """(MB on disk counting hard links once, MB as Explorer adds the files up)."""
    seen, unique, total = set(), 0, 0
    for p in folder.rglob("*"):
        if p.is_file() and not p.is_symlink():
            st = p.stat()
            total += st.st_size
            key = (st.st_dev, st.st_ino)
            if st.st_nlink > 1 and key in seen:
                continue
            seen.add(key)
            unique += st.st_size
    return unique / 1e6, total / 1e6


def outside_snapshot() -> dict:
    snap = {"local_bin": sorted(p.name for p in (Path.home() / ".local" / "bin").glob("*"))
            if (Path.home() / ".local" / "bin").is_dir() else []}
    if os.name == "nt":
        import winreg
        names = []

        def walk(key, path):
            with winreg.OpenKey(key, path) as k:
                i = 0
                while True:
                    try:
                        sub = winreg.EnumKey(k, i)
                    except OSError:
                        break
                    names.append(f"{path}\\{sub}")
                    walk(key, f"{path}\\{sub}")
                    i += 1
        try:
            walk(winreg.HKEY_CURRENT_USER, "Software\\Python")
        except OSError:
            pass
        snap["registry"] = sorted(names)
    return snap


@pytest.mark.skipif(shutil.which("uv") is None, reason="the install test copies the uv found on PATH")
def test_install_move_and_start_again(tmp_path):
    lau = Launcher("windows" if os.name == "nt" else "posix")
    tree = tmp_path / "my lightmind"
    init_tree(tree)                                    # records the source: this checkout (a folder)
    record = (tree / "system" / "state" / INSTALL_SOURCE).read_text(encoding="utf-8").split(" ", 1)
    assert record[0] == "folder", record
    before = outside_snapshot()
    times = {}

    def start(where: Path, **env):
        t = time.time()
        done = lau.run(where, "--version", env=env)
        return done, time.time() - t

    done, times["first"] = start(tree)
    assert done.returncode == 0, done.stderr[-3000:]
    assert done.stdout.strip().endswith(__version__), done.stdout
    assert "building the runtime" in done.stderr
    system = tree / "system"
    assert (system / "bin" / lau.uv_name()).is_file()                      # uv copied from PATH
    assert list((system / "python").glob("cpython-3.12*"))                 # Python lives in the tree
    assert lau.marks(tree)
    assert list((system / "wheels").glob("lightmind-*.whl"))               # the kept copy
    assert outside_snapshot() == before                                    # no registry entry, no python3.12.exe outside

    done, times["second"] = start(tree)
    assert done.returncode == 0 and done.stdout.strip().endswith(__version__)
    assert done.stderr == "" and times["second"] < 15

    moved = tmp_path / "moved here (copy)"
    shutil.move(str(tree), str(moved))
    done, times["moved"] = start(moved, UV_OFFLINE="1")                    # no network: the kept wheel and the cache must do
    assert done.returncode == 0, done.stderr[-3000:]
    assert done.stdout.strip().endswith(__version__) and "building the runtime" in done.stderr
    assert lau.marks(moved)
    done, times["moved again"] = start(moved)
    assert done.returncode == 0 and done.stderr == ""
    assert outside_snapshot() == before

    unique, total = size_of(moved / "system")
    print(f"\ninstall times (s): { {k: round(v, 1) for k, v in times.items()} }; "
          f"system/: {unique:.0f} MB on disk, {total:.0f} MB as Explorer counts")
    if os.name == "nt":                                                    # lm on PATH works through PATHEXT
        env = {"PATH": os.environ["PATH"] + os.pathsep + str(moved / "system")}
        done = subprocess.run("lm --version", shell=True, capture_output=True, text=True, env={**os.environ, **env})
        assert done.returncode == 0 and done.stdout.strip().endswith(__version__), done.stdout + done.stderr
