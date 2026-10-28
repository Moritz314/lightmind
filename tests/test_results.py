"""Result folders (results.py, RunDir.results_path): names, counters, lazy creation, rename after the title, the `out`
link and its fallback, `results_dir` in meta.json, `$LM_RESULTS` and the file names of sub-agent answers."""
import json
import logging
import os
import sys
import types
from pathlib import Path

import pytest

from lightmind import results as RS
from lightmind import runs as R
from lightmind import spawn as SP
from lightmind.model import Run, Task
from test_tools import make as make_dispatcher


def mk(home, agent="chat", rid="20261024-120000-chat-ab12", start="2026-10-24T12:00:00+02:00", **kw):
    run = Run(id=rid, agent=agent, start=start, **kw)
    return R.RunDir(home, run, Task(goal="g"))


def meta(rd) -> dict:
    return json.loads((rd.path / "meta.json").read_text("utf-8"))


def folders(home) -> list[str]:
    root = home.path("runs") / "ergebnisse"
    return sorted(p.name for p in root.iterdir()) if root.is_dir() else []


# ------------------------------------------------------------ names
@pytest.mark.parametrize("raw, expected", [
    ('a<b>c:d"e/f\\g|h?i*j', "a b c d e f g h i j"),
    ("Bericht zur Größe der Übung", "Bericht zur Größe der Übung"),
    ("Titel. ", "Titel"),
    ("tab\tand\nnewline\x07bell", "tab and newline bell"),
    ("CON", "_CON"), ("nul.txt", "_nul.txt"), ("com1", "_com1"),
    ("", "ohne Titel"), ("???", "ohne Titel"), ("...", "ohne Titel"),
])
def test_clean_name(raw, expected):
    assert RS.clean_name(raw) == expected


def test_clean_name_is_short_and_never_ends_in_dot_or_space():
    out = RS.clean_name("wort " * 40)
    assert len(out) <= RS.TITLE_MAX and not out.endswith((" ", "."))
    assert RS.clean_name("x" * 49 + ". . .") == "x" * 49


def test_base_name_with_and_without_title():
    assert RS.base_name("2026-10-24T12:00:00+02:00", "Foo: Bar", "chat", "20261024-120000-chat-ab12") == "2026-10-24 Foo Bar"
    assert RS.base_name("2026-10-24T12:00:00+02:00", "", "web", "20261024-120000-web-ab12") == "2026-10-24 web ab12"


def test_folder_and_file_counters(tmp_path):
    a, b, c = (RS.claim_folder(tmp_path, "2026-10-24 Foo") for _ in range(3))
    assert [p.name for p in (a, b, c)] == ["2026-10-24 Foo", "2026-10-24 Foo (2)", "2026-10-24 Foo (3)"]
    assert RS.strip_counter(c.name) == "2026-10-24 Foo"
    assert RS.free_name(tmp_path, "2026-10-24 Foo", own=b.name) == "2026-10-24 Foo (2)"      # an own name counts as free
    f1, f2 = RS.unique_file(tmp_path, "Bericht"), RS.unique_file(tmp_path, "Bericht")
    assert (f1.name, f2.name) == ("Bericht.md", "Bericht (2).md")


def test_short_task_takes_the_first_words_then_label_then_id():
    assert RS.short_task("Lies zuerst die Datei a/b.md und mach dann alles", "x", "s") == "Lies zuerst die Datei a b.md und"
    assert RS.short_task("", "reviewer", "toolu_1") == "reviewer"
    assert RS.short_task("", "", "toolu_1") == "toolu_1"


# ------------------------------------------------------------ the run folder
def test_a_new_run_has_no_out_and_no_result_folder(home):
    rd = mk(home)
    assert not (rd.path / "out").exists() and folders(home) == [] and meta(rd)["results_dir"] == ""


def test_first_use_makes_the_folder_with_a_provisional_name_and_a_link(home):
    rd = mk(home)
    out = rd.results_path()
    assert out == rd.path / "out" and RS.is_link(out)
    assert folders(home) == ["2026-10-24 chat ab12"]
    assert meta(rd)["results_dir"] == "runs/ergebnisse/2026-10-24 chat ab12"
    (out / "a.md").write_text("x", "utf-8")
    assert (home.path("runs") / "ergebnisse" / "2026-10-24 chat ab12" / "a.md").read_text("utf-8") == "x"
    assert rd.results_path() == out and folders(home) == ["2026-10-24 chat ab12"]          # asking again changes nothing


def test_a_title_before_first_use_makes_no_folder_but_names_it_later(home):
    rd = mk(home)
    rd.set_name("title", "Mein Bericht")
    assert folders(home) == []                                          # lazy: a title alone does not make an empty folder
    rd.results_path()
    assert folders(home) == ["2026-10-24 Mein Bericht"]


def test_title_change_renames_the_empty_folder_and_moves_the_link(home):
    rd = mk(home)
    out = rd.results_path()
    rd.set_name("title", "Neuer Titel")
    assert folders(home) == ["2026-10-24 Neuer Titel"]
    assert meta(rd)["results_dir"] == "runs/ergebnisse/2026-10-24 Neuer Titel"
    (out / "a.md").write_text("x", "utf-8")                             # the link was hung on the new folder
    assert (home.path("runs") / "ergebnisse" / "2026-10-24 Neuer Titel" / "a.md").read_text("utf-8") == "x"


def test_a_folder_with_files_keeps_its_name(home):
    rd = mk(home)
    out = rd.results_path()
    (out / "a.md").write_text("x", "utf-8")                             # a file link to it may be in the chat now
    rd.set_name("title", "Neuer Titel")
    rd.run.title = "Automatisch"                                        # what titles.update does, then _meta
    rd._meta()
    assert folders(home) == ["2026-10-24 chat ab12"] and meta(rd)["title"] == "Automatisch"
    assert meta(rd)["results_dir"] == "runs/ergebnisse/2026-10-24 chat ab12" and (out / "a.md").exists()


def test_has_content():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        assert not RS.has_content(Path(d))
        (Path(d) / "sub").mkdir()
        assert RS.has_content(Path(d))


def test_same_title_gets_a_counter(home):
    a, b = mk(home, rid="20261024-120000-chat-aaaa"), mk(home, rid="20261024-120001-chat-bbbb")
    for rd in (a, b):
        rd.results_path()
        rd.set_name("title", "Gleich")
    assert folders(home) == ["2026-10-24 Gleich", "2026-10-24 Gleich (2)"]
    assert meta(a)["results_dir"].endswith("Gleich") and meta(b)["results_dir"].endswith("Gleich (2)")


def test_a_rename_that_fails_keeps_the_old_name_and_logs(home, monkeypatch, caplog):
    rd = mk(home)
    out = rd.results_path()
    real = Path.rename

    def locked(self, target):
        raise PermissionError("in use")
    monkeypatch.setattr(Path, "rename", locked)
    with caplog.at_level(logging.WARNING):
        rd.set_name("title", "Neu")
    monkeypatch.setattr(Path, "rename", real)
    assert folders(home) == ["2026-10-24 chat ab12"] and meta(rd)["title"] == "Neu"
    assert meta(rd)["results_dir"].endswith("chat ab12") and out.exists()
    assert "not renamed" in caplog.text
    rd.set_name("title", "Neu")                                         # tried again at the next write, now it works
    assert folders(home) == ["2026-10-24 Neu"]


def test_an_older_run_with_a_real_out_stays_as_it_is(home):
    rd = mk(home)
    (rd.path / "out").mkdir()
    (rd.path / "out" / "old.md").write_text("old", "utf-8")
    assert rd.results_path() == rd.path / "out"
    rd.set_name("title", "Titel")
    assert folders(home) == [] and not RS.is_link(rd.path / "out") and meta(rd)["results_dir"] == ""
    assert (rd.path / "out" / "old.md").read_text("utf-8") == "old"


def test_an_empty_real_out_is_replaced_by_the_link(home):
    rd = mk(home)
    (rd.path / "out").mkdir()
    assert rd.results_path() == rd.path / "out"            # empty real `out` counts as an older run: left alone
    assert not RS.is_link(rd.path / "out")


def test_without_a_link_the_note_and_the_folder_itself(home, monkeypatch):
    monkeypatch.setattr(RS, "make_link", lambda link, target: False)
    rd = mk(home)
    path = rd.results_path()
    folder = home.root / meta(rd)["results_dir"]
    assert path == folder and (folder).is_dir()
    assert (rd.path / "ergebnis.txt").read_text("utf-8").strip() == str(folder)


def test_a_dangling_link_is_made_again(home):
    rd = mk(home)
    out = rd.results_path()
    RS.drop_link(out)
    assert not out.exists()
    assert rd.results_path() == out and RS.is_link(out)


def test_an_empty_folder_goes_when_the_run_ends_a_used_one_stays(home):
    empty, used = mk(home, rid="20261024-120000-chat-aaaa"), mk(home, rid="20261024-120001-chat-bbbb")
    empty.results_path()
    (used.results_path() / "a.md").write_text("x", "utf-8")
    empty.finish("done")
    used.finish("done")
    assert folders(home) == ["2026-10-24 chat bbbb"]
    assert meta(empty)["results_dir"] == "" and not (empty.path / "out").exists()
    assert meta(used)["results_dir"] == "runs/ergebnisse/2026-10-24 chat bbbb"


def test_run_listings_ignore_the_result_folder(home):
    rd = mk(home)
    rd.results_path()
    assert [r.id for r in R.list_runs(home)] == [rd.run.id]


def test_a_read_only_run_touches_nothing(home):
    rd = mk(home)
    ro = R.RunDir(home, Run(id="20261024-120000-chat-zzzz", start="2026-10-24T12:00:00+02:00"), write=False)
    assert ro.results_path() == ro.path / "out" and not ro.path.exists() and folders(home) == []
    assert rd.path.exists()


# ------------------------------------------------------------ links (Windows side by mock)
def test_make_link_uses_a_junction_on_windows(tmp_path, monkeypatch):
    calls = []
    fake = types.SimpleNamespace(CreateJunction=lambda target, link: calls.append((target, link)))
    monkeypatch.setitem(sys.modules, "_winapi", fake)
    monkeypatch.setattr(sys, "platform", "win32")
    assert RS.make_link(tmp_path / "out", tmp_path / "t") is True
    assert calls == [(str(tmp_path / "t"), str(tmp_path / "out"))]


def test_make_link_says_false_when_the_system_refuses(tmp_path, monkeypatch):
    def refuse(target, link):
        raise OSError("no")
    monkeypatch.setitem(sys.modules, "_winapi", types.SimpleNamespace(CreateJunction=refuse))
    monkeypatch.setattr(sys, "platform", "win32")
    assert RS.make_link(tmp_path / "out", tmp_path / "t") is False


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX symlink")
def test_make_link_is_a_symlink_on_posix(tmp_path):
    (tmp_path / "t").mkdir()
    assert RS.make_link(tmp_path / "out", tmp_path / "t") and os.path.islink(tmp_path / "out")
    assert RS.link_points_to(tmp_path / "out", tmp_path / "t") and not RS.link_points_to(tmp_path / "out", tmp_path)
    RS.drop_link(tmp_path / "out")
    assert (tmp_path / "t").is_dir() and not (tmp_path / "out").exists()


def test_show_run_and_listings_follow_the_link(home):
    rd = mk(home)
    (rd.results_path() / "a.md").write_text("x", "utf-8")
    assert R.show_run(home, rd.run.id)["run"]["results_dir"].startswith("runs/ergebnisse/")
    assert [p.name for p in (rd.path / "out").iterdir()] == ["a.md"]


# ------------------------------------------------------------ env
def test_tool_env_has_lm_results_as_the_stable_out_path(home):
    d, *_ = make_dispatcher(home)
    env = d._env()
    assert Path(env["LM_RESULTS"]) == Path(env["LM_RUN"]) / "out"
    assert Path(env["LM_RESULTS"]).is_dir()
    d.run.set_name("title", "Etwas")                                    # the variable does not change with the title
    assert d._env()["LM_RESULTS"] == env["LM_RESULTS"] and Path(env["LM_RESULTS"]).is_dir()


def test_a_child_for_a_person_does_not_inherit_lm_results():
    from lightmind import node
    assert "LM_RESULTS" in node.RUN_VARIABLES


# ------------------------------------------------------------ spawn
class FakeSession:
    def __init__(self, run):
        self.run = run
        self.dispatcher = types.SimpleNamespace(masker=types.SimpleNamespace(mask=lambda t: t))
        self.subs = types.SimpleNamespace(feed=lambda ev: None)
        self.readonly = False
        self.home = run.home


def hub_for(home):
    rd = mk(home)
    return rd, SP.Background(FakeSession(rd))


def test_a_sub_agent_answer_is_named_after_its_brief(home):
    rd, hub = hub_for(home)
    hub.finish("toolu_01ABC", "reviewer", True, "all fine", "Pruefe den Diff der Datei foo.py gründlich auf Fehler")
    hub.finish("toolu_02DEF", "reviewer", True, "also fine", "Pruefe den Diff der Datei foo.py gründlich auf Fehler")
    folder = home.root / meta(rd)["results_dir"]
    assert sorted(p.name for p in folder.iterdir()) == ["Pruefe den Diff der Datei foo.py (2).md", "Pruefe den Diff der Datei foo.py.md"]
    assert "sub-toolu" not in "".join(p.name for p in folder.iterdir())
    told = hub.ready[0]
    assert str(folder / "Pruefe den Diff der Datei foo.py.md") in told                    # the path that goes to the lead is the real file
    assert (folder / "Pruefe den Diff der Datei foo.py.md").read_text("utf-8") == "all fine"


def test_a_sub_agent_answer_without_a_brief_uses_the_agent_name(home):
    rd, hub = hub_for(home)
    hub.finish("toolu_01ABC", "con", True, "x")
    folder = home.root / meta(rd)["results_dir"]
    assert [p.name for p in folder.iterdir()] == ["_con.md"]
