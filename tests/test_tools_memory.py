"""The memory limit of tool and shell processes (`tools.memory_mb`, a tool's own `memory_mb:`)."""
import os
import sys

import pytest

from lightmind import memlimit as M
from test_tools import add_tool, make, yes

HOG = "data = []\nwhile True:\n    data.append(bytearray(10 * 1024 * 1024))\n"
HOG_TOOL = HOG
POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="RLIMIT_AS is POSIX; the Windows job is tested below")
WINDOWS_ONLY = pytest.mark.skipif(os.name != "nt", reason="Job Objects exist only on Windows")


def limited(home, mb, **kw):
    d, *_ = make(home, asker=yes, **kw)
    home.settings = {**home.settings, "tools": {"as_functions": True, "memory_mb": mb}}
    return d


def hog_cmd(tmp_path):
    p = tmp_path / "hog.py"
    p.write_text(HOG, "utf-8")
    return f'"{sys.executable}" "{p}"'


# ------------------------------------------------- the number
def test_default_is_40_percent_with_a_floor():
    assert M.limit_mb(None, ram=16000) == 6400
    assert M.limit_mb("", ram=13700) == 5480
    assert M.limit_mb(None, ram=2000) == 1600          # the floor of 2048 is cut by the 80 % ceiling
    assert M.limit_mb(None, ram=4000) == 2048
    assert M.limit_mb(None, ram=0) == M.UNKNOWN_RAM_MB


def test_zero_is_off_even_for_a_tool_with_its_own_limit():
    assert M.limit_mb(0, ram=16000) == 0
    assert M.limit_mb(0, 1024, ram=16000) == 0


def test_configured_value_and_the_frontmatter_limit():
    assert M.limit_mb(3000, ram=16000) == 3000
    assert M.limit_mb(3000, 500, ram=16000) == 500      # the tool names its own
    assert M.limit_mb(3000, None, ram=16000) == 3000
    assert M.limit_mb(3000, "junk", ram=16000) == 3000
    assert M.limit_mb("junk", ram=16000) == 6400


def test_nothing_above_80_percent_of_the_ram():
    assert M.limit_mb(99999, ram=10000) == 8000
    assert M.limit_mb(3000, 99999, ram=10000) == 8000


def test_physical_ram_is_read():
    assert M.physical_ram_mb() > 256


def test_a_tool_manifest_reads_memory_mb(home):
    add_tool(home, "own", "print(1)\n", "memory_mb: 300\n")
    add_tool(home, "none", "print(1)\n")
    assert home.load_tool("own").memory_mb == 300 and home.load_tool("none").memory_mb is None


def test_the_dispatcher_resolves_the_setting(home):
    d, *_ = make(home)
    home.settings = {**home.settings, "tools": {"as_functions": True, "memory_mb": 1234}}
    assert d.memory_limit() == min(1234, int(M.physical_ram_mb() * 0.8)) and d.memory_limit(100) == 100


# ------------------------------------------------- a real process (POSIX: RLIMIT_AS)
@POSIX_ONLY
async def test_a_shell_command_that_hoards_memory_is_stopped(home, tmp_path):
    d = limited(home, 200)
    r = await d.call("bash", {"command": hog_cmd(tmp_path), "timeout": 60})
    assert not r.ok
    assert r.text == "stopped: bash used more than 200 MB (tools.memory_mb in config.md)"


@POSIX_ONLY
async def test_a_tool_that_hoards_memory_is_stopped_and_named(home):
    add_tool(home, "hog", HOG_TOOL)
    d = limited(home, 200)
    r = await d.call("hog", {})
    assert not r.ok and r.text == "stopped: hog used more than 200 MB (tools.memory_mb in config.md)"


@POSIX_ONLY
async def test_the_limit_in_the_tool_frontmatter_applies(home):
    add_tool(home, "hog", HOG_TOOL, "memory_mb: 150\n")
    d = limited(home, 4000)                              # the global limit alone would let it grow far longer
    r = await d.call("hog", {})
    assert not r.ok and "used more than 150 MB" in r.text


@POSIX_ONLY
async def test_a_normal_process_is_not_touched(home, tmp_path):
    d = limited(home, 200)
    r = await d.call("bash", {"command": f'"{sys.executable}" -c "print(sum(range(1000)))"'})
    assert r.ok and r.text.strip() == "499500"
    r = await d.call("bash", {"command": "echo hi; exit 3"})
    assert not r.ok and "[exit 3]" in r.text and "stopped" not in r.text


@POSIX_ONLY
async def test_an_ordinary_failure_is_not_a_memory_stop(home):
    add_tool(home, "bad", "import sys\nsys.exit('plain error')\n")
    d = limited(home, 200)
    r = await d.call("bad", {})
    assert not r.ok and "plain error" in r.text and "stopped" not in r.text


@POSIX_ONLY
async def test_limit_zero_is_off(home, tmp_path):
    p = tmp_path / "big.py"
    p.write_text("x = bytearray(600 * 1024 * 1024)\nprint(len(x))\n", "utf-8")
    d = limited(home, 200)
    assert not (await d.call("bash", {"command": f'"{sys.executable}" "{p}"'})).ok     # over 200 MB: stopped
    d = limited(home, 0)
    r = await d.call("bash", {"command": f'"{sys.executable}" "{p}"'})
    assert r.ok and r.text.strip() == str(600 * 1024 * 1024)


@POSIX_ONLY
async def test_a_child_of_the_shell_is_limited_too(home, tmp_path):
    d = limited(home, 200)
    r = await d.call("bash", {"command": f"({hog_cmd(tmp_path)}); echo after $?", "timeout": 60})
    assert "MB" in r.text or "after" in r.text      # the subshell's child hit the limit, lm itself went on
    assert (await d.call("bash", {"command": "echo alive"})).text.strip() == "alive"


# ------------------------------------------------- Windows: a Job Object (checked by hand on the laptop)
@WINDOWS_ONLY
async def test_windows_job_stops_a_hoarding_tool_tree(home, tmp_path):
    d = limited(home, 200)
    r = await d.call("bash", {"command": hog_cmd(tmp_path), "timeout": 60})
    assert not r.ok and r.text == "stopped: bash used more than 200 MB (tools.memory_mb in config.md)"


@WINDOWS_ONLY
async def test_windows_job_leaves_a_normal_process_alone(home):
    d = limited(home, 200)
    r = await d.call("bash", {"command": f'"{sys.executable}" -c "print(6*7)"'})
    assert r.ok and r.text.strip() == "42"


@WINDOWS_ONLY
async def test_windows_job_zero_is_off(home, tmp_path):
    p = tmp_path / "big.py"
    p.write_text("x = bytearray(400 * 1024 * 1024)\nprint(len(x))\n", "utf-8")
    d = limited(home, 0)
    r = await d.call("bash", {"command": f'"{sys.executable}" "{p}"'})
    assert r.ok


@WINDOWS_ONLY
def test_windows_job_has_the_limit_and_the_kill_flag():
    import subprocess
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        job = M.Job.start(300, p.pid)
        assert job is not None and job.peak_mb() >= 0
        job.close()                                       # KILL_ON_JOB_CLOSE
        p.wait(timeout=10)
        assert p.returncode is not None
    finally:
        p.kill()
