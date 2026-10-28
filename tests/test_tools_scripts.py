"""Pure parts of the shipped tool scripts (templates/tools/**): no network, no GUI, no browser."""
import importlib.util
import sys
from pathlib import Path

import pytest

TOOLS = Path(__file__).resolve().parents[1] / "src" / "lightmind" / "templates" / "tools"


def load(rel: str):
    path = TOOLS / rel
    sys.path.insert(0, str(path.parent))
    try:
        spec = importlib.util.spec_from_file_location(path.stem + "_t", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    finally:
        sys.path.remove(str(path.parent))


browser = load("browser/browser.py")
pc = load("pc/pc.py")
apply = pytest.importorskip("yaml") and load("triggers/apply.py")


# ---- browser
def test_steps_parse():
    s = browser.parse_steps('[{"goto":"http://x"},{"click":"#a"},{"fill":["#u","m"]},'
                            '{"fill":{"selector":"#p","text":"t"}},{"press":"Enter"},{"wait":500},{"wait":".done"}]')
    assert s[0] == ("goto", ("http://x",)) and s[2] == ("fill", ("#u", "m")) and s[3] == ("fill", ("#p", "t"))
    assert s[5] == ("wait", (500,)) and s[6] == ("wait", (".done",))
    assert browser.parse_steps(None) == []


def test_steps_file(tmp_path):
    f = tmp_path / "s.json"
    f.write_text('[{"click":"a"}]')
    assert browser.parse_steps(f"@{f}") == [("click", ("a",))]


@pytest.mark.parametrize("bad", ["nope", "{}", '[{"click":1}]', '[{"jump":"a"}]', '[{"fill":["a"]}]',
                                 '[{"click":"a","press":"b"}]', '["click"]', '[{"wait":true}]'])
def test_steps_bad(bad):
    with pytest.raises(ValueError):
        browser.parse_steps(bad)


def test_profile_dir(tmp_path):
    assert browser.profile_dir("shop", str(tmp_path)) == tmp_path / "profiles" / "shop"
    for bad in ("", "../x", "a/b", ".hidden"):
        with pytest.raises(ValueError):
            browser.profile_dir(bad, str(tmp_path))


# ---- pc
def test_pc_dry_run(capsys):
    assert pc.main(["--dry-run", "click", "10", "20", "--double"]) == 0
    assert capsys.readouterr().out.strip() == "[dry-run] double-click left at 10,20"
    assert pc.main(["--dry-run", "key", "Ctrl+S"]) == 0
    assert "ctrl+s" in capsys.readouterr().out
    assert pc.main(["--dry-run", "type", "hello"]) == 0
    assert "5 characters" in capsys.readouterr().out
    assert pc.main(["--dry-run", "key", "+"]) == 2


# ---- triggers
def _write(folder, name, text):
    (folder / name).write_text(text, encoding="utf-8")


def test_load_triggers(tmp_path):
    _write(tmp_path, "_example.md", "---\nagent: x\n---\n")
    _write(tmp_path, "a.md", '---\nschedule: "0 7 * * 1-5"\nagent: ag\ntask: do it\n---\n')
    _write(tmp_path, "b.md", '---\nname: bee\nwhen: "cron */5 * * * *"\nagent: ag\ntask: t\nenabled: false\n---\n')
    _write(tmp_path, "c.md", "---\nwhen: telegram\nagent: ag\ntask: t\n---\n")
    _write(tmp_path, "d.md", "no frontmatter")
    ts = {t["name"]: t for t in apply.load_triggers(tmp_path)}
    assert set(ts) == {"a", "bee", "c", "d"}
    assert ts["a"]["enabled"] and not ts["a"]["error"] and ts["a"]["schedule"] == "0 7 * * 1-5"
    assert not ts["bee"]["enabled"] and ts["bee"]["schedule"] == "*/5 * * * *"
    assert ts["c"]["error"] and ts["d"]["error"]


def test_triggers_cannot_smuggle_crontab_lines(tmp_path):
    _write(tmp_path, "e.md", '---\nname: "x\\n* * * * * rm -rf ~"\nschedule: "0 7 * * *"\nagent: ag\ntask: t\n---\n')
    _write(tmp_path, "f.md", '---\nschedule: "0 7 * * *"\nagent: ag\ntask: "a\\n* * * * * evil"\n---\n')
    _write(tmp_path, "g.md", '---\nschedule: "0 7 * * * evil"\nagent: ag\ntask: t\n---\n')
    _write(tmp_path, "h.md", '---\nschedule: "@reboot x y z w"\nagent: ag\ntask: t\n---\n')
    ts = apply.load_triggers(tmp_path)
    assert len(ts) == 4 and all(t["error"] for t in ts)
    assert apply.render_block(tmp_path, [t for t in ts if not t["error"]]).count("\n* * * * *") == 0


def test_cron_block_and_merge(tmp_path):
    t = {"name": "morning", "schedule": "0 7 * * *", "agent": "ag", "task": "100% done, it's 'x'"}
    block = apply.render_block(tmp_path, [t])
    lines = block.splitlines()
    assert lines[0] == "# lightmind triggers" and lines[-1] == "# end lightmind triggers" and lines[1] == "# morning"
    assert lines[2].startswith("0 7 * * * ") and "run --detach -a ag" in lines[2] and "100\\% done" in lines[2]
    base = "MAILTO=x\n0 1 * * * backup\n"
    once = apply.merge_crontab(base, block)
    assert once.startswith(base) and apply.merge_crontab(once, block) == once
    two = apply.merge_crontab(once, apply.render_block(tmp_path, [{**t, "name": "n2"}]))
    assert two.count("# lightmind triggers") == 1 and "# n2" in two and "# morning" not in two
    assert apply.merge_crontab(two, None) == base
    assert apply.merge_crontab("", None) == ""


def test_schtasks_args(tmp_path):
    def mk(s):
        return apply.schtasks_args(tmp_path, {"name": "n", "schedule": s, "agent": "ag", "task": 'say "hi"'})
    a = mk("30 7 * * 1-5")
    assert a[:6] == ["schtasks", "/Create", "/F", "/TN", "Lightmind n", "/TR"]
    assert "lm.cmd" in a[6] and "run --detach -a ag" in a[6] and 'say \\"hi\\"' not in a[6] and "'hi'" in a[6]
    assert a[7:] == ["/SC", "WEEKLY", "/D", "MON,TUE,WED,THU,FRI", "/ST", "07:30"]
    assert mk("0 9 * * 1,4")[-3:-1] == ["/D", "MON,THU"] or "MON,THU" in mk("0 9 * * 1,4")
    assert mk("0 6 * * *")[7:] == ["/SC", "DAILY", "/ST", "06:00"]
    assert mk("*/10 * * * *")[7:] == ["/SC", "MINUTE", "/MO", "10"]
    assert mk("15 * * * *")[7:] == ["/SC", "HOURLY", "/ST", "00:15"]
    assert mk("0 6 1 * *")[7:] == ["/SC", "MONTHLY", "/D", "1", "/ST", "06:00"]
    for bad in ("0 6 * 3 *", "0-5 6 * * *", "0 6 1 * 1", "0 25 * * *"):
        with pytest.raises(ValueError):
            mk(bad)
