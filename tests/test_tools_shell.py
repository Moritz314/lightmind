"""The shell the dispatcher runs command lines in, and the match values a tool is handed
(phase 1, last hardening pass)."""
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from lightmind import rules as R
from lightmind import tools as T
from lightmind.files import Home
from test_tools import add_tool, make, yes
from test_tools_hardening import link_dir


@pytest.fixture
def settings(monkeypatch):
    """settings(shell=..., tools={...}): what `home.settings` answers for the next calls."""
    def _set(**kw):
        monkeypatch.setattr(Home, "settings", property(lambda self: kw, lambda self, v: None), raising=False)
    return _set


# ------------------------------------------------- settings
def test_setting_reads_flat_and_nested_keys(home, settings):
    settings(**{"tools.as_functions": True})
    assert T._setting(home, "tools.as_functions") is True
    settings(tools={"as_functions": True}, shell="zsh")
    assert T._setting(home, "tools.as_functions") is True and T._setting(home, "shell") == "zsh"
    assert T._setting(home, "tools.nope", 7) == 7 and T._setting(home, "nope.deeper", 1) == 1
    assert T._setting(home, "shell.deeper", "d") == "d"                              # a leaf is not a map


def test_setting_defaults_when_there_are_no_settings():
    bare = SimpleNamespace()
    assert T._setting(bare, "shell", "bash") == "bash" and T._setting(bare, "tools.as_functions") is None
    assert T._setting(SimpleNamespace(settings=None), "shell", "bash") == "bash"


# ------------------------------------------------- 1: an explicit shell
def test_resolve_shell_finds_a_real_bash():
    sh = T.resolve_shell("bash")
    assert Path(sh).is_file() and "system32" not in sh.lower()


def test_resolve_shell_missing_is_a_clear_error(tmp_path):
    with pytest.raises(RuntimeError, match="not found"):
        T.resolve_shell("no-such-shell-xyz")
    with pytest.raises(RuntimeError, match="no-such-shell"):
        T.resolve_shell(str(tmp_path / "no-such-shell"))


def test_resolve_shell_takes_an_absolute_path_as_given():
    assert T.resolve_shell(sys.executable) == sys.executable


@pytest.mark.skipif(os.name != "nt", reason="Windows picks Git Bash, never WSL's launcher")
def test_resolve_shell_prefers_git_bash_and_never_the_wsl_launcher(tmp_path, monkeypatch):
    sys32 = tmp_path / "Windows" / "System32"
    sys32.mkdir(parents=True)
    (sys32 / "bash.exe").write_text("x")
    monkeypatch.setenv("PATH", str(sys32))
    for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        monkeypatch.setenv(var, str(tmp_path / "none"))
    with pytest.raises(RuntimeError, match="not found"):                 # only the WSL launcher exists
        T.resolve_shell("bash")
    git_bin = tmp_path / "pf" / "Git" / "bin"
    git_bin.mkdir(parents=True)
    (git_bin / "bash.exe").write_text("x")
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "pf"))
    assert T.resolve_shell("bash") == str(git_bin / "bash.exe")
    assert T.resolve_shell("bash.exe") == str(git_bin / "bash.exe")


@pytest.mark.skipif(os.name != "nt", reason="the Git install lookup is a Windows thing")
def test_resolve_shell_finds_the_git_install_of_the_git_on_path(tmp_path, monkeypatch):
    git_dir, other = tmp_path / "per-user" / "Git", tmp_path / "other"
    (git_dir / "cmd").mkdir(parents=True)
    (git_dir / "bin").mkdir()
    (git_dir / "cmd" / "git.exe").write_text("x")
    (git_dir / "bin" / "bash.exe").write_text("x")
    other.mkdir()
    (other / "bash.exe").write_text("x")
    for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
        monkeypatch.setenv(var, str(tmp_path / "none"))
    monkeypatch.setenv("PATH", os.pathsep.join([str(other), str(git_dir / "cmd")]))
    assert T.resolve_shell("bash") == str(git_dir / "bin" / "bash.exe")      # before whatever PATH lists first
    pf_bin = tmp_path / "pf" / "Git" / "bin"
    pf_bin.mkdir(parents=True)
    (pf_bin / "bash.exe").write_text("x")
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "pf"))
    assert T.resolve_shell("bash") == str(pf_bin / "bash.exe")                 # Program Files beats both


SHELL = "lm-test-shell"                                    # a name no real machine has
EXE = SHELL + (".exe" if os.name == "nt" else "")


def fake_shell(folder: Path) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / EXE).write_text("x")
    return folder / EXE


def test_resolve_shell_never_takes_one_from_the_current_directory_or_the_tree_root(tmp_path, monkeypatch):
    cwd, root, good = tmp_path / "cwd", tmp_path / "root", tmp_path / "good"
    fake_shell(cwd), fake_shell(root), fake_shell(cwd / "rel")
    ok = fake_shell(good)
    monkeypatch.chdir(cwd)
    entries = ["", ".", "rel", str(cwd), str(root)]            # an empty or relative entry means the current directory
    monkeypatch.setenv("PATH", os.pathsep.join([*entries, str(good)]))
    assert T.resolve_shell(SHELL, root) == str(ok)             # the shell from the one honest folder
    monkeypatch.setenv("PATH", os.pathsep.join(entries))
    with pytest.raises(RuntimeError, match="not found"):
        T.resolve_shell(SHELL, root)
    assert T.resolve_shell(SHELL) == str(root / EXE)           # `root` is what rules the tree root out


def test_resolve_shell_skips_every_path_entry_inside_the_tree(tmp_path, monkeypatch):
    """A `bash` an agent drops into any folder of the tree that is on PATH (system/bin, a tool's folder, a
    deeper one) must not become the shell."""
    root, good = tmp_path / "root", tmp_path / "good"
    inside = [fake_shell(root / "system" / "bin"), fake_shell(root / "tools" / "x"), fake_shell(root / "a" / "b" / "c")]
    ok = fake_shell(good)
    monkeypatch.chdir(tmp_path)
    entries = [str(f.parent) for f in inside]
    monkeypatch.setenv("PATH", os.pathsep.join([*entries, str(good)]))
    assert T.resolve_shell(SHELL, root) == str(ok)
    monkeypatch.setenv("PATH", os.pathsep.join(entries))
    with pytest.raises(RuntimeError, match="not found"):
        T.resolve_shell(SHELL, root)
    assert T.resolve_shell(SHELL) == str(inside[0])                  # without a root nothing is known to be the tree
    sibling = fake_shell(tmp_path / "root2")                          # same prefix, another folder: not inside
    monkeypatch.setenv("PATH", str(sibling.parent))
    assert T.resolve_shell(SHELL, root) == str(sibling)


def test_resolve_shell_resolves_links_before_it_judges_a_path_entry(tmp_path, monkeypatch):
    root, outside = tmp_path / "root", tmp_path / "outside"
    fake_shell(root / "bin")
    outside.mkdir()
    link = outside / "innocent"
    if not link_dir(link, root / "bin"):
        pytest.skip("cannot create a link here")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PATH", str(link))                              # looks like it lies outside, leads into the tree
    with pytest.raises(RuntimeError, match="not found"):
        T.resolve_shell(SHELL, root)


def test_an_absolute_shell_is_taken_as_given_even_inside_the_tree(tmp_path, monkeypatch):
    mine = fake_shell(tmp_path / "root" / "bin")               # the owner wrote this path into lightmind.md
    monkeypatch.chdir(mine.parent)
    assert T.resolve_shell(str(mine), tmp_path / "root") == str(mine)


async def test_bash_tool_runs_posix_syntax(home):
    d, *_ = make(home)
    r = await d.call("bash", {"command": "echo $((6*7)); printf '%s\\n' a b | tr a-z A-Z; echo \"q;r\" 'it'"})
    assert r.ok and r.text.split() == ["42", "A", "B", "q;r", "it"]       # cmd.exe / PowerShell would not do this


async def test_exit_code_and_stderr_come_through_the_shell(home):
    d, *_ = make(home)
    r = await d.call("bash", {"command": "echo out; echo err >&2; exit 3"})
    assert not r.ok and "out" in r.text and "err" in r.text and "[exit 3]" in r.text


async def test_the_setting_selects_the_shell(home, settings):
    d, *_ = make(home)
    settings(shell=T.resolve_shell("bash"))
    assert (await d.call("bash", {"command": "echo hi"})).text.strip() == "hi"
    settings(shell=sys.executable)                       # any program that takes `-c <text>` can be the shell
    r = await d.call("bash", {"command": "print(6*7)"})
    assert r.ok and r.text.strip() == "42"


async def test_missing_configured_shell_fails_clearly_without_fallback(home, settings, tmp_path):
    settings(shell="no-such-shell-xyz")
    marker = tmp_path / "ran.txt"
    d, *_ = make(home)
    r = await d.call("bash", {"command": f'echo x > "{marker}"'})
    assert not r.ok and "no-such-shell-xyz" in r.text and "not found" in r.text
    assert not marker.exists()                           # neither cmd.exe nor any other shell ran it


# ------------------------------------------------- 4: the tool gets what was checked
MAIL_META = "match: to\nparams:\n  to: {type: array}\n"
RAW = 'a@x.de; b@evil.de, "c@y.de" <d@z.de>'


def mail_tool(home, tmp_path):
    marker = tmp_path / "stdin.json"
    add_tool(home, "mail", f"import sys\nopen(r'{marker}','w').write(sys.stdin.read())\nprint('ok')\n", MAIL_META)
    return marker


async def test_tool_gets_the_checked_match_values_when_run_as_functions(home, settings, tmp_path):
    marker = mail_tool(home, tmp_path)
    settings(tools={"as_functions": True})
    d, *_ = make(home, asker=yes)
    assert (await d.call("mail", {"to": RAW})).ok
    sent = json.loads(marker.read_text(encoding="utf-8"))["to"]
    assert sent == ["a@x.de", "b@evil.de", "c@y.de", "d@z.de"]
    checked = [c.arg.split(":", 1)[1] for c in R.checks_for_call("mail", {"to": RAW}, tool_meta=home.load_tool("mail"))
               if c.tool == "tool"]
    assert sent == checked                                # one list: what the rules judged is what the tool gets


async def test_without_as_functions_the_tool_is_no_function_at_all(home, settings, tmp_path):
    marker = mail_tool(home, tmp_path)
    settings(tools={"as_functions": False})
    d, *_ = make(home, asker=yes)
    assert "mail" not in {s["name"] for s in d.schemas()}
    r = await d.call("mail", {"to": RAW})
    assert not r.ok and "unknown tool" in r.text and not marker.exists()      # the AI runs its script with bash


async def test_flat_settings_key_and_unusable_values(home, settings, tmp_path):
    marker = mail_tool(home, tmp_path)
    settings(**{"tools.as_functions": True})
    d, *_ = make(home, asker=yes)
    for given, expected in (("  ", []), (None, []), (["a@x.de;b@y.de"], ["a@x.de", "b@y.de"]), (5, ["5"])):
        assert (await d.call("mail", {"to": given})).ok
        assert json.loads(marker.read_text(encoding="utf-8"))["to"] == expected, given
    assert (await d.call("mail", {})).ok                  # no value given: nothing invented
    assert "to" not in json.loads(marker.read_text(encoding="utf-8"))
