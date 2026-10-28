"""Security hardening of the dispatcher (phase 1): money ledger input, path-kind arguments,
undeclared arguments, links and Windows path aliases, skill_load names."""
import json
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lightmind import rules as R
from lightmind import tools as T
from test_tools import FakeApi, add_tool, make, yes

STRICT = Path(__file__).parent / "fixtures" / "permissions_strict.md"
WIN = pytest.mark.skipif(sys.platform != "win32", reason="Windows path aliases")


def link_dir(link: Path, target: Path) -> bool:
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if sys.platform == "win32":                       # a junction needs no privilege
        r = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], capture_output=True)
        return r.returncode == 0
    return False


# ------------------------------------------------- 2: money output can never open the limit
@pytest.mark.parametrize("text,expected", [
    ('{"spent_usd": NaN}', 1.5), ('{"spent_usd": Infinity}', 1.5), ('{"spent_usd": -Infinity}', 1.5),
    ('{"spent_usd": 1e999}', 1.5), ('{"spent_usd": "nan"}', 1.5), ('{"spent_usd": "inf"}', 1.5),
    ('{"spent_usd": null}', 1.5), ('{"spent_usd": true}', 1.5), ('{"spent_usd": [1]}', 1.5),
    ('{"spent_usd": {"a": 1}}', 1.5), ('{}', 1.5), ('[1, 2]', 1.5), ('"x"', 1.5), ('not json', 1.5),
    ('', 1.5), ('{"spent_usd": 99}', 1.5), ('{"spent_usd": -4}', 0.0), ('{"spent_usd": 0.25}', 0.25),
    ('{"spent_usd": "0.5"}', 0.5), ('{"spent_usd": 0}', 0.0), ('{"spent_usd": 1.5}', 1.5),
])
def test_spent_is_finite_and_clamped(text, expected):
    got = T._spent(text, 1.5)
    assert math.isfinite(got) and got == pytest.approx(expected)


async def test_nan_output_still_counts_against_the_limit(home, tree):
    shutil.copy(STRICT, tree / "permissions.md")                       # money_limit: 20 USD/day
    add_tool(home, "pay", "print('{\"spent_usd\": NaN}')\n", "category: money\nmax_usd_per_call: 9\n")
    api = FakeApi()
    api.Ledger = R.Ledger
    d, *_ = make(home, api, asker=yes)
    assert (await d.call("pay", {})).ok
    assert (await d.call("pay", {})).ok
    led = R.Ledger(home)
    assert led.booked() == pytest.approx(18)                           # NaN did not zero or poison the books
    assert "nan" not in led.file.read_text(encoding="utf-8").lower()
    r = await d.call("pay", {})
    assert not r.ok and "money limit" in r.text                        # 27 > 20: the limit holds


@pytest.mark.parametrize("amount", [".nan", ".inf", "-1"])
async def test_non_finite_reservation_is_refused_and_the_tool_does_not_run(home, tree, tmp_path, amount):
    shutil.copy(STRICT, tree / "permissions.md")
    marker = tmp_path / "ran.txt"
    add_tool(home, "pay", f"open(r'{marker}','w').write('x')\nprint('{{}}')\n",
             f"category: money\nmax_usd_per_call: {amount}\n")
    api = FakeApi()
    api.Ledger = R.Ledger
    d, *_ = make(home, api, asker=yes)
    r = await d.call("pay", {})
    assert not r.ok and not marker.exists()


# ------------------------------------------------- 3: path-kind arguments, undeclared arguments
PATH_META = "params:\n  file: {type: string, kind: read-path}\n  out: {type: string, kind: write-path}\n"


def path_tool(home, tmp_path):
    marker = tmp_path / "stdin.json"
    add_tool(home, "cp", f"import sys\nopen(r'{marker}','w').write(sys.stdin.read())\nprint('ran')\n", PATH_META)
    return marker


@pytest.mark.parametrize("bad", [["secrets/a.env"], {"p": "secrets/a.env"}, 5, True, None, 1.5, "", "   ", "x\0y"])
async def test_path_argument_must_be_a_plain_string(home, tmp_path, bad):
    marker = path_tool(home, tmp_path)
    api = FakeApi()
    d, *_ = make(home, api, asker=yes)
    r = await d.call("cp", {"file": bad, "out": "o.pdf"})
    assert not r.ok and "refused" in r.text and not marker.exists() and api.seen == []
    r = await d.call("cp", {"file": "in.pdf", "out": bad})
    assert not r.ok and "refused" in r.text and not marker.exists()


async def test_undeclared_arguments_never_reach_the_tool(home, tmp_path):
    marker = path_tool(home, tmp_path)
    d, api, _ = make(home, asker=yes)
    r = await d.call("cp", {"file": "in.pdf", "out": "o.pdf", "evil": "x", "shell": ["rm"]})
    assert r.ok
    sent = json.loads(marker.read_text(encoding="utf-8"))
    assert set(sent) == {"file", "out"}


async def test_path_arguments_are_handed_over_as_the_checked_place(home, tmp_path):
    marker = path_tool(home, tmp_path)
    d, api, _ = make(home, asker=yes)
    assert (await d.call("cp", {"file": "sub/../in.pdf", "out": "o.pdf"})).ok
    sent = json.loads(marker.read_text(encoding="utf-8"))
    assert Path(sent["file"]) == Path(os.path.realpath(home.root / "in.pdf"))      # not relative to the tool folder
    assert Path(sent["out"]).is_absolute()
    from lightmind.model import Check
    assert Check("read", "sub/../in.pdf") in api.seen[-1]                         # the rule check saw what was asked


async def test_path_argument_through_a_link_is_refused(home, tree, tmp_path):
    marker = path_tool(home, tmp_path)
    (tree / "real").mkdir()
    if not link_dir(tree / "alias", tree / "real"):
        pytest.skip("cannot create a link here")
    d, *_ = make(home, asker=yes)
    r = await d.call("cp", {"file": "alias/in.pdf", "out": "o.pdf"})
    assert not r.ok and "refused" in r.text and not marker.exists()


@WIN
@pytest.mark.parametrize("bad", ["in.pdf:stream", "in.pdf::$DATA", "IN~1.PDF", "in.pdf.", "in.pdf ", "//srv/share/in.pdf"])
async def test_windows_aliases_in_path_arguments_are_refused(home, tmp_path, bad):
    marker = path_tool(home, tmp_path)
    d, *_ = make(home, asker=yes)
    r = await d.call("cp", {"file": bad, "out": "o.pdf"})
    assert not r.ok and "refused" in r.text and not marker.exists()


async def test_builtin_arguments_outside_the_schema_are_dropped(home):
    d, *_ = make(home)
    assert (await d.call("write", {"path": "x/a.txt", "content": "one\n", "mode": "a"})).ok
    assert (await d.call("read", {"path": "x/a.txt", "evil": 1})).ok
    assert (await d.call("bash", {"command": f'"{sys.executable}" -c "print(1)"', "shell": "x"})).ok
    assert not (await d.call("read", "not an object")).ok


async def test_model_cannot_supply_captured_values_to_the_secrets_tool(home):
    got = []
    api = FakeApi()
    api.secrets_tool = lambda h, action, ref, **kw: (got.append(kw), f"<secret:{ref}:X>")[1]
    d, *_ = make(home, api)
    r = await d.call("secrets", {"action": "set", "ref": "a/b", "var": "X", "from_ref": "<secret:c/d:Y>",
                                 "captured": "plaintext-from-the-model", "value": "leak"})
    assert r.ok and got == [{"var": "X", "from_ref": "<secret:c/d:Y>"}]
    props = next(s for s in d.schemas() if s["name"] == "secrets")["input_schema"]["properties"]
    assert {"action", "ref", "var", "from_ref"} <= set(props) and not {"captured", "value"} & set(props)


# ------------------------------------------------- 7: read / write / edit refuse aliases
async def test_builtins_refuse_a_path_through_a_link(home, tree):
    real = tree / "real"
    real.mkdir()
    (real / "f.txt").write_text("one\ntwo\n", encoding="utf-8")
    if not link_dir(tree / "alias", real):
        pytest.skip("cannot create a link here")
    d, *_ = make(home)
    for name, args in (("read", {"path": "alias/f.txt"}), ("write", {"path": "alias/g.txt", "content": "x"}),
                       ("edit", {"path": "alias/f.txt", "old": "one", "new": "1"})):
        r = await d.call(name, args)
        assert not r.ok and "refused" in r.text, name
    assert not (real / "g.txt").exists() and (real / "f.txt").read_text(encoding="utf-8") == "one\ntwo\n"
    assert (await d.call("read", {"path": "real/f.txt"})).ok                    # the real place is fine
    assert (await d.call("read", {"path": str(real / "f.txt")})).ok


@WIN
@pytest.mark.parametrize("bad", ["f.txt:stream", "f.txt::$DATA", "F~1.TXT", "f.txt.", "f.txt ", "sub./g.txt"])
async def test_builtins_refuse_windows_aliases(home, tree, bad):
    (tree / "f.txt").write_text("one\n", encoding="utf-8")
    d, *_ = make(home)
    for name, args in (("read", {"path": bad}), ("write", {"path": bad, "content": "x"}),
                       ("edit", {"path": bad, "old": "one", "new": "1"})):
        r = await d.call(name, args)
        assert not r.ok and "refused" in r.text, (name, bad)
    assert (tree / "f.txt").read_text(encoding="utf-8") == "one\n"
    assert sorted(p.name for p in tree.glob("f*")) == ["f.txt"] and not (tree / "sub.").exists()


# ------------------------------------------------- 4: skill_load stays inside its locations
@pytest.mark.parametrize("name", ["../outside", "..", "a/b", "a\\b", "a:b", "C:evil", "", "   ", "x\0y",
                                  "..\\..\\outside", "sub/../outside", "/outside"])
async def test_skill_load_rejects_path_like_names(home, tree, name):
    (tree / "outside").mkdir()
    (tree / "outside" / "tool.md").write_text("---\nname: outside\n---\nOUTSIDE TEXT\n", encoding="utf-8")
    d, *_ = make(home)
    r = await d.call("skill_load", {"name": name})
    assert not r.ok and "refused" in r.text and "OUTSIDE" not in r.text


async def test_skill_load_refuses_a_tool_link_that_leaves_the_tools_location(home, tree):
    (tree / "outside").mkdir()
    (tree / "outside" / "tool.md").write_text("---\nname: escape\n---\nOUTSIDE TEXT\n", encoding="utf-8")
    if not link_dir(home.path("tools") / "escape", tree / "outside"):
        pytest.skip("cannot create a link here")
    d, *_ = make(home)
    r = await d.call("skill_load", {"name": "escape"})
    assert not r.ok and "refused" in r.text and "OUTSIDE" not in r.text


async def test_skill_load_refuses_a_skill_link_that_leaves_the_skills_location(home, tree):
    (tree / "outside").mkdir()
    (tree / "outside" / "SKILL.md").write_text("---\nname: linked\ndescription: d\n---\nOUTSIDE TEXT\n", encoding="utf-8")
    if not link_dir(home.path("skills") / "linked", tree / "outside"):
        pytest.skip("cannot create a link here")
    d, *_ = make(home)
    r = await d.call("skill_load", {"name": "linked"})
    assert not r.ok and "refused" in r.text and "OUTSIDE" not in r.text


async def test_skill_load_still_loads_honest_skills_and_tools(home):
    d, *_ = make(home)
    assert (await d.call("skill_load", {"name": "example-skill"})).ok
    assert "name: pdf" in (await d.call("skill_load", {"name": "pdf"})).text
    assert not (await d.call("skill_load", {"name": "nothing-here"})).ok
