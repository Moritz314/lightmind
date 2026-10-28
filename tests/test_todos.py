"""To-dos of a chat: the Textual-free module, migration of old files, the `todo` tool and the context block."""
import json

import pytest

from lightmind import loop as L
from lightmind import todos as T

from test_tui_support import FakeProv, install, quiet


@pytest.fixture
def prov(monkeypatch, home):
    quiet(home)
    return install(monkeypatch, FakeProv())


async def send(s, text="go"):
    async for _ in s.send(text):
        pass


def last_user(prov) -> str:
    return prov.requests[-1].messages[-1].text


# ------------------------------------------------------------------------ the module
def test_tree_items_survive_saving_and_loading_in_creation_order(tmp_path):
    s = T.Store(tmp_path)
    a = s.add_task("loose", "some info")
    f = s.add_folder("Feature A")
    b = s.add_task("in folder", folder="feature a")             # a folder is found by name, case does not matter
    n = s.add_note("idea", "long text", folder=f.id)
    c = s.add_note("root note")
    s.annotate(a.id, "mind windows")
    s2 = T.Store(tmp_path)
    assert [x.id for x in s2.items] == [a.id, f.id, c.id]
    assert [x.id for x in s2.items[1].items] == [b.id, n.id]
    assert s2.items[0].info == "some info" and s2.items[0].remarks[0].text == "mind windows"
    assert s2.items[2].title == "root note" and s2.items[1].items[1].text == "long text"
    assert s2.parent(b.id).id == f.id and s2.parent(a.id) is None


def test_states_cycle_stay_in_place_and_a_bad_state_is_refused(tmp_path):
    s = T.Store(tmp_path)
    a, b = s.add_task("a"), s.add_task("b")
    s.cycle(a.id)
    assert s.task(a.id).state == "doing"
    s.cycle(a.id)
    assert s.task(a.id).state == "done" and [x.id for x in s.items] == [a.id, b.id]      # nothing jumps
    s.cycle(a.id)
    assert s.task(a.id).state == "open"
    with pytest.raises(ValueError):
        s.update(a.id, state="halfway")
    with pytest.raises(KeyError):
        s.task("t99")


def test_folders_are_flat_unique_and_deleting_takes_the_content_along(tmp_path):
    s = T.Store(tmp_path)
    f = s.add_folder("Work")
    with pytest.raises(ValueError):
        s.add_folder("work")
    with pytest.raises(KeyError):
        s.add_task("x", folder="nowhere")
    t = s.add_task("inside", folder="Work")
    s.add_task("outside")
    s.delete(f.id)
    assert [x.title for x in s.items] == ["outside"]
    with pytest.raises(KeyError):
        s.find(t.id)
    s.delete(s.items[0].id)
    assert s.items == []


def test_a_second_store_sees_changes_of_the_first(tmp_path):
    a, b = T.Store(tmp_path), T.Store(tmp_path)
    assert not b.changed_on_disk()
    a.add_task("from a")
    assert b.changed_on_disk()
    b.reload()
    assert [x.title for x in b.items] == ["from a"] and not b.changed_on_disk()


def test_a_damaged_file_reads_as_empty(tmp_path):
    (tmp_path / "todos.json").write_text("{not json", "utf-8")
    assert T.Store(tmp_path).items == []
    (tmp_path / "todos.json").write_text("[1, 2]", "utf-8")
    assert T.Store(tmp_path).items == []


def test_the_old_lists_and_the_notes_file_move_into_the_tree_without_loss(tmp_path):
    old = {"seq": 7, "lists": [
        {"id": "l1", "title": "feature A", "status": "paused", "created": "2026-10-20T10:00:00+02:00",
         "tasks": [{"id": "t2", "title": "write", "state": "doing", "info": "half", "updated": "x",
                    "notes": [{"id": "n3", "text": "mind it", "ts": "2026-10-21T10:00:00+02:00", "by": "user", "seen": False}]},
                   {"id": "t4", "title": "test", "state": "done"}]},
        {"id": "l5", "title": "feature B", "status": "active", "tasks": [{"id": "t6", "title": "other"}]}]}
    (tmp_path / "todos.json").write_text(json.dumps(old), "utf-8")
    (tmp_path / "notes.md").write_text("my thoughts\nsecond line\n", "utf-8")
    s = T.Store(tmp_path)
    fa, fb, note = s.items
    assert (fa.title, fb.title) == ("feature A", "feature B") and isinstance(note, T.Note)
    assert [(t.title, t.state) for t in fa.items] == [("write", "doing"), ("test", "done")]
    r = fa.items[0].remarks[0]
    assert (r.text, r.by, r.seen) == ("mind it", "user", False)                    # still unseen: the session hears it
    assert note.title == "Notes" and note.text == "my thoughts\nsecond line"
    assert note.id not in {"l1", "t2", "n3", "t4", "l5", "t6"}                     # ids are not reused
    assert not (tmp_path / "notes.md").exists() and (tmp_path / "notes.md.old").exists()
    raw = json.loads((tmp_path / "todos.json").read_text("utf-8"))
    assert "lists" not in raw and len(raw["items"]) == 3                             # saved in the new format
    assert [x.id for x in T.Store(tmp_path).items] == [x.id for x in s.items]        # and stable the second time
    assert len(T.Store(tmp_path).items) == 3


def test_only_a_notes_file_becomes_one_note_in_the_root(tmp_path):
    (tmp_path / "notes.md").write_text("just text", "utf-8")
    s = T.Store(tmp_path)
    assert [(x.title, x.text) for x in s.items] == [("Notes", "just text")]


def test_the_context_block_comes_only_when_something_changed(tmp_path):
    s = T.Store(tmp_path)
    assert T.context_block(s)[0] == ""                    # nothing at all
    f = s.add_folder("feature")
    t = s.add_task("do it", "careful", folder=f.id)
    s.add_note("idea", "a text")
    blk, dig = T.context_block(s)
    assert "folder" in blk and "do it" in blk and "careful" in blk and "idea" in blk
    assert T.context_block(s, dig)[0] == ""
    s.update(t.id, state="doing")
    blk2, dig2 = T.context_block(s, dig)
    assert "[~]" in blk2
    s.cycle(t.id)
    s.cycle(t.id)
    assert T.context_block(s, dig2)[0] != ""


def test_new_remarks_of_user_come_once_and_do_not_count_as_a_change_afterwards(tmp_path):
    s = T.Store(tmp_path)
    t = s.add_task("do it")
    _, dig = T.context_block(s)
    s.annotate(t.id, "please hurry")
    s.annotate(t.id, "agent answer", by="agent")
    blk, dig2 = T.context_block(s, dig)
    assert "New remarks from the user" in blk and "please hurry" in blk
    assert T.context_block(s, dig2)[0] == ""              # told: silent
    assert s.unseen() == []


# ------------------------------------------------------------------------ the tool
async def test_the_todo_tool_works_on_the_tree(home, prov):
    s = L.open_session(home)
    call = lambda **kw: s.dispatcher.call("todo", kw)
    assert (await call(action="list")).text == "nothing yet"
    r = await call(action="add_folder", title="feature A")
    assert r.ok and "f1" in r.text
    t = (await call(action="add_task", title="write module", info="see todos.py", folder="Feature A")).text
    tid = t.split()[2].rstrip(":")
    loose = (await call(action="add_task", title="loose one")).text
    assert (await call(action="add_note", title="idea", text="remember", folder="f1")).ok
    assert (await call(action="update", id=tid, status="doing", info="half done")).ok
    out = (await call(action="list")).text
    assert "write module" in out and "half done" in out and "folder f1 feature A" in out and "loose one" in out
    assert "remember" in (await call(action="list", folder="feature a")).text
    assert "loose one" not in (await call(action="list", folder="feature a")).text
    assert not (await call(action="update", id="t99", status="done")).ok
    assert not (await call(action="update", id=tid, status="halfway")).ok
    assert not (await call(action="update", id=tid)).ok
    assert not (await call(action="add_task", title="x", folder="nowhere")).ok
    assert not (await call(action="frobnicate")).ok
    assert (await call(action="comments")).text == "(no remarks)"
    T.Store(s.run.path).annotate(tid, "the user: mind the tests")
    assert "mind the tests" in (await call(action="comments", id=tid)).text
    nid = [x for x in T.Store(s.run.path).folders()[0].items if isinstance(x, T.Note)][0].id
    assert (await call(action="update", id=nid, text="changed")).ok
    assert "changed" in (await call(action="list", folder="f1")).text
    assert (await call(action="delete", id=loose.split()[2].rstrip(":"))).ok
    assert (await call(action="delete", id="f1")).ok
    assert T.Store(s.run.path).items == []
    s.close()


async def test_the_schema_lists_the_tool(home, prov):
    s = L.open_session(home)
    sch = {x["name"]: x for x in s.dispatcher.schemas()}
    assert "todo" in sch and "add_folder" in str(sch["todo"])
    s.close()


# ------------------------------------------------------------------------ the session
async def test_the_session_hears_about_changes_and_new_remarks_but_not_twice(home, prov):
    s = L.open_session(home)
    await send(s, "hello")
    assert "To-dos" not in last_user(prov)                # nothing yet: no block
    st = T.Store(s.run.path)
    st.add_folder("feature")
    t = st.add_task("do the thing", folder="feature")
    await send(s, "next")
    assert "To-dos changed" in last_user(prov) and "do the thing" in last_user(prov)
    await send(s, "again")
    assert "To-dos changed" not in last_user(prov)        # unchanged: silent
    T.Store(s.run.path).annotate(t.id, "the user says: mind the tests")
    await send(s, "third")
    assert "mind the tests" in last_user(prov) and "New remarks from the user" in last_user(prov)
    await send(s, "fourth")
    assert "mind the tests" not in last_user(prov)
    s.close()


async def test_a_resumed_session_has_the_data_and_tells_the_session_again(home, prov):
    s = L.open_session(home)
    st = T.Store(s.run.path)
    st.add_task("keep me")
    st.add_note("later", "for later")
    await send(s, "hello")
    sid = s.id
    s.close()
    again = L.resume_session(home, sid)
    assert [x.title for x in T.Store(again.run.path).items] == ["keep me", "later"]
    await send(again, "back")
    assert "keep me" in last_user(prov)
    again.close()
