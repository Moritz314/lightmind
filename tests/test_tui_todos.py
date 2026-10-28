"""To-dos in the chat (Ctrl+N): split and switch layout, the tree, its keys, resume.  Textual Pilot."""
import pytest

from lightmind import loop as L
from lightmind import todos as T
from lightmind.tui.chat import ChatScreen
from lightmind.tui.chatinput import ChatInput
from lightmind.tui.notes import EditArea, NotesPane, TodoView

from test_boot import set_ui
from test_tui_support import FakeProv, install, open_chat, quiet, running, until


@pytest.fixture
def prov(monkeypatch, home):
    quiet(home)
    return install(monkeypatch, FakeProv())


def pane(app) -> NotesPane:
    return app.screen.query_one(NotesPane)


def view(app) -> TodoView:
    return pane(app).query_one(TodoView)


def rows(app) -> list[str]:
    return [r[2].plain for r in view(app).rows]


def store(app) -> T.Store:
    return T.Store(app.chat.s.run.path)


async def seen(app, pilot, word):
    """The tool wrote into the tree behind the panel's back: ask the panel to look now (it looks by itself every 0.8 s) instead of waiting for that."""
    await until(pilot, lambda: (pane(app).poll(), any(word in x for x in rows(app)))[1], timeout=4)


async def opened(app, pilot):
    await open_chat(app, pilot)
    await pilot.press("ctrl+n")
    await until(pilot, lambda: pane(app).display)
    await pilot.pause(0.1)


async def type_in(pilot, text):
    await pilot.press(*text, "enter")


async def write_in(pilot, text):
    """Type into the multi-line editor (a newline in `text` is Enter) and save with Ctrl+S."""
    await pilot.press(*[("enter" if c == chr(10) else c) for c in text], "ctrl+s")


async def test_ctrl_n_opens_the_panel_beside_the_chat_in_split_and_again_closes_it(home, prov):
    async with running(home, size=(140, 40)) as (app, pilot):
        await open_chat(app, pilot)
        assert not pane(app).display
        await pilot.press("ctrl+n")
        assert pane(app).display and app.screen.query_one("#box").display        # split: both are there
        assert isinstance(app.focused, TodoView)
        assert pane(app).region.x > app.screen.query_one("#box").region.x
        await pilot.press("ctrl+n")
        assert not pane(app).display and isinstance(app.focused, ChatInput)


async def test_switch_replaces_the_chat_and_under_120_columns_it_is_always_switch(home, prov):
    set_ui(home, notes_layout="switch")
    async with running(home, size=(140, 40)) as (app, pilot):
        await open_chat(app, pilot)
        await pilot.press("ctrl+n")
        assert pane(app).display and not app.screen.query_one("#box").display
        await pilot.press("escape")                                           # Esc goes back to the chat
        assert not pane(app).display and app.screen.query_one("#box").display
    set_ui(home, notes_layout="split")
    async with running(home, size=(100, 40)) as (app, pilot):
        await open_chat(app, pilot)
        await pilot.press("ctrl+n")
        assert pane(app).display and not app.screen.query_one("#box").display


async def test_create_in_the_root_and_in_a_folder_and_walk_with_ctrl_arrows(home, prov):
    async with running(home, size=(140, 40)) as (app, pilot):
        await opened(app, pilot)
        assert view(app).cur == "root"
        await pilot.press("t")
        await type_in(pilot, "loose task")                                    # selected: the root -> root
        await pilot.press("f")
        await type_in(pilot, "Work")
        st = store(app)
        assert [x.title for x in st.items] == ["loose task", "Work"] and view(app).cur == st.items[1].id
        await pilot.press("t")
        await type_in(pilot, "in work")                                       # the folder is selected -> inside it
        await pilot.press("n")
        await type_in(pilot, "idea")
        await write_in(pilot, "note text")
        st = store(app)
        assert [x.title for x in st.items[1].items] == ["in work", "idea"] and st.items[1].items[1].text == "note text"
        assert [x.title for x in st.items] == ["loose task", "Work"]
        await pilot.press("ctrl+up", "ctrl+up")                               # idea -> in work -> Work
        assert view(app).cur == st.items[1].id
        await pilot.press("ctrl+up")
        assert view(app).cur == st.items[0].id
        await pilot.press("ctrl+up")
        assert view(app).cur == "root"
        await pilot.press("ctrl+up", "ctrl+up")                               # above the list: description, then name
        assert view(app).cur == "@name"
        await pilot.press("ctrl+up")                                          # at the top: stays
        assert view(app).cur == "@name"
        await pilot.press("ctrl+down", "ctrl+down")
        assert view(app).cur == "root"
        await pilot.press("ctrl+down", "ctrl+down", "down")                 # plain arrows work too
        assert view(app).cur == st.items[1].items[0].id
        await pilot.press("t")                                                # selected item is in Work -> Work
        await type_in(pilot, "second in work")
        assert "second in work" in [x.title for x in store(app).items[1].items]
        for _ in range(8):                                                     # walk up to the root (the name rows lie above it)
            if view(app).cur == "root":
                break
            await pilot.press("ctrl+up")
        assert view(app).cur == "root"
        await pilot.press("t")                                                # selected: root again
        await type_in(pilot, "second loose")
        assert [x.title for x in store(app).items][-1] == "second loose"


async def test_space_cycles_a_task_and_it_stays_in_place(home, prov):
    async with running(home, size=(140, 40)) as (app, pilot):
        await opened(app, pilot)
        st = store(app)
        a, b = st.add_task("alpha"), st.add_task("beta")
        await seen(app, pilot, "beta")
        await pilot.press("ctrl+down")
        assert view(app).cur == a.id
        await pilot.press("space")
        assert store(app).task(a.id).state == "doing"
        await pilot.press("space")
        assert store(app).task(a.id).state == "done"
        plain = rows(app)
        assert [i for i, x in enumerate(plain) if "alpha" in x] < [i for i, x in enumerate(plain) if "beta" in x]
        await pilot.press("space")
        assert store(app).task(a.id).state == "open"
        await pilot.press("ctrl+up")                                          # on the root: space does nothing
        await pilot.press("space")
        assert [t.state for t in store(app).items] == ["open", "open"]


async def test_enter_opens_folders_tasks_and_notes_and_a_remark_is_added(home, prov):
    async with running(home, size=(140, 40)) as (app, pilot):
        await opened(app, pilot)
        st = store(app)
        f = st.add_folder("Work")
        t = st.add_task("write", "agent found this", folder=f.id)
        n = st.add_note("idea", "the long text of the note")
        await seen(app, pilot, "idea")
        assert any("write" in x for x in rows(app))                            # a folder starts open
        await pilot.press("ctrl+down")
        assert view(app).cur == f.id
        await pilot.press("enter")
        assert not any("write" in x for x in rows(app))
        assert any("▸" in x and "Work" in x for x in rows(app))
        await pilot.press("enter")
        assert any("write" in x for x in rows(app)) and any("▾" in x and "Work" in x for x in rows(app))
        await pilot.press("ctrl+down")
        assert view(app).cur == t.id
        await pilot.press("a")
        await write_in(pilot, "mind windows")
        r = store(app).task(t.id).remarks
        assert [(x.text, x.by, x.seen) for x in r] == [("mind windows", "user", False)]
        assert not any("agent found this" in x for x in rows(app))
        await pilot.press("enter")
        assert any("agent found this" in x for x in rows(app)) and any("mind windows" in x for x in rows(app))
        await pilot.press("enter")
        assert not any("mind windows" in x for x in rows(app))
        await pilot.press("ctrl+down")
        assert view(app).cur == n.id
        await pilot.press("enter")
        assert any("long text" in x for x in rows(app))
        await pilot.press("enter")
        assert not any("long text" in x for x in rows(app))


async def test_edit_and_delete_with_a_question(home, prov):
    async with running(home, size=(140, 40)) as (app, pilot):
        await opened(app, pilot)
        st = store(app)
        f = st.add_folder("Work")
        t = st.add_task("old title", folder=f.id)
        n = st.add_note("idea", "old text")
        await seen(app, pilot, "idea")
        await pilot.press("ctrl+down", "ctrl+down")
        assert view(app).cur == t.id
        await pilot.press("e")
        await pilot.press("ctrl+u")                                   # clear the prefilled title
        await type_in(pilot, "new title")
        assert store(app).task(t.id).title == "new title"
        await pilot.press("ctrl+down", "e")
        await pilot.press("ctrl+u")
        await type_in(pilot, "idea 2")
        pane(app).query_one(EditArea).select_all()                                   # next: the text
        await write_in(pilot, "new text")
        assert (store(app).find(n.id).title, store(app).find(n.id).text) == ("idea 2", "new text")
        await pilot.press("delete", "x")                                        # any other key: no
        assert store(app).find(n.id)
        await pilot.press("delete", "y")
        assert [x.title for x in store(app).items] == ["Work"]
        await pilot.press("ctrl+up")                                            # to the folder
        await pilot.press("delete", "y")
        assert store(app).items == []


async def test_a_resumed_chat_shows_its_tree(home, prov):
    s = L.open_session(home)
    st = T.Store(s.run.path)
    st.add_folder("old work")
    st.add_task("still open", folder="old work")
    st.add_note("from before", "text")
    sid = s.id
    s.close()
    async with running(home, size=(140, 40)) as (app, pilot):
        session = L.resume_session(home, sid, asker=app.ask)
        app.open_chat(session, resumed=True)
        await until(pilot, lambda: isinstance(app.screen, ChatScreen) and app.chat is not None)
        await pilot.press("ctrl+n")
        await until(pilot, lambda: pane(app).display)
        assert any("still open" in x for x in rows(app)) and any("from before" in x for x in rows(app))


async def test_the_view_as_text_at_width_60(home, prov):
    async with running(home, size=(60, 24)) as (app, pilot):
        await opened(app, pilot)
        st = store(app)
        a = st.add_task("Write the report", "draft is in docs/")
        st.annotate(a.id, "keep it short")
        b = st.add_task("Book flights")
        st.cycle(b.id)
        st.cycle(b.id)
        f = st.add_folder("Website")
        c = st.add_task("Fix header", folder=f.id)
        st.cycle(c.id)
        st.cycle(c.id)
        st.add_note("Ideas", "Try a dark mode", folder=f.id)
        st.add_note("Shopping", "milk, bread")
        await seen(app, pilot, "Shopping")
        await pilot.press("ctrl+down", "enter")
        print("\n" + "\n".join(rows(app)))


async def test_notes_and_remarks_are_multi_line_with_save_and_cancel(home, prov):
    async with running(home, size=(140, 40)) as (app, pilot):
        await opened(app, pilot)
        await pilot.press("n")
        await type_in(pilot, "memo")
        assert pane(app).query_one(EditArea).display and isinstance(app.focused, EditArea)
        await write_in(pilot, "line one\nline two")
        n = store(app).items[0]
        assert (n.title, n.text) == ("memo", "line one\nline two")
        assert not pane(app).query_one(EditArea).display and isinstance(app.focused, TodoView)
        assert not any("line one" in x for x in rows(app))                       # folded: the title only
        await pilot.press("enter")                                               # unfolded: lines under the title
        plain = rows(app)
        i = next(k for k, x in enumerate(plain) if "memo" in x)
        assert "line one" in plain[i + 1] and "line two" in plain[i + 2]
        assert plain[i + 1].startswith("      ")
        await pilot.press("e")                                                   # edit: title, then the text
        await pilot.press("enter")
        pane(app).query_one(EditArea).select_all()
        await write_in(pilot, "new\ntext\nthree")
        assert store(app).items[0].text == "new\ntext\nthree"
        await pilot.press("e", "enter")
        pane(app).query_one(EditArea).select_all()
        await pilot.press("x", "x")
        await pilot.press("escape")                                              # cancel: nothing changes
        assert store(app).items[0].text == "new\ntext\nthree" and isinstance(app.focused, TodoView)
        assert not pane(app).query_one(EditArea).display
        t = store(app).add_task("job")
        await seen(app, pilot, "job")
        await pilot.press("ctrl+down", "ctrl+down")
        assert view(app).cur == t.id
        await pilot.press("a")
        await write_in(pilot, "first\nsecond")
        assert store(app).task(t.id).remarks[0].text == "first\nsecond"
        await pilot.press("enter")
        plain = rows(app)
        assert any("> " in x and "first" in x for x in plain) and any(x.strip() == "second" for x in plain)
