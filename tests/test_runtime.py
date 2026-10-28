"""runtime.py and what hangs on it: the pointer, the pending stamp, who is in use, the launcher refresh, `lm update` that
builds the new runtime at once, `lm check`'s runtime line, and the processes lm starts by itself.  The launchers that
read the same layout are in test_launcher.py."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lightmind import checks, cli, init, runtime
from lightmind.files import Home, platform_tag
from lightmind.init import update_tree

TAG = "-".join(platform_tag())
POSIX = os.name != "nt"


@pytest.fixture
def home(tree) -> Home:
    return Home(tree)


def make_build(home: Home, name: str = "r1", *, stamp: str = "", point: bool = True, version: str = "") -> Path:
    """A stand-in build `name` (lm program, stamp, lock file), the current one when `point`."""
    rt = runtime.runtimes_dir(home) / name
    exe = runtime.exe_of(rt)
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"x")
    runtime.python_of(rt).write_bytes(b"x")
    (rt / "lm-stamp.txt").write_text(stamp, encoding="ascii")
    (rt / "lm-root.txt").write_text(str(home.root), encoding="utf-8")
    if POSIX:
        (rt / "lm-use.lock").write_bytes(b"")
    if version:
        site = rt / "lib" / "python3.12" / "site-packages" if POSIX else rt / "Lib" / "site-packages"
        (site / f"lightmind-{version}.dist-info").mkdir(parents=True)
    if point:
        runtime.pointer_file(home).parent.mkdir(parents=True, exist_ok=True)
        runtime.pointer_file(home).write_text(name, encoding="ascii")
    return rt


def make_legacy(home: Home) -> Path:
    rt = runtime.legacy_dir(home)
    exe = runtime.exe_of(rt)
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"x")
    runtime.python_of(rt).write_bytes(b"x")
    (rt / "lm-root.txt").write_text(str(home.root), encoding="utf-8")
    return rt


# ------------------------------------------------------------------ the layout and the pointer
def test_the_layout_is_one_folder_per_machine_type_with_a_pointer_next_to_it(home):
    system = home.path("system")
    assert runtime.runtimes_dir(home) == system / "runtimes" / TAG
    assert runtime.pointer_file(home) == system / "runtimes" / f"{TAG}.current"
    assert runtime.legacy_dir(home) == system / f"runtime-{TAG}"
    assert runtime.current(home) is None


def test_the_pointer_names_the_current_build(home):
    make_build(home, "r1", point=False)
    r2 = make_build(home, "r2")
    assert runtime.current(home) == r2
    runtime.pointer_file(home).write_text("r1\n", encoding="ascii")            # a trailing newline is fine
    assert runtime.current(home).name == "r1"


@pytest.mark.parametrize("text", ["..", "../r1", "r1/..", "x", "", "r", "r1.drop", "R1 ", "r1\\..\\r1"])
def test_a_pointer_that_is_not_a_plain_build_name_is_not_followed(home, text):
    make_build(home, "r1", point=False)
    runtime.pointer_file(home).parent.mkdir(parents=True, exist_ok=True)
    runtime.pointer_file(home).write_text(text, encoding="ascii")
    assert runtime.pointed(home) is None and runtime.current(home) is None


def test_a_pointer_to_a_build_that_is_gone_falls_back_to_the_legacy_runtime(home):
    legacy = make_legacy(home)
    runtime.pointer_file(home).parent.mkdir(parents=True, exist_ok=True)
    runtime.pointer_file(home).write_text("r7", encoding="ascii")
    assert runtime.current(home) == legacy
    r1 = make_build(home, "r1")
    assert runtime.current(home) == r1                                         # a pointed build beats the legacy one


def test_all_runtimes_lists_the_builds_by_number_then_the_legacy_one(home):
    for name in ("r10", "r2", "r1"):
        make_build(home, name, point=False)
    (runtime.runtimes_dir(home) / "notabuild").mkdir()
    legacy = make_legacy(home)
    assert [p.name for p in runtime.all_runtimes(home)] == ["r1", "r2", "r10", legacy.name]


def test_python_for_new_process_is_the_newest_runtimes_and_falls_back_to_this_one(home):
    assert runtime.python_for_new_process(home) == sys.executable
    rt = make_build(home, "r3")
    assert runtime.python_for_new_process(home) == str(runtime.python_of(rt))
    runtime.python_of(rt).unlink()
    assert runtime.python_for_new_process(home) == sys.executable


def test_a_process_outside_the_runtime_folders_counts_as_the_newest(home):
    assert runtime.is_newest(home)                                             # a checkout, as in these tests


def test_a_process_in_an_older_runtime_is_not_the_newest(home, monkeypatch):
    old = make_build(home, "r1", point=False)
    make_build(home, "r2")
    monkeypatch.setattr(runtime, "own", lambda: old)
    assert not runtime.is_newest(home)
    monkeypatch.setattr(runtime, "own", lambda: runtime.runtimes_dir(home) / "r2")
    assert runtime.is_newest(home)


# ------------------------------------------------------------------ the pending signal
def test_nothing_is_pending_until_lm_update_asks(home):
    assert runtime.pending(home)                                               # no runtime at all
    make_build(home, "r1")
    assert not runtime.pending(home) and runtime.wanted_stamp(home) == ""
    token = runtime.request_rebuild(home)
    assert token and runtime.wanted_stamp(home) == token and runtime.pending(home)
    (runtime.runtimes_dir(home) / "r1" / "lm-stamp.txt").write_text(token, encoding="ascii")    # a build for exactly this
    assert not runtime.pending(home)
    assert runtime.request_rebuild(home) != token and runtime.pending(home)    # asking again is a new request
    assert not list(runtime.stamp_file(home).parent.glob("*.tmp"))             # written through a temp file that was renamed away


def test_the_legacy_runtime_counts_as_built_without_a_stamp(home):
    make_legacy(home)
    assert not runtime.pending(home)
    runtime.request_rebuild(home)
    assert runtime.pending(home)


def test_asking_touches_no_runtime_folder(home):
    rt = make_build(home, "r1")
    before = sorted(str(p.relative_to(rt)) for p in rt.rglob("*"))
    marker = (rt / "lm-root.txt").read_bytes()
    runtime.request_rebuild(home)
    assert sorted(str(p.relative_to(rt)) for p in rt.rglob("*")) == before and (rt / "lm-root.txt").read_bytes() == marker


def test_the_version_is_read_from_the_dist_info_without_importing(home):
    rt = make_build(home, "r1", version="9.8.7")
    assert runtime.version_of(rt) == "9.8.7"
    assert runtime.version_of(make_build(home, "r2", point=False)) == ""


def test_has_any_runtime_looks_at_every_machine_type(home):
    assert not runtime.has_any_runtime(home)
    (home.path("system") / "runtime-sunos-sparc").mkdir()
    assert runtime.has_any_runtime(home)
    shutil.rmtree(home.path("system") / "runtime-sunos-sparc")
    (home.path("system") / "runtimes" / "linux-arm64" / "r1").mkdir(parents=True)
    assert runtime.has_any_runtime(home)


# ------------------------------------------------------------------ in use
@pytest.mark.skipif(not POSIX, reason="flock")
def test_a_runtime_is_in_use_while_a_process_holds_its_shared_lock(home):
    import fcntl
    rt = make_build(home, "r1")
    assert not runtime.in_use(rt)
    fd = os.open(rt / "lm-use.lock", os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        assert runtime.in_use(rt)
    finally:
        os.close(fd)
    assert not runtime.in_use(rt)                                              # and free again when the holder is gone


@pytest.mark.skipif(not POSIX or not Path("/proc/self").is_dir(), reason="needs /proc")
def test_the_legacy_runtime_without_a_lock_file_is_in_use_while_a_process_runs_from_it(home):
    legacy = make_legacy(home)
    hold = legacy / "bin" / "hold"
    hold.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8", newline="\n")
    hold.chmod(0o755)
    assert not runtime.in_use(legacy)
    proc = subprocess.Popen([str(hold)])
    try:
        import time
        time.sleep(0.5)
        assert runtime.in_use(legacy)
    finally:
        proc.kill()
        proc.wait()
    assert not runtime.in_use(legacy)


@pytest.mark.skipif(POSIX, reason="a running program cannot be opened for writing on Windows")
def test_a_runtime_is_in_use_while_its_program_runs_on_windows(home):
    rt = make_build(home, "r1")
    exe = runtime.exe_of(rt)
    shutil.copy(os.environ["COMSPEC"], exe)
    assert not runtime.in_use(rt)
    proc = subprocess.Popen([str(exe), "/c", "ping -n 20 127.0.0.1 >nul"], stdout=subprocess.DEVNULL)
    try:
        assert runtime.in_use(rt)
    finally:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True)
        proc.wait()
    assert not runtime.in_use(rt)


@pytest.mark.skipif(not POSIX, reason="flock")
def test_every_lm_process_holds_the_lock_of_its_own_runtime(home, monkeypatch):
    """A process lm starts by itself does not inherit the launcher's lock; main() takes it, so it counts."""
    rt = make_build(home, "r1")
    monkeypatch.setattr(runtime, "own", lambda: rt)
    monkeypatch.setattr(runtime, "_hold_fd", None)
    runtime.hold()
    try:
        assert runtime.in_use(rt)
    finally:
        os.close(runtime._hold_fd)
    bare = home.path("system") / "bare"
    bare.mkdir()
    monkeypatch.setattr(runtime, "own", lambda: bare)
    monkeypatch.setattr(runtime, "_hold_fd", None)
    runtime.hold()
    assert runtime._hold_fd is None                                            # no lock file (a checkout): silent


# ------------------------------------------------------------------ lm check
def test_the_runtime_line_is_unchanged_in_a_tree_without_runtimes(home):
    line = checks.check_runtime(home)[0]
    assert line.status == checks.OK and "runtime r" not in line.detail and "older runtime" not in line.detail


def test_the_runtime_line_names_the_runtime_and_counts_the_older_ones_in_use(home, monkeypatch):
    old = make_build(home, "r1", point=False, version="0.1.0")
    make_build(home, "r2", version="0.2.0")
    monkeypatch.setattr(runtime, "own", lambda: old)
    monkeypatch.setattr(runtime, "in_use", lambda rt: rt.name == "r1")
    detail = checks.check_runtime(home)[0].detail
    assert "runtime r1" in detail and "new windows use r2 (0.2.0)" in detail and "1 older runtime still in use" in detail
    assert checks.check_runtime(home)[0].status == checks.OK                   # normal, not a warning
    monkeypatch.setattr(runtime, "own", lambda: runtime.runtimes_dir(home) / "r2")
    monkeypatch.setattr(runtime, "in_use", lambda rt: False)
    detail = checks.check_runtime(home)[0].detail
    assert "runtime r2" in detail and "new windows use" not in detail and "no older runtime in use" in detail


def test_two_older_runtimes_in_use_are_counted(home, monkeypatch):
    make_build(home, "r1", point=False)
    make_build(home, "r2", point=False)
    make_build(home, "r3")
    monkeypatch.setattr(runtime, "own", lambda: runtime.runtimes_dir(home) / "r3")
    monkeypatch.setattr(runtime, "in_use", lambda rt: True)
    assert "2 older runtimes still in use" in checks.check_runtime(home)[0].detail


# ------------------------------------------------------------------ the launchers are replaced by temp file and rename
def test_changed_launchers_are_replaced_through_a_temp_file_and_the_rest_is_left_alone(home):
    system = home.path("system")
    (system / "lm.cmd").write_bytes(b"old cmd launcher\r\n")
    (system / "lm").write_bytes(b"#!/bin/sh\necho old\n")
    (system / "lmq").write_bytes(b"my own alias")                              # the aliases are not engine files: left alone
    untouched = (system / "lmq").stat().st_mtime_ns
    held = open(system / "lm", "rb") if POSIX else None                         # a running shell holds its script open
    try:
        before = os.stat(system / "lm").st_ino
        changed = runtime.refresh_launchers(home)
        assert changed == ["lm", "lm.cmd"]
        shipped = init.templates()
        by_name = {rel[len("system/"):]: node for rel, node in shipped if rel.startswith("system/")}
        assert (system / "lm").read_bytes() == by_name["lm"].read_bytes()
        assert (system / "lm.cmd").read_bytes() == by_name["lm.cmd"].read_bytes()
        if POSIX:
            assert os.stat(system / "lm").st_ino != before                      # a new file: never written in place
            assert held.read() == b"#!/bin/sh\necho old\n"                       # the running shell still reads the old one
            assert os.access(system / "lm", os.X_OK)
    finally:
        if held:
            held.close()
    assert (system / "lmq").stat().st_mtime_ns == untouched
    assert not [p for p in system.iterdir() if p.name.endswith(".new")]
    assert runtime.refresh_launchers(home) == []                                # and a second time there is nothing to do


def test_update_refreshes_the_launchers_only_from_the_newest_runtime(home, monkeypatch):
    system = home.path("system")
    (system / "lm.cmd").write_bytes(b"newer launcher\r\n")
    old = make_build(home, "r1", point=False)
    make_build(home, "r2")
    monkeypatch.setattr(runtime, "own", lambda: old)                            # an old window runs `lm update`
    created = update_tree(home.root)
    assert created.launchers == [] and (system / "lm.cmd").read_bytes() == b"newer launcher\r\n"
    monkeypatch.setattr(runtime, "own", lambda: runtime.runtimes_dir(home) / "r2")
    created = update_tree(home.root)
    assert "lm.cmd" in created.launchers and (system / "lm.cmd").read_bytes() != b"newer launcher\r\n"


def test_the_launchers_are_refreshed_before_a_new_runtime_is_asked_for(home, monkeypatch):
    """A build that follows must find the new launcher: the order is launchers, then the stamp."""
    make_build(home, "r1")
    (home.path("state") / "install-source.txt").write_text("requirement lightmind[tui]\n", encoding="utf-8")
    order = []
    monkeypatch.setattr(runtime, "refresh_launchers", lambda h: order.append("launchers") or [])
    monkeypatch.setattr(runtime, "request_rebuild", lambda h: order.append("stamp") or "t")
    update_tree(home.root)
    assert order == ["launchers", "stamp"]


# ------------------------------------------------------------------ lm update builds the new runtime at once
@pytest.fixture
def update_env(tree, monkeypatch):
    """`lm update` with the build step replaced: records the call and says what it should answer."""
    box = {"calls": 0, "result": runtime.BuildResult("built", "exit code 0")}

    def fake(home, **kw):
        box["calls"] += 1
        return box["result"]
    monkeypatch.setattr(runtime, "build_now", fake)
    monkeypatch.setattr(cli, "_offer_tick", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_follow_up_update", lambda home: None)
    return box


def test_lm_update_builds_the_new_runtime_at_its_end(tree, update_env, capsys):
    (tree / "system" / "state" / "install-source.txt").write_text("requirement lightmind[tui]\n", encoding="utf-8")
    make_build(Home(tree), "r1")
    assert cli.main(["--home", str(tree), "update"]) == 0
    assert update_env["calls"] == 1
    assert runtime.pending(Home(tree))                                          # the stamp was written before the build was asked for
    assert "added" in capsys.readouterr().out


def test_lm_update_says_so_when_the_build_fails_and_exits_with_1(tree, update_env, capsys):
    update_env["result"] = runtime.BuildResult("failed", "exit code 1")
    assert cli.main(["--home", str(tree), "update"]) == 1
    out = capsys.readouterr().out
    assert "could not be built" in out and "next start tries again" in out


def test_lm_update_that_skips_the_build_leaves_it_to_the_next_start(tree, update_env, capsys):
    update_env["result"] = runtime.BuildResult("skipped", "LM_NO_BUILD is set")
    assert cli.main(["--home", str(tree), "update"]) == 0
    assert "the next start builds it" in capsys.readouterr().out


def test_lm_update_no_build_does_not_call_the_launcher(tree, update_env):
    assert cli.main(["--home", str(tree), "update", "--no-build"]) == 0
    assert update_env["calls"] == 0


def test_the_follow_up_update_adds_files_but_neither_asks_nor_builds(tree, update_env):
    make_build(Home(tree), "r1")
    (tree / "system" / "state" / "install-source.txt").write_text("requirement lightmind[tui]\n", encoding="utf-8")
    assert cli.main(["--home", str(tree), "update", "--after-build"]) == 0
    assert update_env["calls"] == 0 and not runtime.stamp_file(Home(tree)).exists()


def test_update_from_a_folder_asks_for_the_runtime_and_builds_it(tree, update_env, monkeypatch, tmp_path, capsys):
    wheel = tmp_path / "lightmind-9.9-py3-none-any.whl"
    wheel.write_bytes(b"w")
    seen = []
    monkeypatch.setattr(init, "install_from_folder", lambda src, target=None: seen.append(src) or (wheel, "tok"))
    assert cli.main(["--home", str(tree), "update", "--from", str(tmp_path)]) == 0
    assert seen == [str(tmp_path)] and update_env["calls"] == 1
    assert "built lightmind-9.9-py3-none-any.whl" in capsys.readouterr().out


def test_after_a_new_runtime_stands_the_new_version_runs_the_update_once_more(tree, monkeypatch, capsys):
    """Template files and launchers must come from the NEW version, so its runtime runs `update --after-build`."""
    home = Home(tree)
    monkeypatch.setattr(cli, "_offer_tick", lambda *a, **k: None)
    new = make_build(home, "r2")
    monkeypatch.setattr(runtime, "build_now", lambda h, **k: runtime.BuildResult("built", "exit code 0"))
    argvs = []

    def fake_run(argv, **kw):
        argvs.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout='{"created": ["x/new.md"], "launchers": ["lm.cmd"]}', stderr="")
    monkeypatch.setattr(cli.subprocess, "run", fake_run)
    assert cli.main(["--home", str(tree), "update"]) == 0
    assert argvs and argvs[0][0] == str(runtime.python_of(new)) and argvs[0][-2:] == ["update", "--after-build"]
    out = capsys.readouterr().out
    assert "x/new.md" in out and "launchers updated: lm.cmd" in out and "runtime r2 is ready" in out


# ------------------------------------------------------------------ build_now runs the tree's launcher
@pytest.fixture
def fake_launcher(home, monkeypatch, tmp_path):
    monkeypatch.delenv("LM_NO_BUILD", raising=False)
    out = tmp_path / "launcher-args.txt"
    monkeypatch.setenv("LM_TEST_OUT", str(out))
    launcher = runtime.launcher_of(home)
    if POSIX:
        launcher.write_text('#!/bin/sh\necho "$@" > "$LM_TEST_OUT"\nexit ${LM_TEST_RC:-0}\n', encoding="utf-8", newline="\n")
        launcher.chmod(0o755)
    else:
        launcher.write_bytes(b'@echo off\r\necho %* > "%LM_TEST_OUT%"\r\nexit /b %LM_TEST_RC%\r\n')
    monkeypatch.setenv("LM_TEST_RC", "0")
    return out


def test_build_now_asks_the_launcher_to_build_and_nothing_else(home, fake_launcher):
    res = runtime.build_now(home)
    assert res.status == "built" and fake_launcher.read_text(encoding="utf-8").strip() == "build"


def test_build_now_reports_a_failing_launcher(home, fake_launcher, monkeypatch):
    monkeypatch.setenv("LM_TEST_RC", "3")
    res = runtime.build_now(home)
    assert res.status == "failed" and "3" in res.detail


def test_build_now_is_skipped_without_a_launcher_or_by_the_switch(home, monkeypatch):
    monkeypatch.delenv("LM_NO_BUILD", raising=False)
    runtime.launcher_of(home).unlink()
    assert runtime.build_now(home).status == "skipped"
    monkeypatch.setenv("LM_NO_BUILD", "1")
    assert runtime.build_now(home).status == "skipped"


# ------------------------------------------------------------------ processes lm starts by itself
def test_no_code_starts_lm_again_with_its_own_interpreter():
    """A detached run, a wake or a timer must start in the newest runtime (python_for_new_process), or a new
    version would only reach windows that are opened by hand."""
    src = Path(__file__).resolve().parents[1] / "src" / "lightmind"
    allowed = {"timers.py": 1}                                    # the OS beat's fallback when the tree has no launcher
    for name in ("cli.py", "timers.py", "wake.py", "watch.py"):
        text = (src / name).read_text(encoding="utf-8")
        assert text.count('[sys.executable, "-m", "lightmind.cli"') == allowed.get(name, 0), name
