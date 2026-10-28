"""What the dispatcher starts: which tools are functions (`tools.as_functions`), the environment of
every tool process, and that a stopped call stops the whole process tree."""
import asyncio
import os
import sys
from pathlib import Path

import pytest

from lightmind import tools as T
from test_tools import ECHO, add_tool, make, yes

# a grandchild of the shell that writes a beat until somebody kills it (or, should the code under test
# fail to, for 10 seconds: a leftover never runs for good)
BEAT = "import time\nfor _ in range(200):\n    open(r'{beat}', 'a').write('x')\n    time.sleep(0.05)\n"


def size(p: Path) -> int:
    return p.stat().st_size if p.exists() else 0


async def beating(p: Path) -> bool:
    """Does the beat file still grow?"""
    before = size(p)
    await asyncio.sleep(0.6)
    return size(p) > before


async def start_beat(d, tmp_path, timeout=120):
    beat = tmp_path / "beat.txt"
    script = tmp_path / "beat.py"
    script.write_text(BEAT.format(beat=beat), "utf-8")
    task = asyncio.create_task(d.call("bash", {"command": f'"{sys.executable}" "{script}"', "timeout": timeout}))
    for _ in range(100):                                       # until the grandchild runs
        await asyncio.sleep(0.1)
        if size(beat) > 2:
            break
    assert await beating(beat), "the helper process never started"
    return beat, task


# ------------------------------------------------- tools.as_functions
def test_tools_are_not_functions_unless_lightmind_md_says_so(home):
    d, *_ = make(home, functions=False)
    assert {s["name"] for s in d.schemas()} == set(T._BUILTINS) and d.ext == {}
    d, *_ = make(home)
    assert "pdf" in {s["name"] for s in d.schemas()}


async def test_a_tool_is_not_callable_when_tools_are_not_functions(home):
    d, *_ = make(home, functions=False, asker=yes)
    r = await d.call("pdf", {"command": "split", "file": "a.pdf", "out": "o.pdf"})
    assert not r.ok and "unknown tool" in r.text


def test_a_tool_without_a_run_command_is_never_a_function(home):
    manifest = home.path("tools") / "manifest-only"
    manifest.mkdir()
    (manifest / "tool.md").write_text("---\nname: manifest-only\ndescription: no run line\n---\nbody\n", "utf-8")
    readme = home.path("tools") / "readme-only"
    readme.mkdir()
    (readme / "README.md").write_text("# readme-only\n\nA script the AI runs with bash.\n", "utf-8")
    add_tool(home, "real", ECHO)
    names = {s["name"] for s in make(home)[0].schemas()}
    assert "real" in names and "pdf" in names
    assert not names & {"manifest-only", "readme-only", "vault"}                 # `vault` ships with a README only


def test_as_functions_is_read_from_the_shipped_setting(tree):
    from lightmind.files import Home
    h = Home(tree)
    assert h.settings["tools"] == {"as_functions": False}
    assert make(h, functions=False)[0].ext == {}
    text = (tree / "lightmind.md").read_text("utf-8")
    (tree / "lightmind.md").write_text(text.replace("as_functions: false", "as_functions: true"), "utf-8")
    assert "pdf" in make(Home(tree), functions=False)[0].ext                       # no test double, the real file


# ------------------------------------------------- environment
def test_tool_processes_start_from_the_runtime_env(home, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-leak")
    monkeypatch.setenv("UV_CACHE_DIR", "/somewhere/else")
    d, *_ = make(home)
    env = d._env()
    assert env["UV_CACHE_DIR"] == str(home.path("cache"))                           # the tree's, not the parent's
    assert env["UV_PYTHON_INSTALL_DIR"] == str(home.path("python"))
    assert env["PLAYWRIGHT_BROWSERS_PATH"] == str(home.path("browsers"))
    entries = env["PATH"].split(os.pathsep)
    assert entries[-1] == str(home.path("bin")) and entries.count(str(home.path("bin"))) == 1    # last: it never shadows git, bash or python
    assert env["LM_HOME"] == str(home.root) and env["LM_RUN"].endswith("r1")
    assert "ANTHROPIC_API_KEY" not in env                                           # the filter still applies


# ------------------------------------------------- stopping a call
async def test_cancelling_a_call_kills_the_whole_process_tree(home, tmp_path):
    d, *_ = make(home)
    beat, task = await start_beat(d, tmp_path)
    task.cancel()
    done, _ = await asyncio.wait({task}, timeout=5)          # a child that keeps the pipes open keeps the call waiting
    assert done and task.cancelled()
    assert not await beating(beat)                           # the shell AND its child are gone


async def test_a_timeout_kills_the_whole_process_tree(home, tmp_path):
    d, *_ = make(home)
    beat, task = await start_beat(d, tmp_path, timeout=2)
    done, _ = await asyncio.wait({task}, timeout=6)
    assert done
    r = task.result()
    assert not r.ok and "timeout after 2s" in r.text
    assert not await beating(beat)
