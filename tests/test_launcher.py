"""The launchers `system/lm.cmd` (Windows) and `system/lm` (POSIX sh): the portable environment, the
runtime bootstrap, the marker that detects a moved tree, runtimes side by side (an update while windows run, the pointer, the stamp,
cleaning up, the runtime of earlier launchers), launchers replaced while they run, `lm path`.  Nothing is downloaded: uv is a
fake (LM_UV) and the runtime is a stand-in, see launcher_support.py.  The real thing, with a real uv,
Python and packages, is tests/test_install.py (`-m install`)."""
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from launcher_support import WHEEL, Launcher, calls, environ_of, git_sh, tree_in, verbs
from lightmind.files import Home
from lightmind.init import INSTALL_SOURCE

KINDS = [pytest.param("windows", marks=pytest.mark.skipif(os.name != "nt", reason="lm.cmd runs on Windows")),
         pytest.param("posix", marks=pytest.mark.skipif(git_sh() is None, reason="needs sh (and cygpath on Windows)"))]
REQ = "requirement lightmind[tui]==0.1.0"


@pytest.fixture(params=KINDS)
def lau(request) -> Launcher:
    return Launcher(request.param)


@pytest.fixture
def tree(tmp_path) -> Path:
    return tree_in(tmp_path)


@pytest.fixture
def uv(lau, tmp_path) -> dict[str, str]:
    return lau.fake_uv(tmp_path / "fake-uv")


def record(tree: Path, line: str) -> None:
    (tree / "system" / "state" / INSTALL_SOURCE).write_text(line + "\n", encoding="utf-8", newline="\n")


def source_folder(parent: Path) -> Path:
    folder = parent / "source-folder"
    folder.mkdir(exist_ok=True)
    (folder / "pyproject.toml").write_text("[project]\nname = 'lightmind'\n", encoding="utf-8")
    return folder


def start(lau: Launcher, tree: Path, uv: dict[str, str], **extra: str):
    """One start; the stand-in runtime prints its environment."""
    return lau.run(tree, *lau.env_args(), env={**uv, **extra})


# ------------------------------------------------------------------ the launcher files
def test_launcher_files_have_the_right_bytes(tree):
    cmd = (tree / "system" / "lm.cmd").read_bytes()
    cmd.decode("ascii")                                                  # pure ASCII
    assert cmd.count(b"\r\n") == cmd.count(b"\n") > 100 and cmd.endswith(b"\r\n")
    sh = (tree / "system" / "lm").read_bytes()
    assert sh.startswith(b"#!/bin/sh\n") and b"\r" not in sh
    for text in (cmd.decode(), sh.decode()):
        for word in ("LIGHTMIND_HOME", "UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR", "UV_TOOL_DIR", "UV_TOOL_BIN_DIR",
                     "PLAYWRIGHT_BROWSERS_PATH", "UV_PYTHON_INSTALL_BIN", "UV_PYTHON_INSTALL_REGISTRY",
                     "UV_UNMANAGED_INSTALL", "install-source.txt", "wheels", "LM_UV"):
            assert word in text, word
        assert "TODO" not in text and ".lm" not in text
        assert not re.search(r"(?im)^\s*setx\b", text)                      # setx cuts a long Path at 1024 characters


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_gitattributes_keep_the_launchers_byte_exact():
    """A checkout on any system must give lm.cmd CRLF and lm LF, or the launchers break."""
    repo = Path(__file__).resolve().parent.parent
    if not (repo / ".git").exists():
        pytest.skip("not a git checkout")
    def attr(name: str) -> str:
        out = subprocess.run(["git", "check-attr", "eol", "text", "--", f"src/lightmind/templates/system/{name}"],
                             cwd=repo, capture_output=True, text=True).stdout
        return " ".join(sorted(ln.split(": ", 2)[1] + "=" + ln.split(": ", 2)[2] for ln in out.splitlines()))
    assert attr("lm.cmd") == "eol=crlf text=set"
    assert attr("lm") == "eol=lf text=set"
    assert attr("lightmind.cmd") == "eol=crlf text=set"
    assert attr("lightmind") == "eol=lf text=set"


# ------------------------------------------------------------------ fast start
def test_fast_start_sets_the_portable_environment_and_calls_no_uv(lau, tree, uv):
    lau.fake_runtime(tree)
    done = lau.run(tree, *lau.env_args(), env=uv, cwd=tree.parent)
    assert done.returncode == 0, done.stderr
    env = environ_of(done.stdout)                       # Windows names the variable Path, other systems PATH
    home = Home(tree)
    assert lau.is_path(env["LIGHTMIND_HOME"], tree)
    for var, key in (("UV_CACHE_DIR", "cache"), ("UV_PYTHON_INSTALL_DIR", "python"), ("UV_TOOL_BIN_DIR", "bin"),
                     ("PLAYWRIGHT_BROWSERS_PATH", "browsers")):
        assert lau.is_path(env[var], home.path(key)), var
    assert lau.is_path(env["UV_TOOL_DIR"], tree / "system" / f"uv-tools-{lau.tag()}")
    assert env["UV_PYTHON_INSTALL_BIN"] == "0" and env["UV_PYTHON_INSTALL_REGISTRY"] == "0"
    entries = [d for d in env["PATH"].split(";" if lau.kind == "windows" else ":") if d]
    assert lau.is_path(entries[-1], home.path("bin"))                      # LAST: a file left there cannot shadow a system program
    assert not any(lau.is_path(d, home.path("bin")) for d in entries[:-1])
    assert calls(uv) == []                              # nothing started uv


def test_the_lightmind_alias_does_what_lm_does(lau, tree, uv):
    """`lightmind` (and lightmind.cmd) only hands over to the launcher next to it."""
    lau.fake_runtime(tree)
    if lau.kind == "windows":
        cmd = [str(tree / "system" / "lightmind.cmd")]
    else:
        cmd = [git_sh(), lau.seen(tree / "system" / "lightmind")]
    full = {k: v for k, v in os.environ.items() if not k.startswith(("LM_", "LIGHTMIND", "UV_"))}
    full.update(uv)
    done = subprocess.run([*cmd, *lau.env_args()], capture_output=True, text=True, env=full, cwd=tree.parent,
                          encoding="utf-8", errors="replace", timeout=120)
    assert done.returncode == 0, done.stderr
    assert lau.is_path(environ_of(done.stdout)["LIGHTMIND_HOME"], tree)
    assert calls(uv) == []


def test_portable_environment_matches_home_runtime_env(lau, tree, uv):
    """The launcher and Home.runtime_env() name the same places (UV_TOOL_DIR is the launcher's alone)."""
    if lau.msys:
        pytest.skip("the places are compared as native paths")
    lau.fake_runtime(tree)
    env = environ_of(start(lau, tree, uv).stdout)
    home = Home(tree)
    mine = home.runtime_env({"PATH": "x"})
    for var in ("UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR", "PLAYWRIGHT_BROWSERS_PATH"):
        assert lau.is_path(env[var], Path(mine[var])), var
    assert lau.is_path(env["PATH"].split(os.pathsep)[-1], Path(mine["PATH"].split(os.pathsep)[-1]))   # both: bin last


def test_arguments_and_exit_code_are_passed_through(lau, tree, uv):
    lau.fake_runtime(tree)
    args, extra = lau.exit_args(7)
    assert lau.run(tree, *args, env={**uv, **extra}).returncode == 7
    assert lau.run(tree, *lau.exit_args(0)[0], env=uv).returncode == 0
    assert calls(uv) == []


def test_python_variables_of_the_caller_do_not_leak_into_the_runtime(lau, tree, uv):
    lau.fake_runtime(tree)
    env = environ_of(start(lau, tree, uv, PYTHONHOME="x", PYTHONPATH="y").stdout)
    assert "PYTHONHOME" not in env and "PYTHONPATH" not in env


# ------------------------------------------------------------------ first start
def test_first_start_builds_the_runtime_and_starts_it(lau, tree, uv):
    folder = source_folder(tree.parent)
    record(tree, f"folder {lau.seen(folder)}")
    done = start(lau, tree, uv)
    assert done.returncode == 0, done.stderr
    assert "building runtime r1" in done.stderr and "runtime r1 ready" in done.stderr
    assert "building" not in done.stdout and "ready" not in done.stdout    # stdout stays clean for pipes
    assert lau.is_path(environ_of(done.stdout)["LIGHTMIND_HOME"], tree)    # and then the runtime ran
    assert verbs(uv) == ["build", "python install", "venv", "pip install"]
    build, pyinst, venv, pip = calls(uv)
    wheels = tree / "system" / "wheels"
    assert build["args"][:3] == ["build", "--wheel", "--out-dir"]
    assert lau.is_path(build["args"][3], wheels) and lau.is_path(build["args"][4], folder)
    assert pyinst["args"] == ["python", "install", "3.12"]
    assert venv["args"][:4] == ["venv", "--python", "3.12", "--managed-python"]
    assert lau.is_path(venv["args"][4], lau.runtime_dir(tree))
    assert pip["args"][:2] == ["pip", "install"] and "--offline" not in pip["args"]
    assert pip["args"][-1].endswith(WHEEL + "[tui]")
    assert lau.is_path(pip["args"][-1][:-len("[tui]")], wheels / WHEEL)
    home = Home(tree)
    for c in calls(uv):                                                    # every uv call runs in the portable environment
        assert lau.is_path(c["env"]["UV_CACHE_DIR"], home.path("cache"))
        assert lau.is_path(c["env"]["UV_PYTHON_INSTALL_DIR"], home.path("python"))
        assert c["env"]["UV_PYTHON_INSTALL_BIN"] == "0" and c["env"]["UV_PYTHON_INSTALL_REGISTRY"] == "0"
    assert lau.marks(tree)
    assert (wheels / WHEEL).is_file()
    assert lau.pointed(tree) == "r1" and lau.builds(tree) == ["r1"]        # the build is in its own folder, the pointer names it
    assert not (tree / "system" / "runtimes" / (lau.tag() + ".current.tmp")).exists()
    assert (lau.runtime_dir(tree) / "lm-stamp.txt").read_bytes() == b""    # no `lm update` asked for anything: the empty stamp


def test_second_start_calls_no_uv(lau, tree, uv):
    record(tree, REQ)
    assert start(lau, tree, uv).returncode == 0
    assert verbs(uv) == ["python install", "venv", "pip install"]
    again = start(lau, tree, uv)
    assert again.returncode == 0 and "building" not in again.stderr
    assert verbs(uv) == ["python install", "venv", "pip install"]          # not one more call


def test_requirement_is_passed_to_uv_as_it_is(lau, tree, uv):
    record(tree, "requirement lightmind[tui] @ git+https://example.org/x/lightmind@abc123")
    assert start(lau, tree, uv).returncode == 0
    assert calls(uv)[-1]["args"][-1] == "lightmind[tui] @ git+https://example.org/x/lightmind@abc123"
    assert not (tree / "system" / "wheels").exists()


def test_wheel_file_is_kept_inside_the_tree(lau, tree, uv, tmp_path):
    wheel = tmp_path / "elsewhere" / WHEEL
    wheel.parent.mkdir()
    wheel.write_bytes(b"wheel")
    record(tree, f"wheel {lau.seen(wheel)}")
    assert start(lau, tree, uv).returncode == 0
    kept = tree / "system" / "wheels" / WHEEL
    assert kept.read_bytes() == b"wheel"
    assert lau.is_path(calls(uv)[-1]["args"][-1][:-len("[tui]")], kept)
    wheel.unlink()                                                         # the source may go: the tree has its copy
    shutil.rmtree(lau.runtime_dir(tree))
    assert start(lau, tree, uv).returncode == 0
    assert verbs(uv)[-2:] == ["pip-offline", "pip install"]                # the cache of the fake is empty: online follows


# ------------------------------------------------------------------ a moved or copied tree
def test_moved_tree_is_rebuilt_from_the_wheel_it_kept(lau, tmp_path, uv):
    tree = tree_in(tmp_path, "first place")
    folder = source_folder(tmp_path)
    record(tree, f"folder {lau.seen(folder)}")
    assert start(lau, tree, uv).returncode == 0
    shutil.rmtree(folder)                                                  # the source is gone, only the kept wheel is left
    moved = tmp_path / "second place (copy)"
    shutil.copytree(tree, moved)
    before = len(calls(uv))
    done = start(lau, moved, uv, LM_FAKE_OFFLINE_OK="1")
    assert done.returncode == 0, done.stderr
    assert "building runtime r2" in done.stderr
    new = calls(uv)[before:]
    assert [c["args"][0] for c in new] == ["python", "venv", "pip"]        # no build: the wheel is the source
    assert "--offline" in new[-1]["args"]                                  # and the cache is enough: no network
    assert lau.is_path(new[-1]["args"][-1][:-len("[tui]")], moved / "system" / "wheels" / WHEEL)
    rt = lau.runtime_dir(moved)
    assert rt.name == "r2" and lau.builds(moved) == ["r2"]                 # a new build; the copy of the old one was in nobody's use
    assert [p.name for p in rt.iterdir() if p.name.startswith("lm-root.")] == ["lm-root.txt"] and lau.marks(moved)   # the old place is gone
    assert start(lau, moved, uv).returncode == 0 and len(calls(uv)) == before + 3                 # and now it is quiet


def test_a_source_folder_with_umlauts_is_read_as_utf8(lau, tree, uv, tmp_path):
    if lau.msys:
        pytest.skip("Git's sh on Windows does not see non-ASCII paths the way a real POSIX sh does")
    parent = tmp_path / "Quellen äöü"
    parent.mkdir()
    folder = source_folder(parent)
    (tree / "system" / "state").mkdir(parents=True, exist_ok=True)
    (tree / "system" / "state" / "install-source.txt").write_bytes(f"folder {lau.seen(folder)}\n".encode("utf-8"))
    done = start(lau, tree, uv)
    assert done.returncode == 0, done.stderr
    assert lau.is_path(calls(uv)[0]["args"][4], folder)


def test_offline_attempt_falls_back_to_the_network(lau, tree, uv):
    wheels = tree / "system" / "wheels"
    wheels.mkdir()
    (wheels / WHEEL).write_bytes(b"w")
    done = start(lau, tree, uv)                                            # the fake cache is empty: offline fails
    assert done.returncode == 0 and "No solution" not in done.stderr       # quietly, nothing scary on the screen
    assert verbs(uv) == ["python install", "venv", "pip-offline", "pip install"]


def test_a_runtime_of_another_os_stays_and_is_not_mistaken_for_this_one(lau, tree, uv):
    other = tree / "system" / "runtime-sunos-sparc"
    other.mkdir()
    (other / "lm-root.whatever").write_bytes(b"")
    record(tree, REQ)
    assert start(lau, tree, uv).returncode == 0
    assert other.is_dir() and verbs(uv)[0] == "python install"


def test_a_runtime_without_its_marker_is_rebuilt(lau, tree, uv):
    lau.fake_runtime(tree, marker=False)
    record(tree, REQ)
    assert start(lau, tree, uv).returncode == 0
    assert verbs(uv) == ["python install", "venv", "pip install"]


# ------------------------------------------------------------------ updates while lm windows run
def hold_runtime(lau: Launcher, tree: Path, rt: Path | None = None) -> subprocess.Popen:
    """A process that runs from the stand-in runtime folder for a while, as another lm window does.  On POSIX a build
    is held the way the launcher holds it (a shared flock on lm-use.lock); the legacy runtime has no such file and is
    recognised by the command line of the process."""
    rt = rt or lau.runtime_dir(tree)
    exe = lau.runtime_exe(tree, rt)
    if lau.kind == "windows":
        return subprocess.Popen([str(exe), "/c", "ping -n 60 127.0.0.1 >nul"], stdout=subprocess.DEVNULL)
    hold = exe.parent / "hold"
    hold.write_text("#!/bin/sh\nsleep 60\n", encoding="utf-8", newline="\n")
    hold.chmod(0o755)
    lock = rt / "lm-use.lock"
    proc = subprocess.Popen(["flock", "-s", str(lock), str(hold)] if lock.is_file() else [str(hold)], stdout=subprocess.DEVNULL,
                            start_new_session=True)                 # flock runs the command as a child: stop() ends the whole group
    time.sleep(0.7)
    return proc


def stop(proc: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)    # a child (ping) may hold the lock
    else:
        try:
            os.killpg(proc.pid, 9)
        except (ProcessLookupError, PermissionError):
            pass
    proc.kill()
    proc.wait()


def need_flock(lau: Launcher) -> None:
    if lau.kind == "posix" and shutil.which("flock") is None:
        pytest.skip("needs the flock program (the launcher keeps old runtimes without it)")


def hold_build_lock(lau: Launcher, tree: Path, tmp_path: Path):
    """Another window that is building right now: the lock the way each launcher takes it.  Returns what to stop."""
    lock = tree / "system" / "build.lock"
    if lau.kind == "windows":
        holder = tmp_path / "holder.cmd"
        holder.write_bytes(b'@echo off\r\n9>"%~1" ping -n 60 127.0.0.1 >nul\r\n')
        proc = subprocess.Popen([str(holder), str(lock)], stdout=subprocess.DEVNULL)
        time.sleep(1.0)
        return proc
    lock.mkdir()
    (lock / "pid").write_text(str(os.getpid()), encoding="utf-8")      # alive: this test process
    return None


def release_build_lock(tree: Path, holder) -> None:
    if holder is not None:
        stop(holder)
    elif (tree / "system" / "build.lock").is_dir():
        shutil.rmtree(tree / "system" / "build.lock")


def test_a_new_version_is_built_next_to_a_runtime_in_use_and_the_old_one_goes_when_free(lau, tree, uv):
    need_flock(lau)
    old = lau.fake_runtime(tree)
    (old.parent.parent / "sentinel.txt").write_text("keep", encoding="utf-8")
    record(tree, REQ)
    lau.set_stamp(tree, "t1")                           # what `lm update` leaves behind
    other = hold_runtime(lau, tree)
    try:
        done = start(lau, tree, uv)
        assert done.returncode == 0, done.stderr
        assert "runtime r2 ready" in done.stderr
        assert verbs(uv) == ["python install", "venv", "pip install"]
        assert lau.pointed(tree) == "r2" and lau.marks(tree)
        assert lau.is_path(calls(uv)[1]["args"][4], lau.rts(tree) / "r2")
        assert (lau.runtime_dir(tree) / "lm-stamp.txt").read_text(encoding="ascii") == "t1"
        assert old.is_file() and (old.parent.parent / "sentinel.txt").read_text(encoding="utf-8") == "keep"   # in use: untouched
        assert lau.builds(tree) == ["r1", "r2"]
        assert "LIGHTMIND_HOME" in done.stdout or lau.kind == "windows"               # and the NEW runtime ran
    finally:
        stop(other)
    again = start(lau, tree, uv)                        # nobody holds r1 now: it is removed, nothing is built
    assert again.returncode == 0 and "building" not in again.stderr and len(verbs(uv)) == 3
    assert lau.builds(tree) == ["r2"] and not (tree / "system" / "build.lock").is_dir()


def test_a_pending_update_is_built_when_no_other_window_is_open(lau, tree, uv):
    lau.fake_runtime(tree)
    record(tree, REQ)
    lau.set_stamp(tree, "t1")
    done = start(lau, tree, uv)
    assert done.returncode == 0, done.stderr
    assert lau.pointed(tree) == "r2" and lau.builds(tree) == ["r2"]       # the old build was free: gone at the end of the build
    quiet = start(lau, tree, uv)
    assert quiet.returncode == 0 and "building" not in quiet.stderr and len(verbs(uv)) == 3


def test_no_pending_update_without_a_new_stamp(lau, tree, uv):
    lau.fake_runtime(tree, stamp="t1")
    lau.set_stamp(tree, "t1")                           # the runtime was built for exactly this
    assert start(lau, tree, uv).returncode == 0 and verbs(uv) == []
    record(tree, REQ)
    lau.set_stamp(tree, "t2")                           # lm update asked again: now the runtime is outdated
    assert start(lau, tree, uv).returncode == 0 and len(verbs(uv)) == 3 and lau.pointed(tree) == "r2"


def test_a_start_while_another_window_builds_runs_the_current_runtime(lau, tree, uv, tmp_path):
    lau.fake_runtime(tree)
    record(tree, REQ)
    lau.set_stamp(tree, "t1")
    holder = hold_build_lock(lau, tree, tmp_path)
    try:
        done = start(lau, tree, uv)
    finally:
        release_build_lock(tree, holder)
    assert done.returncode == 0, done.stderr
    assert "being built in another lm window" in done.stderr
    assert "LIGHTMIND_HOME" in done.stdout or lau.kind == "windows"           # the current runtime ran
    assert verbs(uv) == [] and lau.pointed(tree) == "r1"
    assert start(lau, tree, uv).returncode == 0 and lau.pointed(tree) == "r2"  # the lock is free again: this start builds


def test_a_start_with_no_runtime_waits_for_a_running_build_and_gives_up(lau, tree, uv, tmp_path):
    record(tree, REQ)
    holder = hold_build_lock(lau, tree, tmp_path)
    try:
        done = start(lau, tree, uv, LM_LOCK_WAIT="2")
    finally:
        release_build_lock(tree, holder)
    assert done.returncode == 1 and "build.lock" in done.stderr and verbs(uv) == []


def test_build_prepares_the_runtime_and_starts_nothing(lau, tree, uv):
    record(tree, REQ)
    first = lau.run(tree, "build", env=uv)
    assert first.returncode == 0, first.stderr
    assert first.stdout == "" and "runtime r1 ready" in first.stderr          # nothing was started
    assert verbs(uv) == ["python install", "venv", "pip install"] and lau.marks(tree)
    second = lau.run(tree, "build", env=uv)
    assert second.returncode == 0 and "up to date" in second.stderr and len(verbs(uv)) == 3
    lau.set_stamp(tree, "t1")
    third = lau.run(tree, "build", env=uv)
    assert third.returncode == 0 and lau.pointed(tree) == "r2" and len(verbs(uv)) == 6
    assert start(lau, tree, uv).returncode == 0 and len(verbs(uv)) == 6      # and the next window opens without waiting


def test_a_failing_build_command_says_so_and_changes_nothing(lau, tree, uv):
    lau.fake_runtime(tree)
    record(tree, REQ)
    lau.set_stamp(tree, "t1")
    done = lau.run(tree, "build", env={**uv, "LM_FAKE_FAIL": "pip"})
    assert done.returncode == 1 and "could not be built" in done.stderr
    assert lau.pointed(tree) == "r1" and lau.builds(tree) == ["r1"]


@pytest.mark.parametrize("fail", ["python", "venv", "pip"])
def test_a_failed_new_build_leaves_the_current_runtime_running(lau, tree, uv, fail):
    old = lau.fake_runtime(tree)
    (old.parent.parent / "sentinel.txt").write_text("old", encoding="utf-8")
    record(tree, REQ)
    lau.set_stamp(tree, "t1")
    done = start(lau, tree, uv, LM_FAKE_FAIL=fail)
    assert done.returncode == 0 and "this window starts the current one" in done.stderr, done.stderr
    assert (old.parent.parent / "sentinel.txt").read_text(encoding="utf-8") == "old" and old.is_file()
    assert lau.pointed(tree) == "r1" and lau.builds(tree) == ["r1"]          # the partial r2 is dropped, the pointer untouched
    assert not (tree / "system" / "build.lock").is_dir()                      # (Windows keeps its lock as an empty file)
    assert start(lau, tree, uv).returncode == 0 and lau.pointed(tree) == "r2"    # the next start tries again, and works


def test_a_runtime_of_a_moved_tree_is_not_run_and_is_replaced(lau, tree, uv):
    exe = lau.fake_runtime(tree)
    (exe.parent.parent / lau.marker(tree)).write_bytes(b"/somewhere/else")
    record(tree, REQ)
    done = start(lau, tree, uv, LM_FAKE_FAIL="pip")
    assert done.returncode == 1 and verbs(uv)[-1] == "pip install"           # not started: it belongs to another tree
    ok = start(lau, tree, uv)
    assert ok.returncode == 0 and lau.pointed(tree) == "r2" and lau.marks(tree)
    assert lau.builds(tree) == ["r2"]                                         # the foreign one was in nobody's use: removed


def test_a_pointer_that_does_not_name_a_plain_build_is_not_followed(lau, tree, uv):
    lau.fake_runtime(tree)
    lau.pointer(tree).write_text("..", encoding="ascii")
    record(tree, REQ)
    done = start(lau, tree, uv)
    assert done.returncode == 0 and lau.pointed(tree) == "r2"               # r1 was not taken for the pointer's target


# ------------------------------------------------------------------ the runtime of earlier launchers
def test_the_single_runtime_of_earlier_launchers_runs_without_a_rebuild(lau, tree, uv):
    lau.fake_runtime(tree, legacy=True)
    done = start(lau, tree, uv)
    assert done.returncode == 0 and verbs(uv) == [] and not lau.pointer(tree).exists()


def test_an_update_builds_next_to_the_legacy_runtime_in_use_and_removes_it_when_free(lau, tree, uv):
    exe = lau.fake_runtime(tree, legacy=True, marker=False)        # what an old `lm update` leaves: outdated, no marker
    (exe.parent.parent / "sentinel.txt").write_text("keep", encoding="utf-8")
    record(tree, REQ)
    other = hold_runtime(lau, tree, exe.parent.parent)
    try:
        done = start(lau, tree, uv)
        assert done.returncode == 0, done.stderr
        assert lau.pointed(tree) == "r1" and lau.marks(tree)
        assert exe.is_file() and (exe.parent.parent / "sentinel.txt").read_text(encoding="utf-8") == "keep"
    finally:
        stop(other)
    assert start(lau, tree, uv).returncode == 0
    assert not lau.legacy_dir(tree).exists() and len(verbs(uv)) == 3


def test_the_legacy_runtime_that_nobody_uses_goes_with_the_first_build(lau, tree, uv):
    lau.fake_runtime(tree, legacy=True, marker=False)
    record(tree, REQ)
    assert start(lau, tree, uv).returncode == 0
    assert not lau.legacy_dir(tree).exists() and lau.builds(tree) == ["r1"]


def test_a_stamp_makes_the_legacy_runtime_outdated(lau, tree, uv):
    lau.fake_runtime(tree, legacy=True)
    record(tree, REQ)
    lau.set_stamp(tree, "t1")
    assert start(lau, tree, uv).returncode == 0 and lau.pointed(tree) == "r1"


# ------------------------------------------------------------------ cleaning up
def test_cleanup_keeps_the_current_the_pointed_and_every_runtime_in_use(lau, tree, uv):
    need_flock(lau)
    lau.fake_runtime(tree, name="r1", point=False)                    # old, in use below
    lau.fake_runtime(tree, name="r2")                                 # current
    partial = lau.runtime_exe(tree, lau.rts(tree) / "r3")             # a build that was killed: no marker, no lock file
    partial.parent.mkdir(parents=True)
    partial.write_bytes(b"x")
    other = hold_runtime(lau, tree, lau.rts(tree) / "r1")
    try:
        assert start(lau, tree, uv).returncode == 0
        assert lau.builds(tree) == ["r1", "r2"]                       # r3 went, r1 stays while it runs, r2 is current
    finally:
        stop(other)
    assert start(lau, tree, uv).returncode == 0
    assert lau.builds(tree) == ["r2"] and lau.pointed(tree) == "r2" and verbs(uv) == []


def test_without_flock_old_runtimes_are_kept_not_risked(tree, uv, tmp_path):
    lau = Launcher("posix")
    if git_sh() is None or lau.msys:
        pytest.skip("needs a POSIX sh")
    bins = tmp_path / "bin-without-flock"
    bins.mkdir()
    for name in ("sh", "uname", "dirname", "cat", "mkdir", "rm", "printf", "sleep", "ls", "head", "ps", "awk", "tr", "kill",
                 "cp", "chmod", "readlink", "basename", "mv", "env", "sed", "find"):
        found = shutil.which(name)
        if found:
            (bins / name).symlink_to(found)
    lau.fake_runtime(tree, name="r1", point=False)
    lau.fake_runtime(tree, name="r2")
    done = start(lau, tree, uv, PATH=str(bins))
    assert done.returncode == 0, done.stderr
    assert lau.builds(tree) == ["r1", "r2"]                             # r1 may be in use, and nothing can tell: kept


def test_the_launcher_holds_the_shared_lock_of_the_runtime_it_starts(lau, tree, uv):
    need_flock(lau)
    if lau.kind != "posix":
        pytest.skip("the flock is a POSIX mechanism")
    lau.fake_runtime(tree)
    exe = lau.runtime_exe(tree)                         # the stand-in asks for the lock through a second open of the file
    exe.write_text('#!/bin/sh\nflock -xn "$(dirname "$0")/../lm-use.lock" true && echo FREE || echo HELD\n', encoding="utf-8", newline="\n")
    done = start(lau, tree, uv)
    assert done.returncode == 0 and "HELD" in done.stdout                # the exec'd lm inherits the shared lock


# ------------------------------------------------------------------ launchers that are replaced while they run
OFFSETS_OF_OLD_LAUNCHERS = (1190, 2859, 2972, 3365, 3403)               # where earlier lm.cmd versions go on after the lm.exe line


def template(name: str) -> bytes:
    return (Path(__file__).resolve().parents[1] / "src" / "lightmind" / "templates" / "system" / name).read_bytes()


def test_the_padding_of_lm_cmd_covers_every_offset_an_old_window_comes_back_to():
    """cmd.exe reopens a running batch file by name and goes on at the old byte offset: at each of these offsets the new
    file must have blanks that are followed by `exit /b %ERRORLEVEL%` (or a line start), never the middle of a word."""
    data = template("lm.cmd")
    for offset in OFFSETS_OF_OLD_LAUNCHERS:
        line_start = data.rfind(b"\n", 0, offset) + 1
        line_end = data.index(b"\n", offset)
        rest = data[offset:line_end + 1]
        assert rest.strip(b" ") .startswith(b"exit /b %ERRORLEVEL%") or rest.strip() == b"", (offset, data[line_start:line_end])
        assert set(data[line_start:offset]) <= {0x20}, (offset, data[line_start:line_end])


def test_the_new_lm_cmd_never_reads_on_after_it_started_lm_exe():
    text = template("lm.cmd").decode("ascii")
    run_lines = [ln for ln in text.split("\r\n") if "Scripts\\lm.exe\" %*" in ln]
    assert len(run_lines) == 1 and run_lines[0].endswith("& call exit /b %%ERRORLEVEL%%"), run_lines


def wait_for(flag: Path, seconds: float = 30.0) -> None:
    """Wait until the stand-in lm.exe says it runs; the launcher is replaced only after that."""
    end = time.time() + seconds
    while not flag.exists() and time.time() < end:
        time.sleep(0.1)
    assert flag.exists(), "the stand-in lm.exe did not start"
    time.sleep(0.3)


OLD_LAUNCHERS = sorted((Path(__file__).parent / "fixtures" / "old-launchers").glob("lm-*.cmd"))


@pytest.mark.skipif(os.name != "nt", reason="lm.cmd runs on Windows")
@pytest.mark.parametrize("old", OLD_LAUNCHERS, ids=lambda p: p.stem)
def test_a_window_on_an_old_launcher_survives_the_launcher_being_replaced(tmp_path, old):
    """The real thing: an old lm.cmd waits for its lm.exe; lm update replaces system\\lm.cmd; the old window must end
    with the exit code of lm.exe and run nothing else."""
    tree = tree_in(tmp_path)
    system = tree / "system"
    rt = system / "runtime-windows-x86_64"                          # the single runtime these launchers know
    (rt / "Scripts").mkdir(parents=True)
    shutil.copy(os.environ["COMSPEC"], rt / "Scripts" / "lm.exe")
    (rt / "lm-root.txt").write_bytes(str(tree).encode("oem"))
    (system / "lm.cmd").write_bytes(old.read_bytes())
    proc = subprocess.Popen([str(system / "lm.cmd"), "/c", "echo.>started.txt & ping -n 6 127.0.0.1 >nul & echo ran-lm-exe & exit 5"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", cwd=tmp_path)
    wait_for(tmp_path / "started.txt")
    new = system / "lm.cmd.new"
    new.write_bytes(template("lm.cmd"))
    os.replace(new, system / "lm.cmd")                              # what refresh_launchers does
    out, _ = proc.communicate(timeout=60)
    assert proc.returncode == 5 and out.strip() == "ran-lm-exe", out


@pytest.mark.skipif(os.name != "nt", reason="lm.cmd runs on Windows")
def test_the_replacement_test_is_sensitive_a_file_without_padding_breaks_an_old_window(tmp_path):
    """The counter-probe of the test above: a new lm.cmd WITHOUT the padding really disturbs a window on the old launcher
    (cmd goes on at the old offset in whatever it finds), so the passing test proves the padding works."""
    tree = tree_in(tmp_path)
    system = tree / "system"
    rt = system / "runtime-windows-x86_64"
    (rt / "Scripts").mkdir(parents=True)
    shutil.copy(os.environ["COMSPEC"], rt / "Scripts" / "lm.exe")
    (rt / "lm-root.txt").write_bytes(str(tree).encode("oem"))
    (system / "lm.cmd").write_bytes(OLD_LAUNCHERS[-1].read_bytes())
    proc = subprocess.Popen([str(system / "lm.cmd"), "/c", "echo.>started.txt & ping -n 6 127.0.0.1 >nul & echo ran-lm-exe & exit 5"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", cwd=tmp_path)
    wait_for(tmp_path / "started.txt")
    (system / "lm.cmd.new").write_bytes(b"@echo off\r\n" + b"echo GARBAGE-LINE-OF-THE-NEW-FILE\r\n" * 400)
    os.replace(system / "lm.cmd.new", system / "lm.cmd")
    out, _ = proc.communicate(timeout=60)
    assert "GARBAGE" in out or proc.returncode != 5, out


@pytest.mark.skipif(os.name != "nt", reason="lm.cmd runs on Windows")
def test_a_window_on_the_new_launcher_survives_the_launcher_being_replaced_again(tmp_path):
    tree = tree_in(tmp_path)
    lau = Launcher("windows")
    lau.fake_runtime(tree)
    system = tree / "system"
    proc = subprocess.Popen([str(system / "lm.cmd"), "/c", "echo.>started.txt & ping -n 6 127.0.0.1 >nul & echo ran-lm-exe & exit 6"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace", cwd=tmp_path)
    wait_for(tmp_path / "started.txt")
    (system / "lm.cmd.new").write_bytes(b"@echo off\r\necho GARBAGE\r\n" + b"echo GARBAGE\r\n" * 400)
    os.replace(system / "lm.cmd.new", system / "lm.cmd")
    out, _ = proc.communicate(timeout=60)
    assert proc.returncode == 6 and out.strip() == "ran-lm-exe", out


def test_lm_cmd_is_well_formed():
    """Every goto/call target of lm.cmd exists exactly once (a broken splice cannot be run on Linux)."""
    text = template("lm.cmd").decode("ascii")
    labels = re.findall(r"^:(\w+)\r?$", text, re.M)
    assert len(labels) == len(set(labels)), "duplicate labels"
    assert set(re.findall(r"(?:goto|call) :(\w+)", text)) <= set(labels)
    for needed in ("being built in another lm window", "runtimes\\windows-", ".current", "runtime-stamp.txt", "lm-stamp.txt",
                   "venv --python", "pip install", "move /y", "pythonw.exe"):
        assert needed in text, needed
    assert text.split("\r\n")[1].startswith("rem") and "goto :lm_main" in text.split("\r\n")[:20]


# ------------------------------------------------------------------ names that break shell scripts
# What a fake uv can take: a .cmd fake cannot hold , ; = & ^ (CALL), and MSYS rewrites ; and quotes in arguments of
# Windows programs. The launchers themselves are checked with the full set by the tests that need no fake uv.
ODD = {"windows": "a !d! 'e' (i)",
       "posix": "a $HOME (b)&c ^d !e,f=g" if os.name == "nt" else "it's $HOME `x` (a;b)&c ^d !e,f=g"}
ODD_FULL = "a&b ^c !d! 'e' ,f =g ;h (i)"


def test_odd_folder_names_do_not_break_the_launcher(lau, tmp_path, uv):
    tree = tree_in(tmp_path, ODD[lau.kind])
    record(tree, REQ)
    first = start(lau, tree, uv)
    assert first.returncode == 0, first.stderr
    assert lau.is_path(environ_of(first.stdout)["LIGHTMIND_HOME"], tree)
    assert verbs(uv) == ["python install", "venv", "pip install"]
    assert start(lau, tree, uv).returncode == 0 and len(verbs(uv)) == 3
    moved = tmp_path / ("moved " + ODD[lau.kind])
    shutil.copytree(tree, moved)
    assert start(lau, moved, uv).returncode == 0 and len(verbs(uv)) == 6   # rebuilt once, here too
    assert lau.marks(moved)
    assert lau.is_path(calls(uv)[-1]["args"][-1].rsplit("[", 1)[0], moved) is False


# ------------------------------------------------------------------ failures
def test_failed_build_is_reported_and_leaves_no_marker(lau, tree, uv):
    record(tree, REQ)
    done = start(lau, tree, uv, LM_FAKE_FAIL="pip")
    assert done.returncode == 1 and "could not be built" in done.stderr
    assert not lau.marks(tree) and not lau.runtime_dir(tree).exists() and lau.builds(tree) == []      # the partial runtime is dropped, too
    assert start(lau, tree, uv).returncode == 0                            # the next start tries again, and works
    assert verbs(uv).count("venv") == 2


@pytest.mark.parametrize("fail", ["python", "venv"])
def test_each_failing_step_stops_the_build(lau, tree, uv, fail):
    record(tree, REQ)
    done = start(lau, tree, uv, LM_FAKE_FAIL=fail)
    assert done.returncode == 1 and "could not be built" in done.stderr
    assert "pip install" not in verbs(uv)


def test_missing_record_says_what_to_do(lau, tree, uv):
    (tree / "system" / "state" / INSTALL_SOURCE).unlink()
    done = lau.run(tree, env=uv)
    assert done.returncode == 1 and INSTALL_SOURCE in done.stderr and verbs(uv) == []


@pytest.mark.parametrize("kind", ["folder", "wheel"])
def test_source_that_is_gone_says_so(lau, tree, uv, kind):
    record(tree, f"{kind} " + lau.seen(tree.parent / "nowhere"))
    done = lau.run(tree, env=uv)
    assert done.returncode == 1 and "does not exist on this machine" in done.stderr and verbs(uv) == []


def test_missing_uv_is_an_error_not_a_crash(lau, tree, uv):
    record(tree, REQ)
    done = lau.run(tree, env={**uv, "LM_UV": lau.seen(tree.parent / "no-such-uv")})
    assert done.returncode == 1 and "uv is not available" in done.stderr


# ------------------------------------------------------------------ uv into system/bin
@pytest.mark.skipif(shutil.which("uv") is None, reason="needs a real uv on PATH to copy")
def test_uv_on_path_is_copied_into_system_bin(tree):
    lau = Launcher("windows" if os.name == "nt" else "posix")
    real = Path(shutil.which("uv"))
    lau.fake_runtime(tree)
    done = lau.run(tree, *lau.env_args(), env={"PATH": os.environ["PATH"]})
    assert done.returncode == 0, done.stderr
    copy = tree / "system" / "bin" / lau.uv_name()
    assert copy.is_file() and copy.stat().st_size == real.stat().st_size
    if lau.kind == "windows" and (real.parent / "uvx.exe").is_file():
        assert (tree / "system" / "bin" / "uvx.exe").is_file()
    assert subprocess.run([str(copy), "--version"], capture_output=True).returncode == 0
    assert not (tree / "system" / "wheels").exists()                       # copying uv does not start a build


def test_uv_is_not_copied_when_the_tree_has_one(lau, tree, uv):
    lau.fake_runtime(tree)
    (tree / "system" / "bin").mkdir()
    mine = tree / "system" / "bin" / lau.uv_name()
    mine.write_bytes(b"#!/bin/sh\necho mine\n")
    mine.chmod(0o755)
    env = {k: v for k, v in uv.items() if k != "LM_UV"}                     # no override: the launcher looks in system/bin
    assert lau.run(tree, *lau.env_args(), env=env).returncode == 0
    assert mine.read_bytes() == b"#!/bin/sh\necho mine\n"


# ------------------------------------------------------------------ lm path
@pytest.fixture
def regkey():
    """A scratch key below HKCU for the launcher's `lm path` (LM_PATH_REGKEY); the real user Path is never touched."""
    if os.name != "nt":
        pytest.skip("the user Path is a Windows registry value")
    import winreg
    key = f"Software\\LightmindTest-{uuid.uuid4().hex}"
    yield key
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
    except FileNotFoundError:
        pass


def test_lm_path_adds_system_to_the_user_path_once_and_keeps_the_value_type(tree, regkey):
    import winreg
    system = str(tree / "system")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, regkey) as k:
        winreg.SetValueEx(k, "Path", 0, winreg.REG_EXPAND_SZ, "%USERPROFILE%\\bin;C:\\Tools;;")

    def value():
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, regkey) as k:
            return winreg.QueryValueEx(k, "Path")
    lau, env = Launcher("windows"), {"LM_PATH_REGKEY": regkey}
    done = lau.run(tree, "path", env=env)
    assert done.returncode == 0 and "added" in done.stdout, done.stdout + done.stderr
    text, kind = value()
    assert kind == winreg.REG_EXPAND_SZ and text == f"%USERPROFILE%\\bin;C:\\Tools;{system}"   # unexpanded, typed, no empty entry
    done = lau.run(tree, "path", env=env)                                  # a second time: nothing to do
    assert done.returncode == 0 and "already" in done.stdout and value() == (text, kind)
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, regkey, 0, winreg.KEY_SET_VALUE) as k:   # same folder, spelled differently
        winreg.SetValueEx(k, "Path", 0, winreg.REG_SZ, "C:\\Tools;" + system.upper() + "\\")
    assert "already" in lau.run(tree, "path", env=env).stdout and value()[1] == winreg.REG_SZ


def test_lm_path_creates_the_value_when_there_is_none(tree, regkey):
    import winreg
    done = Launcher("windows").run(tree, "path", env={"LM_PATH_REGKEY": regkey})
    assert done.returncode == 0, done.stderr
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, regkey) as k:
        assert winreg.QueryValueEx(k, "Path") == (str(tree / "system"), winreg.REG_EXPAND_SZ)


def test_lm_path_on_posix_prints_the_line_and_writes_nothing(tmp_path):
    if git_sh() is None:
        pytest.skip("needs sh")
    lau = Launcher("posix")
    tree = tree_in(tmp_path, ODD["posix"])
    uv = lau.fake_uv(tmp_path / "fake-uv")
    before = sorted(p for p in tmp_path.rglob("*") if "fake-uv" not in p.parts)
    done = lau.run(tree, "path", env=uv)
    assert done.returncode == 0 and verbs(uv) == [] and not lau.runtime_dir(tree).exists()
    assert sorted(p for p in tmp_path.rglob("*") if "fake-uv" not in p.parts) == before
    line = [ln for ln in done.stdout.splitlines() if "export PATH=" in ln][0].strip()
    # sh itself reads the line back: the value must be exactly the folder, whatever characters it holds
    echoed = subprocess.run([git_sh(), "-c", line.replace('$PATH', '') + '; printf %s "$PATH"'],
                            capture_output=True, text=True, env={**os.environ, "PATH": ""}).stdout
    assert echoed == lau.seen(tree / "system") + ":"


@pytest.mark.skipif(shutil.which("uv") is None or os.name != "nt", reason="needs a real uv.exe to copy")
def test_windows_launcher_survives_every_character_a_folder_name_may_hold(tmp_path):
    """Fast path plus the uv copy, with a real uv.exe (no CALL involved): & ^ , ; = ! ' and parentheses."""
    lau = Launcher("windows")
    tree = tree_in(tmp_path, ODD_FULL)
    lau.fake_runtime(tree)
    done = lau.run(tree, *lau.env_args(), env={"PATH": os.environ["PATH"]})
    assert done.returncode == 0, done.stderr
    assert lau.is_path(environ_of(done.stdout)["LIGHTMIND_HOME"], tree)
    assert (tree / "system" / "bin" / "uv.exe").is_file()
    moved = tmp_path / ("m " + ODD_FULL)
    shutil.copytree(tree, moved)
    assert not lau.marks(moved)                                           # the copy sees a moved tree: it would rebuild
