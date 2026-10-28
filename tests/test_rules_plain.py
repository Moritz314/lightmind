"""Permission questions in plain words: one sentence, the reason, the program; the raw command stays out of the text."""
import asyncio
import shutil
from pathlib import Path

import pytest

from lightmind import rules as R
from lightmind.files import Home
from lightmind.model import Agent, Check, Decision, Rule, RuleContext, SessionState
from lightmind.tools import _BUILTINS

STRICT = Path(__file__).parent / "fixtures" / "permissions_strict.md"


@pytest.fixture
def home(tree) -> Home:
    shutil.copy(STRICT, tree / "permissions.md")
    return Home(tree)


def ctx(home):
    return RuleContext(home=str(home.root), run_dir=str(home.root / "runs" / "r1"), project_dir=str(home.root / "projects" / "k"))


def test_bash_schema_requires_a_description():
    sch = _BUILTINS["bash"]["input_schema"]
    assert "description" in sch["properties"] and "description" in sch["required"]


@pytest.mark.parametrize("name,args,want", [
    ("read", {"path": "notes/today.md"}, "View file notes/today.md"),
    ("write", {"path": "a.md", "content": "x"}, "Write file a.md"),
    ("edit", {"path": "src/a.py", "old": "x", "new": "y"}, "Make changes in src/a.py"),
    ("web", {"url": "https://Example.org/a/b?token=1"}, "Fetch web page example.org"),
    ("spawn", {"task": "Check the logs\nthen report"}, "Start sub-agent: Check the logs"),
    ("spawn", {"agent": "webdev", "task": "Fix it"}, "Start sub-agent webdev: Fix it"),
    ("ssh", {"server": "box", "command": "ls", "description": "List files"}, "List files on box"),
    ("web", {"url": "http://[bad"}, "Fetch a web page"),
    ("skill_load", {"name": "jev"}, "Read skill jev"),
    ("bash", {"command": "ls -la src", "description": "List the source files"}, "List the source files"),
    ("bash", {"command": "cd /x && git push origin main"}, "Run git push"),
    ("bash", {"command": "FOO=1 sudo rm -rf build"}, "Run sudo -> rm build"),
    ("bash", {"command": "ls", "description": "  \n "}, "Run ls"),
    ("pdf", {}, "Use tool pdf"),
])
def test_describe_call(name, args, want):
    assert R.describe_call(name, args, "/tree") == want


def test_describe_never_carries_the_whole_command_or_control_characters():
    cmd = "cat /etc/passwd | " + "x" * 3000
    d = R.describe_call("bash", {"command": cmd})
    assert len(d) < 60 and "x" * 41 not in d
    d = R.describe_call("bash", {"command": "ls", "description": "Show\x1b[2J files\n" + "y" * 500})
    assert "\x1b" not in d and "\n" not in d and len(d) <= R.DESC_MAX
    assert R.describe_call("read", {"path": "/a/very/long/" + "d/" * 40 + "file.txt"}, "/tree").endswith("file.txt")
    assert len(R.describe_call("read", {"path": "/".join(["dir"] * 60)})) < 70


def test_a_path_inside_the_tree_is_shown_relative():
    assert R.describe_call("read", {"path": "/tree/notes/a.md"}, "/tree") == "View file notes/a.md"


def test_the_question_has_a_sentence_the_program_and_the_reason_but_not_the_command(home):
    layers = R.build_layers(home, Agent("webdev", permissions={"ask": ["bash(git push*)"]}), "auto")
    seen = []

    async def asker(q, c):
        seen.append((q, c))
        return "yes"
    cmd = "git push origin main --force-with-lease"
    d = asyncio.run(R.enforce(layers, [Check("bash", cmd)], ctx(home), asker, SessionState(), "Upload the changes"))
    assert d.effect == "allow"
    q, c = seen[0]
    lines = q.split("\n")
    assert lines[0] == "Upload the changes" and lines[1] == "Program: git" and "bash(git push*)" in lines[2]
    assert "origin" not in q and c.arg == cmd                    # the raw command only travels in the check


def test_without_a_description_a_short_summary_is_made_from_the_check(home):
    layers = R.build_layers(home, Agent("webdev", permissions={"ask": ["bash(git push*)", "read(**)"]}), "auto")
    seen = []

    async def asker(q, c):
        seen.append(q)
        return "no"
    asyncio.run(R.enforce(layers, [Check("bash", "git push origin main")], ctx(home), asker, SessionState()))
    asyncio.run(R.enforce(layers, [Check("read", "notes/a.md")], ctx(home), asker, SessionState()))
    assert seen[0].startswith("Run git push\nProgram: git") and seen[1].startswith("View file notes/a.md")
    assert all("Allow " not in q for q in seen)


def test_an_unreadable_line_is_explained_without_printing_it(home):
    layers = R.build_layers(home, Agent("a", permissions={"allow": ["bash(*)"]}), "auto")
    seen = []

    async def asker(q, c):
        seen.append(q)
        return "no"
    asyncio.run(R.enforce(layers, R.checks_for_call("bash", {"command": "echo 'open"}), ctx(home), asker, SessionState()))
    assert any("cannot be read safely" in q for q in seen) and all("open'" not in q for q in seen)
    assert all("Run a command line that cannot be checked" in q.split("\n")[0] for q in seen if "cannot be read" in q)   # no model sentence here


def test_the_model_cannot_hide_the_program_behind_its_description():
    q = R.ask_text("View a harmless file", Decision("ask", None, "mode:auto", "x"), Check("bash", "rm -rf build"))
    assert "Program: rm" in q.split("\n")


@pytest.mark.parametrize("cmd,prog", [
    ("sudo -u root rm -rf /", "sudo -> rm"),
    ("env -i FOO=1 rm x", "env -> rm"),
    ("timeout 5 rm -rf x", "timeout -> rm"),
    ("xargs -0 rm", "xargs -> rm"),
    ("bash -c 'rm -rf ~'", "bash -c (a script)"),
    ("./git status", "./git"),
    ("/tmp/x/git push", "/tmp/x/git"),
    ("/usr/bin/git push", "git"),
    ("cd a && export B=1 && make all", "make"),
])
def test_the_program_line_names_what_really_runs(cmd, prog):
    assert R.bash_program(cmd)[0] == prog


def test_bidi_and_zero_width_characters_are_removed_from_the_sentence():
    d = R.describe_call("bash", {"command": "ls", "description": "Show \u202egnp.exe\u2066 fi\u200ble\ufeffs"})
    assert not any(c in d for c in "\u202e\u2066\u200b\ufeff")


def test_other_checks_of_a_call_say_their_own_sentence(home):
    layers = R.build_layers(home, Agent("a", permissions={"allow": ["bash(*)"], "ask": ["server(box)", "ssh(*)"]}), "auto")
    seen = []

    async def asker(q, c):
        seen.append((c.tool, q.split("\n")[0]))
        return "yes"
    chk = R.checks_for_call("ssh", {"server": "box", "command": "ls"})
    asyncio.run(R.enforce(layers, chk, ctx(home), asker, SessionState(), "List files on box"))
    assert ("ssh", "List files on box") in seen and ("server", "Connect to server box") in seen


def test_the_model_sentence_belongs_to_the_first_check_only_and_a_line_without_program_shows_itself():
    import asyncio
    from lightmind.model import Layer, RuleContext, SessionState
    layers = [Layer("defaults", [], "ask")]
    checks = R.checks_for_call("bash", {"command": "git status && export LD_PRELOAD=/tmp/x.so"})
    assert len(checks) >= 2
    asked = []

    async def asker(q, c):
        asked.append(q)
        return "yes"
    asyncio.run(R.enforce(layers, checks, RuleContext(home="/h"), asker, SessionState(), describe="Show the changed files"))
    assert asked[0].startswith("Show the changed files")
    assert all(not q.startswith("Show the changed files") for q in asked[1:])
    assert any("Command: export LD_PRELOAD=/tmp/x.so" in q for q in asked[1:])
