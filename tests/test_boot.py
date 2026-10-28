"""Health checks (fakes for ssh, git and credentials), the boot screen (Pilot), `lm check`, lazy imports, `lm update --from`."""
import json
import re
import subprocess
import sys
import threading
import time

import pytest

from lightmind import checks, cli, settings as S
from lightmind.tui import boot as boot_mod
from lightmind.tui.boot import BootScreen
from lightmind.tui.screens import MapScreen

from test_tui_support import running, until

L = checks.Line
BAD = "---" + chr(10) + "modes:" + chr(10) + "  a: [x" + chr(10) + "---" + chr(10)        # nested and not valid YAML: the lenient flat reader refuses it too


@pytest.fixture
def calls(monkeypatch):
    """`checks._run` replaced: every git and ssh call is recorded and answered from a table."""
    seen, answers = [], {}

    def fake(cmd, timeout=checks.NET_TIMEOUT, cwd=None):
        seen.append(cmd)
        for key, ans in answers.items():
            if key in " ".join(cmd):
                return ans
        return 1, "no answer"
    monkeypatch.setattr(checks, "_run", fake)
    fake.seen, fake.answers = seen, answers
    return fake


def test_runtime_config_and_providers_lines(home):
    got = {x.label: x for x in checks.check_runtime(home) + checks.check_config(home) + checks.check_providers(home)}
    assert got["runtime"].status == checks.OK and "python" in got["runtime"].detail
    assert got["config"].status == checks.OK
    assert any(x.startswith("account ") for x in got)


def test_a_broken_permissions_md_is_a_fail(home):
    (home.root / "permissions.md").write_text(BAD, "utf-8")
    assert any(x.status == checks.FAIL for x in checks.check_config(home))


def test_provider_line_follows_the_account_state(home):
    from lightmind.accounts import list_accounts
    rows = {r["name"]: r for r in list_accounts(home)}
    lines = checks.check_providers(home)
    for name, r in rows.items():
        line = next(x for x in lines if x.label == f"account {name}")
        assert (line.status == checks.OK) == (r["state"] in ("ready", "no-key-needed"))


def test_broken_skill_is_a_warning(home):
    d = home.path("skills") / "kaputt"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(BAD, "utf-8")
    line = checks.check_skills(home)[0]
    assert line.status == checks.WARN and "kaputt" in line.detail


def test_servers_local_is_ok_and_remote_uses_ssh_with_batchmode(home, calls):
    far = home.path("servers") / "far"
    far.mkdir(parents=True, exist_ok=True)
    (far / "info.md").write_text("---\nhost: 10.0.0.9\nssh: far-alias\n---\n# far\n", "utf-8")
    here = home.path("servers") / "here"
    here.mkdir(exist_ok=True)
    (here / "info.md").write_text("---\nhost: local\n---\n# here\n", "utf-8")
    assert checks.check_server(home, "here")[0].status == checks.OK
    calls.answers["far-alias"] = (0, "")
    assert checks.check_server(home, "far")[0].status == checks.OK
    cmd = next(c for c in calls.seen if c[0] == "ssh")
    assert "BatchMode=yes" in cmd and "ConnectTimeout=3" in cmd and cmd[-2:] == ["far-alias", "true"]
    calls.answers["far-alias"] = (-1, "timeout")
    bad = checks.check_server(home, "far")[0]
    assert bad.status == checks.WARN and "timed out" in bad.detail


def test_vault_without_repo_and_with_a_remote_that_is_ahead(home, calls):
    vault = home.path("vault")
    vault.mkdir(parents=True, exist_ok=True)
    (vault / "a.md").write_text("x", "utf-8")
    calls.answers["rev-parse --show-toplevel"] = (1, "not a repo")
    line = checks.check_vault(home)[0]
    assert line.status == checks.INFO and "notes" in line.detail
    calls.answers.clear()
    calls.answers.update({"rev-parse --show-toplevel": (0, str(vault)), "branch --show-current": (0, "main"),
                          "status --porcelain": (0, ""), "rev-list": (0, "0 2"), "--abbrev-ref": (0, "origin/main"),
                          "fetch": (0, "")})
    line = checks.check_vault(home)[0]
    assert line.status == checks.OK and "2 behind" in line.detail and "main" in line.detail
    calls.answers["fetch"] = (-1, "timeout")
    assert checks.check_vault(home)[0].status == checks.WARN


def test_sync_is_info_when_the_tree_is_no_repo(home, calls):
    calls.answers["rev-parse --show-toplevel"] = (128, "fatal")
    line = checks.check_sync(home)[0]
    assert line.status == checks.INFO and "not set up" in line.detail


def test_a_crashing_group_is_a_fail_line_not_a_crash():
    from concurrent.futures import ThreadPoolExecutor

    def boom():
        raise RuntimeError("kaput")
    with ThreadPoolExecutor(1) as pool:
        p = checks.Pending("x", pool.submit(boom))
        p.future.exception()
        assert p.lines()[0].status == checks.FAIL


def test_boot_mode_reading(home):
    assert checks.boot_mode(home) == "true"
    p = home.root / "config.md"                              # ui.boot is a setting of config.md
    for word in ("false", "brief", "true"):
        p.write_text(re.sub(r"  boot: \w+", f"  boot: {word}", p.read_text("utf-8"), count=1), "utf-8")
        assert checks.boot_mode(home) == word


def test_lm_check_prints_the_list_and_exits_by_fail(home, calls, capsys, monkeypatch):
    monkeypatch.setattr(checks, "check_providers", lambda h: [L("ok", "providers", "stub")])      # the overlay tree has no usable login
    assert cli.main(["--home", str(home.root), "check"]) == 0
    out = capsys.readouterr().out
    assert "[ OK ]" in out and "runtime" in out
    cli.main(["--home", str(home.root), "--json", "check"])
    assert json.loads(capsys.readouterr().out)[0]["label"] == "runtime"
    (home.root / "permissions.md").write_text(BAD, "utf-8")
    assert cli.main(["--home", str(home.root), "check"]) == 1


# ------------------------------------------------------------------ the boot screen
@pytest.fixture
def shut():
    """Set at the end of a test: a fake check that is still 'running' (3 s, 5 s) wakes up and the app does not wait for it."""
    ev = threading.Event()
    yield ev
    ev.set()


@pytest.fixture
def short_hold(monkeypatch):
    """The finished checklist holds 0.7 s (1.8 s with a warning) before the landing: the tests that wait for it use 50 ms and 100 ms."""
    monkeypatch.setattr(boot_mod, "HOLD", 0.05)
    monkeypatch.setattr(boot_mod, "HOLD_BAD", 0.1)


def fake_groups(monkeypatch, plan, shut=None):
    """plan: [(name, delay, [Line, ...])]"""
    def mk(delay, lines):
        def f():
            if shut is not None and delay >= 1.0:
                shut.wait(delay)
            else:
                time.sleep(delay)
            return lines
        return f
    monkeypatch.setattr(checks, "groups", lambda home: [(n, mk(d, ls)) for n, d, ls in plan])


def boot_text(app):
    return app.screen.query_one("#boot").content.plain


def set_ui(home, **kv):
    """ui.* settings of config.md, line by line (`boot`, `boot_wait`)."""
    p = home.root / "config.md"
    p.write_bytes(S.edit_text(p.read_bytes().decode("utf-8"), {("ui", k): v for k, v in kv.items()}).encode("utf-8"))


async def test_boot_reveals_lines_in_order_and_then_shows_the_landing(home, monkeypatch):
    monkeypatch.delenv("LM_NO_BOOT")
    set_ui(home, boot_wait=False)                              # the old way: it closes by itself
    fake_groups(monkeypatch, [("a", 0.25, [L("ok", "alpha", "fine")]),
                              ("b", 0.0, [L("warn", "beta", "hmm"), L("fail", "gamma", "bad")])])
    async with running(home, anim=True) as (app, pilot):
        assert isinstance(app.screen, BootScreen)
        await until(pilot, lambda: "gamma" in boot_text(app), timeout=3)
        text = boot_text(app)
        assert text.index("alpha") < text.index("beta") < text.index("gamma")
        assert "[ OK ]  alpha" in text and "[WARN]" in text and "[FAIL]" in text
        await until(pilot, lambda: isinstance(app.screen, MapScreen), timeout=6)
        assert "boot: 1 failed, 1 warning" in app.boot_note


async def test_with_boot_wait_false_any_key_skips_the_boot(home, monkeypatch, shut):
    monkeypatch.delenv("LM_NO_BOOT")
    set_ui(home, boot_wait=False)
    fake_groups(monkeypatch, [("slow", 3.0, [L("ok", "slow", "")])], shut)
    async with running(home, anim=True) as (app, pilot):
        assert isinstance(app.screen, BootScreen) and "any key skips this" in boot_text(app)
        await pilot.press("x")
        await until(pilot, lambda: isinstance(app.screen, MapScreen), timeout=2)


async def test_plain_mode_waits_for_all_and_boot_false_skips(home, monkeypatch):
    monkeypatch.delenv("LM_NO_BOOT")
    set_ui(home, boot_wait=False)
    fake_groups(monkeypatch, [("a", 0.1, [L("ok", "alpha", "fine")]), ("b", 0.1, [L("info", "beta", "n/a")])])
    async with running(home, anim=False) as (app, pilot):
        assert isinstance(app.screen, BootScreen)
        await until(pilot, lambda: "beta" in boot_text(app), timeout=3)
        assert "alpha" in boot_text(app) and "[ . ]" not in boot_text(app)
        await until(pilot, lambda: isinstance(app.screen, MapScreen), timeout=4)
    set_ui(home, boot=False)
    async with running(home, anim=False) as (app, pilot):
        assert isinstance(app.screen, MapScreen)


# ------------------------------------------------------------------ ui.boot_wait: the finished checklist stays until Enter
def test_boot_wait_reading(home):
    assert checks.boot_wait(home) is True                              # the default, and what the shipped config.md says
    for v, want in ((False, False), ("false", False), ("No", False), ("off", False), (0, False),
                    (True, True), ("yes", True), ("true", True), (1, True), ("garbage", True)):
        set_ui(home, boot_wait=v)
        assert checks.boot_wait(home) is want, v
    home.settings = {"ui": "oops"}
    assert checks.boot_wait(home) is True                              # unreadable settings: the default
    home.settings = {}
    assert checks.boot_wait(home) is True


async def test_the_finished_checklist_stays_until_enter(home, monkeypatch, short_hold):
    monkeypatch.delenv("LM_NO_BOOT")
    fake_groups(monkeypatch, [("a", 0.0, [L("ok", "alpha", "fine")]), ("b", 0.0, [L("warn", "beta", "hmm")])])
    async with running(home, anim=True) as (app, pilot):
        assert isinstance(app.screen, BootScreen)
        await until(pilot, lambda: "Enter: continue" in boot_text(app), timeout=4)
        text = boot_text(app)
        assert "alpha" in text and "beta" in text and "ready" in text and "starting" not in text and "skips" not in text
        await pilot.pause(0.4)                                         # four times the hold with a warning (0.1 s here, 1.8 s in life)
        assert isinstance(app.screen, BootScreen) and "Enter: continue" in boot_text(app)
        for key in ("x", "space", "escape", "n"):
            await pilot.press(key)                                     # no other key closes it
        await pilot.pause(0.3)
        assert isinstance(app.screen, BootScreen)
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, MapScreen), timeout=2)
        assert "boot: 0 failed, 1 warning" in app.boot_note            # what was seen still counts


async def test_while_it_checks_only_enter_skips(home, monkeypatch, shut):
    monkeypatch.delenv("LM_NO_BOOT")
    fake_groups(monkeypatch, [("slow", 5.0, [L("ok", "slow", "")])], shut)
    async with running(home, anim=True) as (app, pilot):
        assert isinstance(app.screen, BootScreen) and "Enter skips this" in boot_text(app)
        await pilot.press("x")
        await pilot.press("space")
        await pilot.pause(0.3)
        assert isinstance(app.screen, BootScreen)                      # a stray key does not skip the rest
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, MapScreen), timeout=2)


async def test_plain_mode_waits_for_enter_as_well(home, monkeypatch, short_hold):
    monkeypatch.delenv("LM_NO_BOOT")
    fake_groups(monkeypatch, [("a", 0.0, [L("ok", "alpha", "fine")])])
    async with running(home, anim=False) as (app, pilot):
        await until(pilot, lambda: "Enter: continue" in boot_text(app), timeout=4)
        await pilot.pause(0.3)                                         # plain mode used to leave after 0.28 s (here the hold is 0.05 s)
        assert isinstance(app.screen, BootScreen)
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, MapScreen), timeout=2)


async def test_boot_wait_false_and_lm_no_boot_keep_working(home, monkeypatch):
    set_ui(home, boot_wait=False)
    fake_groups(monkeypatch, [("a", 0.0, [L("ok", "alpha", "fine")])])
    monkeypatch.delenv("LM_NO_BOOT")
    async with running(home, anim=True) as (app, pilot):
        assert isinstance(app.screen, BootScreen) and "Enter: continue" not in boot_text(app)
        await until(pilot, lambda: isinstance(app.screen, MapScreen), timeout=6)       # closes by itself
    set_ui(home, boot_wait=True)
    monkeypatch.setenv("LM_NO_BOOT", "1")
    async with running(home, anim=True) as (app, pilot):
        assert isinstance(app.screen, MapScreen)                       # no checklist at all, as before


def test_brief_lists_only_problems(home):
    class P:
        def __init__(self, name, lines):
            self.name, self._ls = name, lines

        def lines(self):
            return self._ls
    s = BootScreen([P("a", [L("ok", "fine", "")]), P("b", [L("warn", "careful", "x")])], brief=True)
    s.t0, s.step, s.calm = time.monotonic(), 0.0, False
    s.advance(time.monotonic())
    assert [x.label for x in s.rows] == ["careful"] and len(s.all) == 2 and s.end


# ------------------------------------------------------------------ lazy imports and update --from
def test_importing_the_tui_does_not_load_the_sdk_or_the_chat():
    code = ("import sys, lightmind.tui.app; print([m for m in ('claude_agent_sdk', 'httpx', 'rich.markdown', "
            "'lightmind.loop', 'lightmind.tui.chat') if m in sys.modules])")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120).stdout.strip()
    assert out == "[]", out


def test_update_from_builds_a_wheel_of_the_committed_code(tmp_path, home):
    import shutil
    import zipfile
    if not shutil.which("uv") or not shutil.which("git"):
        pytest.skip("needs uv and git")
    src = tmp_path / "core"
    (src / "src" / "lmx").mkdir(parents=True)
    (src / "pyproject.toml").write_text(
        '[project]\nname = "lightmind"\nversion = "0.1.0"\n[build-system]\nrequires = ["hatchling"]\n'
        'build-backend = "hatchling.build"\n[tool.hatch.build.targets.wheel]\npackages = ["src/lmx"]\n', "utf-8")
    init = src / "src" / "lmx" / "__init__.py"
    init.write_text("X = 1\n", "utf-8")

    def git(*a):
        subprocess.run(["git", "-C", str(src), *a], check=True, capture_output=True)
    git("init", "-q")
    git("add", ".")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "x")
    init.write_text("X = 2\n", "utf-8")                                   # uncommitted: must not ship
    rt = home.path("system") / "runtime-test"
    rt.mkdir(parents=True, exist_ok=True)
    (rt / "lm-root.txt").write_text("x", "utf-8")
    from lightmind.init import install_from_folder
    wheel, stamp = install_from_folder(src, home.root)
    assert b"X = 1" in zipfile.ZipFile(wheel).read("lmx/__init__.py")
    assert (rt / "lm-root.txt").is_file()                                  # no runtime is touched ...
    assert (home.path("state") / "runtime-stamp.txt").read_text("ascii") == stamp != ""   # ... a new one is asked for
    assert (home.path("state") / "install-source.txt").read_text("utf-8").startswith("wheel ")
    assert list(wheel.parent.glob("lightmind-*.whl")) == [wheel]


def test_control_characters_from_git_and_names_never_reach_a_line(home, calls):
    esc = chr(27)
    vault = home.path("vault")
    vault.mkdir(parents=True, exist_ok=True)
    calls.answers.update({"rev-parse --show-toplevel": (0, str(vault)), "branch --show-current": (0, "ma" + esc + "[31min"),
                          "status --porcelain": (0, ""), "--abbrev-ref": (1, "none")})
    line = checks.check_vault(home)[0]
    assert esc not in line.detail and "[31min" in line.detail
    assert esc not in checks.Line("ok", "a" + esc + "]0;x", "b" + esc + "c" + chr(10) + "d").label
    assert chr(10) not in checks.Line("ok", "a", "b" + chr(10) + "d").detail
    assert esc not in checks.clean(checks._run.__doc__ or "") and checks.clean("x" + chr(7) + "y") == "xy"
    far = home.path("servers") / "far"
    far.mkdir(parents=True, exist_ok=True)
    (far / "info.md").write_text("---" + chr(10) + "host: h" + chr(10) + "ssh: far" + chr(10) + "---" + chr(10), "utf-8")
    calls.answers["far"] = (255, "ssh: " + esc + "[2Jdenied")
    assert esc not in checks.check_server(home, "far")[0].detail
