"""Where a chat opens (launch.py): the command for a new tab or window in each terminal, the fallbacks, what is refused.
Nothing in here starts a process: `plan` is a pure function and `spawn` gets a fake Popen."""
import subprocess
from pathlib import Path

import pytest

from lightmind import launch, settings as S
from lightmind.files import Home
from lightmind.launch import Plan

REAL_SPAWN = launch.spawn                              # conftest swaps `launch.spawn` out for every test: keep the real one
TREE = Path("/t/lm")
RID = "20261021-101010-chat-ab12"
LM_WIN, LM_POSIX = str(TREE / "system" / "lm.cmd"), str(TREE / "system" / "lm")
WT = {"WT_SESSION": "abc"}
TMUX = {"TMUX": "/tmp/tmux-1000/default,1234,0"}


def plan(wish, env, platform="win32", tree=TREE, exists=lambda p: True, ref=RID):
    return launch.plan(tree, ref, wish, env, platform, exists)


# ------------------------------------------------------------------ which terminal
def test_tmux_comes_first_then_windows_terminal_and_vscode_is_no_windows_terminal():
    assert launch.terminal({}) == "none"
    assert launch.terminal(TMUX) == "tmux"
    assert launch.terminal(WT) == "wt"
    assert launch.terminal({**TMUX, **WT}) == "tmux"                           # a tmux inside Windows Terminal is tmux
    assert launch.terminal({**WT, "TERM_PROGRAM": "vscode"}) == "none"         # VS Code inherits WT_SESSION without being one
    assert launch.terminal({**WT, "TERM_PROGRAM": "VSCode"}) == "none"
    assert launch.terminal({**WT, "TERM_PROGRAM": "iTerm.app"}) == "wt"
    assert launch.terminal({"WT_SESSION": ""}) == "none"


# ------------------------------------------------------------------ the commands, as argument lists
def test_windows_terminal_opens_a_tab_in_the_current_window_or_a_new_window():
    tab = plan("tab", WT)
    assert tab == Plan("wt", ("wt.exe", "-w", "0", "nt", "-d", str(TREE), LM_WIN, "open", RID), "tab")
    win = plan("window", WT)
    assert win == Plan("wt", ("wt.exe", "-w", "new", "nt", "-d", str(TREE), LM_WIN, "open", RID), "window")
    assert tab.separate and not tab.note and not win.note


def test_tmux_opens_a_window_for_tab_and_window_and_the_launcher_follows_the_platform():
    for wish in ("tab", "window"):
        p = plan(wish, TMUX, platform="linux")
        assert p.how == "tmux" and p.note == ""
        assert p.argv == ("tmux", "new-window", "-c", str(TREE), LM_POSIX, "open", RID)
    assert plan("tab", TMUX, platform="win32").argv[4] == LM_WIN


def test_without_a_terminal_that_can_do_it_a_tab_stays_here_and_a_window_opens_a_console():
    tab, win = plan("tab", {}), plan("window", {"TERM_PROGRAM": "vscode", **WT})
    assert tab.how == "replace" and tab.note == ""                             # PowerShell or cmd: the chat stays in this window
    assert win.how == "cmd" and win.argv == ("cmd", "/c", "start", "", LM_WIN, "open", RID) and win.what == "window"


def test_linux_and_macos_without_tmux_keep_the_chat_here_and_say_so():
    for platform in ("linux", "darwin"):
        for wish in ("tab", "window"):
            p = plan(wish, {}, platform=platform)
            assert p.how == "replace" and p.argv == () and not p.separate
            assert ("this window" in p.note) == (wish == "window")              # a tab staying here is no news


def test_replace_is_replace_everywhere_and_an_unknown_wish_is_the_default():
    for env in ({}, WT, TMUX):
        assert plan("replace", env) == Plan("replace")
    assert plan("tabb", WT) == plan("tab", WT) and plan("", TMUX) == plan("tab", TMUX)


# ------------------------------------------------------------------ nothing typed reaches a command line unchecked
@pytest.mark.parametrize("ref", ["", "a b", "x;rm", "a&b", "../x", "-rf", ".hidden", "a\nb", "$(id)", "a`b`", "a|b", "ä", "x" * 0 + " "])
def test_an_id_or_name_with_other_characters_is_refused_before_a_command_exists(ref):
    for env in ({}, WT, TMUX):
        with pytest.raises(ValueError):
            plan("tab", env, ref=ref)


@pytest.mark.parametrize("ref", ["20261021-101010-chat-ab12", "lead", "my.agent_2", "a-b", "0"])
def test_ids_and_names_of_letters_digits_dot_underscore_dash_pass(ref):
    assert plan("tab", WT, ref=ref).argv[-1] == ref


def test_a_path_with_spaces_stays_one_argument_and_a_path_the_shell_would_read_is_not_used():
    spaced = Path("/Users/Some One/lightmind")
    p = plan("tab", WT, tree=spaced)
    assert str(spaced) in p.argv and str(spaced / "system" / "lm.cmd") in p.argv and " ".join(p.argv).count("open") == 1
    semi = Path("/t/a;b")
    assert plan("tab", WT, tree=semi).how == "replace"                         # `;` separates commands for wt.exe, and cmd.exe splits at it too
    assert plan("tab", WT, tree=semi, platform="linux").how == "replace"
    assert plan("tab", TMUX, tree=semi, platform="linux").how == "tmux"
    for bad in ("a&b", "a^b", "a%b", "a(b)", "a|b", "a!b"):
        p = plan("window", {}, tree=Path("/t") / bad)
        assert p.how == "replace" and "this window" in p.note                   # cmd.exe would read it as syntax
    assert plan("window", TMUX, tree=Path("/t/a&b"), platform="linux").how == "tmux"   # tmux takes every argument as it is


def test_a_tree_without_its_launcher_keeps_the_chat_here():
    p = plan("tab", WT, exists=lambda path: False)
    assert p.how == "replace" and "is missing" in p.note and LM_WIN in p.note
    assert plan("replace", WT, exists=lambda path: False).note == ""           # nothing was asked of it


def test_no_plan_holds_a_shell_string():
    for env, platform in ((WT, "win32"), (TMUX, "linux"), ({}, "win32")):
        for wish in ("tab", "window"):
            p = plan(wish, env, platform=platform)
            if p.how == "replace":
                continue                                                        # nothing is started
            assert isinstance(p.argv, tuple) and all(isinstance(a, str) for a in p.argv)
            assert RID in p.argv and not any(RID in a and a != RID for a in p.argv)      # the id is an argument of its own


# ------------------------------------------------------------------ sessions.open
def home_with(tmp_path, value):
    from lightmind.init import init_tree
    init_tree(tmp_path / "lm")
    cfg = tmp_path / "lm" / "config.md"
    if value is not None:
        cfg.write_bytes(S.edit_text(cfg.read_bytes().decode("utf-8"), {("sessions", "open"): value}).encode("utf-8"))
    return Home(tmp_path / "lm")


def test_the_wish_comes_from_sessions_open_in_config_md(tmp_path):
    assert launch.want(home_with(tmp_path, None)) == "tab"                     # the shipped value
    for v in ("tab", "window", "replace"):
        h = home_with(tmp_path / v, v)
        assert launch.want(h) == v
    assert launch.want(home_with(tmp_path / "up", "Window")) == "window"
    assert launch.want(home_with(tmp_path / "bad", "elsewhere")) == "tab"      # unknown: the default


def test_the_wish_is_the_default_when_the_settings_have_no_such_section(tmp_path):
    h = home_with(tmp_path, None)
    h.settings = {"defaults": {}}
    assert launch.want(h) == "tab"
    h.settings = {"sessions": "oops"}
    assert launch.want(h) == "tab"


def test_plan_open_reads_the_wish_and_the_launcher_of_the_tree(tmp_path):
    h = home_with(tmp_path, "window")
    p = launch.plan_open(h, RID, env=TMUX, platform="linux")
    assert p.how == "tmux" and p.argv[:4] == ("tmux", "new-window", "-c", str(h.root)) and p.argv[-3:] == (str(h.root / "system" / "lm"), "open", RID)
    h2 = home_with(tmp_path / "r", "replace")
    assert launch.plan_open(h2, RID, env=TMUX, platform="linux") == Plan("replace")
    p3 = launch.plan_open(h, RID, env=WT, platform="win32")
    assert p3.argv[:3] == ("wt.exe", "-w", "new") and p3.argv[-3] == str(h.root / "system" / "lm.cmd")


# ------------------------------------------------------------------ spawn (a fake Popen)
class FakePopen:
    made: list = []

    def __init__(self, code=0, error=None, wait_error=None):
        self.code, self.error, self.wait_error, self.waited = code, error, wait_error, None

    def __call__(self, argv, **kw):
        FakePopen.made.append((argv, kw))
        if self.error:
            raise self.error
        return self

    def wait(self, timeout=None):
        self.waited = timeout
        if self.wait_error:
            raise self.wait_error
        return self.code


def test_spawn_starts_the_argument_list_without_a_shell_or_a_pipe():
    FakePopen.made = []
    p = plan("tab", WT)
    fake = FakePopen(0)
    assert REAL_SPAWN(p, TREE, popen=fake) == (True, "")
    (argv, kw), = FakePopen.made
    assert argv == list(p.argv) and kw["cwd"] == str(TREE) and "shell" not in kw
    assert kw["stdin"] == kw["stdout"] == kw["stderr"] == subprocess.DEVNULL        # a pipe would be kept open by the new window
    assert fake.waited and fake.waited <= 30                                       # it waits for the command to be accepted, not for the chat


@pytest.mark.parametrize("fake, why", [
    (FakePopen(3), "failed (exit 3)"),
    (FakePopen(error=FileNotFoundError()), "was not found"),
    (FakePopen(error=PermissionError("denied")), "denied"),
    (FakePopen(wait_error=subprocess.TimeoutExpired("wt.exe", 20)), "did not answer"),
])
def test_spawn_says_why_it_could_not_and_never_raises(fake, why):
    ok, msg = REAL_SPAWN(plan("tab", WT), TREE, popen=fake)
    assert ok is False and why in msg and msg.startswith("wt.exe")


def test_spawn_of_nothing_is_a_no():
    assert REAL_SPAWN(Plan("replace"), TREE, popen=FakePopen(0)) == (False, "nothing to start")
