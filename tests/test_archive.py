"""The archive: a marker file `runs/<id>/archived.json`, no folder is moved.  Archived chats are missing from the session list,
the grouped list and the resume list, and come back with `unarchive`."""
import json
import os

import pytest

from lightmind import resume, runs
from lightmind.model import Run
from lightmind.runs import RunDir


def chat(home, rid: str, status: str = "done", parent: str = "", when: str = "2026-10-23T10:00:00") -> RunDir:
    """A chat on disk: meta.json plus the `session` event that makes a run a chat."""
    rd = RunDir(home, Run(id=rid, status=status, parent=parent, start=when, account="abo", model="haiku"))
    rd.event("session", {"account": "abo"})
    rd.event("message", {"role": "user", "text": f"hello {rid}"})
    return rd


def ids(rows) -> list[str]:
    return [r.id for r in rows]


# ------------------------------------------------------------------------ the marker
def test_archive_writes_the_marker_and_moves_nothing(home):
    rd = chat(home, "20261023-100000-chat-aaaa")
    assert runs.is_archived(rd.path) is False
    assert runs.archive(home, rd.run.id, by="phone") == "archived"
    marker = json.loads((rd.path / "archived.json").read_text("utf-8"))
    assert marker["by"] == "phone" and marker["status"] == "done" and marker["at"]
    assert runs.is_archived(rd.path) is True
    assert (rd.path / "meta.json").is_file() and (rd.path / "events.jsonl").is_file()      # the folder stays where it is
    assert runs.archive(home, rd.run.id) == "already archived"
    assert json.loads((rd.path / "archived.json").read_text("utf-8"))["by"] == "phone"      # the first marker stays


def test_unarchive_removes_the_marker_and_says_so_when_there_is_none(home):
    rd = chat(home, "20261023-100000-chat-aaaa")
    runs.archive(home, rd.run.id)
    assert runs.unarchive(home, rd.run.id) == "restored"
    assert not (rd.path / "archived.json").exists() and runs.is_archived(rd.path) is False
    assert runs.unarchive(home, rd.run.id) == "not archived"


def test_a_chat_that_does_not_exist_is_an_error_not_a_marker(home):
    with pytest.raises(ValueError, match="no such chat"):
        runs.archive(home, "20261023-100000-chat-nope")
    with pytest.raises(ValueError, match="no such chat"):
        runs.unarchive(home, "20261023-100000-chat-nope")
    assert not (home.path("runs") / "20261023-100000-chat-nope").exists()      # nothing was created for it


def test_archiving_a_running_chat_nobody_holds_closes_it_on_disk_first(home):
    rd = chat(home, "20261023-100000-chat-aaaa", status="running")
    runs.archive(home, rd.run.id, by="cli")
    meta = json.loads((rd.path / "meta.json").read_text("utf-8"))
    assert meta["status"] == "done"
    assert json.loads((rd.path / "archived.json").read_text("utf-8"))["status"] == "running"     # the state it had before
    assert runs.END_RE.search((rd.path / "log.md").read_text("utf-8"))


def test_a_chat_this_process_holds_is_refused_until_it_lets_go(home):
    rd = chat(home, "20261023-100000-chat-aaaa", status="running")
    assert runs.acquire_lock(home, rd.run.id) == 0
    with pytest.raises(ValueError, match="open in this process"):
        runs.archive(home, rd.run.id)
    assert not runs.is_archived(rd.path)                     # refused means no marker
    runs.release_lock(home, rd.run.id)
    runs.archive(home, rd.run.id)
    assert runs.is_archived(rd.path)


def test_a_chat_another_process_holds_is_asked_and_refused_when_it_does_not_answer(home, monkeypatch):
    rd = chat(home, "20261023-100000-chat-aaaa", status="paused")
    other = os.getpid() + 1
    asked = []
    holder = {"pid": other}
    monkeypatch.setattr(runs, "lock_holder", lambda h, rid: holder["pid"])
    monkeypatch.setattr(runs, "request_stop", lambda h, rid, wait=4.0: asked.append(rid) or "asked")
    with pytest.raises(ValueError, match=f"still running in process {other}"):
        runs.archive(home, rd.run.id)
    assert asked == [rd.run.id] and not runs.is_archived(rd.path)
    holder["pid"] = 0                                        # now it lets go
    runs.archive(home, rd.run.id)
    assert runs.is_archived(rd.path)


# ------------------------------------------------------------------------ the lists
def test_list_sessions_filters_by_archive_state(home):
    a, b = chat(home, "20261023-100000-chat-aaaa"), chat(home, "20261023-110000-chat-bbbb")
    runs.archive(home, a.run.id)
    assert ids(runs.list_sessions(home)) == [b.run.id]                      # the default: no archive
    assert ids(runs.list_sessions(home, archived=False)) == [b.run.id]
    assert ids(runs.list_sessions(home, archived=True)) == [a.run.id]
    assert ids(runs.list_sessions(home, archived=None)) == [b.run.id, a.run.id]
    flags = {r.id: r.archived for r in runs.list_sessions(home, archived=None)}
    assert flags == {a.run.id: True, b.run.id: False}


def test_the_limit_counts_only_the_chats_that_are_listed(home):
    old = [chat(home, f"20261023-1{i}0000-chat-a{i:03d}") for i in range(3)]
    new = chat(home, "20261023-200000-chat-zzzz")
    runs.archive(home, new.run.id)
    assert ids(runs.list_sessions(home, limit=2)) == [old[2].run.id, old[1].run.id]      # the archived newest does not eat a place


def test_children_of_an_archived_chat_leave_the_grouped_list_with_it(home):
    boss = chat(home, "20261023-100000-chat-boss1", when="2026-10-23T10:00:00")
    kid = chat(home, "20261023-101000-chat-kid1", parent=boss.run.id, when="2026-10-23T10:10:00")
    grandkid = chat(home, "20261023-102000-chat-kid2", parent=kid.run.id, when="2026-10-23T10:20:00")
    other = chat(home, "20261023-090000-chat-othe", when="2026-10-23T09:00:00")
    assert set(ids(runs.list_session_groups(home))) == {boss.run.id, kid.run.id, grandkid.run.id, other.run.id}
    runs.archive(home, boss.run.id)                          # only the head is archived
    assert not runs.is_archived(kid.path)
    assert ids(runs.list_session_groups(home)) == [other.run.id]
    assert set(ids(runs.list_session_groups(home, archived=True))) == {boss.run.id, kid.run.id, grandkid.run.id}
    assert set(ids(runs.list_session_groups(home, archived=None))) == {boss.run.id, kid.run.id, grandkid.run.id, other.run.id}
    runs.unarchive(home, boss.run.id)
    assert set(ids(runs.list_session_groups(home))) == {boss.run.id, kid.run.id, grandkid.run.id, other.run.id}


def test_an_archived_child_alone_leaves_the_list_but_not_its_parent(home):
    boss = chat(home, "20261023-100000-chat-boss1")
    kid = chat(home, "20261023-101000-chat-kid1", parent=boss.run.id)
    runs.archive(home, kid.run.id)
    assert ids(runs.list_session_groups(home)) == [boss.run.id]


def test_a_parent_loop_does_not_hang_the_archive_filter(home):
    a = chat(home, "20261023-100000-chat-aaaa", parent="20261023-110000-chat-bbbb")
    b = chat(home, "20261023-110000-chat-bbbb", parent=a.run.id)
    assert set(ids(runs.list_session_groups(home))) == {a.run.id, b.run.id}


# ------------------------------------------------------------------------ the resume menu's data
def test_the_resume_list_and_the_newest_chat_skip_the_archive(home):
    a, b = chat(home, "20261023-100000-chat-aaaa"), chat(home, "20261023-110000-chat-bbbb")
    assert resume.latest(home) == b.run.id
    runs.archive(home, b.run.id)
    assert resume.latest(home) == a.run.id
    assert [r.id for r in resume.rows(home)] == [a.run.id]
    assert [r.id for r in resume.rows(home, archived=True)] == [b.run.id]
    runs.archive(home, a.run.id)
    assert resume.latest(home) is None and resume.rows(home) == []


def test_an_archived_chat_is_still_found_by_name_or_id_and_the_note_says_it_is_archived(home):
    rd = chat(home, "20261023-100000-chat-aaaa")
    assert resume.archived_note(home, rd.run.id) == ""
    runs.archive(home, rd.run.id)
    assert resume.lookup(home, rd.run.id) == rd.run.id
    assert resume.lookup(home, "aaaa") == rd.run.id
    note = resume.archived_note(home, rd.run.id)
    assert "archived" in note and rd.run.id in note
