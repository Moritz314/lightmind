"""Remote Control in the Orbit chat: /rc, the mirror of a turn, what a device may send, questions on both sides, archive from
the phone.  Textual Pilot, the scripted provider, a test client on the link socket; no network."""
import asyncio
import json

import pytest
from lightmind.tui.chat import ChatScreen, Msg
from lightmind.tui.screens import AskModal

from lightmind import core, loop, rc, runs, settings
from lightmind import model as m
from lightmind.files import Home
from test_rc_link import Client, connect, meta_of
from test_tui_support import FakeProv, idle, install, open_chat, pause_forever, quiet, running, say, texts, until


@pytest.fixture
def prov(monkeypatch, home):
    quiet(home)
    return install(monkeypatch, FakeProv())


@pytest.fixture
def on(home) -> Home:
    """The same tree with `remote: enabled: true` and `auto: false` in config.md: the link opens only by /rc."""
    cfg = home.root / "config.md"
    cfg.write_bytes(settings.edit_text(cfg.read_bytes().decode("utf-8"),
                                       {("remote", "enabled"): True, ("remote", "auto"): False}).encode("utf-8"))
    return Home(home.root)


def has(app, needle: str) -> bool:
    return any(needle in t for t in texts(app))


def status_line(app) -> str:
    return app.screen.query_one("#status").content.plain


async def start_rc(app, pilot, name="test") -> Client:
    """`/rc <name>` typed in the chat; returns a connected client that has read the hello."""
    await say(pilot, f"/rc {name}")
    await until(pilot, lambda: app.chat is not None and app.chat.rc is not None)
    c = await connect(app.home, app.chat.s.id)
    assert (await c.read())["type"] == "link.hello"
    return c


def gated(gate: asyncio.Event):
    async def turn(self, req):
        yield m.Text("first answer")
        await gate.wait()
        yield m.Usage()
    return turn


# ------------------------------------------------------------------------------------------ /rc
async def test_rc_with_a_name_opens_the_link_files_and_says_so(home, on, prov):
    async with running(on) as (app, pilot):
        await open_chat(app, pilot)
        assert " · RC" not in status_line(app)
        c = await start_rc(app, pilot, "my chat")
        meta = meta_of(on, app.chat.s.id)
        assert meta["name"] == "my chat" and meta["origin"] == "tui" and meta["pid"] > 0
        assert has(app, "Remote Control on: my chat") and " · RC" in status_line(app)
        await say(pilot, "/rc other")
        await until(pilot, lambda: has(app, "already on as my chat"))
        assert app.chat.rc.name == "my chat"
        c.close()


async def test_rc_without_a_name_takes_the_title_else_the_short_id_and_remote_control_is_the_same_command(home, on, prov):
    async with running(on) as (app, pilot):
        await open_chat(app, pilot)
        await say(pilot, "/remote-control")
        await until(pilot, lambda: app.chat.rc is not None)
        from lightmind.tui.data import short_id
        assert app.chat.rc.name == short_id(app.chat.s.id)
        await say(pilot, "/rc off")
        await until(pilot, lambda: app.chat.rc is None)
        app.chat.s.rename("Mein Titel")
        await say(pilot, "/rc")
        await until(pilot, lambda: app.chat.rc is not None)
        assert app.chat.rc.name == "Mein Titel"


async def test_rc_says_off_in_config_md_and_opens_nothing_when_remote_is_not_enabled(home, prov):
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await say(pilot, "/rc test")
        await until(pilot, lambda: has(app, "off in config.md"))
        assert app.chat.rc is None and not rc.links_dir(home).exists()


async def test_rc_off_and_leaving_the_chat_remove_the_files_and_give_the_session_its_asker_back(home, on, prov):
    async with running(on) as (app, pilot):
        await open_chat(app, pilot)
        s = app.chat.s
        before = (s.asker, s.questioner)
        assert before[0] == app.ask and before[1] is None
        c = await start_rc(app, pilot)
        assert s.asker != before[0] and s.questioner is not None            # wrapped while the link is on
        d = rc.links_dir(on)
        assert (d / f"{s.id}.json").exists()
        await say(pilot, "/rc off")
        await until(pilot, lambda: app.chat.rc is None)
        assert not (d / f"{s.id}.json").exists() and (s.asker, s.questioner) == before
        assert " · RC" not in status_line(app)
        await c.until(lambda x: x["type"] == "link.closed")
        c2 = await start_rc(app, pilot, "again")                             # and it can be turned on again
        await pilot.press("escape")                                          # leaving the chat ends the link
        await until(pilot, lambda: not (d / f"{s.id}.json").exists())
        c2.close()
        c.close()


async def test_no_gateway_in_time_says_so_and_sends_the_sentence_once_but_a_connected_node_stops_that(home, on, prov, monkeypatch):
    monkeypatch.setattr(rc, "NODE_WAIT", 0.4)
    sent = []
    from lightmind import watch
    monkeypatch.setattr(watch, "notify", lambda h, text, key="": sent.append((text, key)) or True)
    async with running(on) as (app, pilot):
        await open_chat(app, pilot)
        await say(pilot, "/rc Alpha")
        await until(pilot, lambda: has(app, "no gateway is connected"), timeout=5)
        assert sent == [("Remote Control wurde für Sitzung Alpha aktiviert", f"rc-{app.chat.s.id}")]
        await say(pilot, "/rc off")
        await until(pilot, lambda: app.chat.rc is None)
        sent.clear()
        await say(pilot, "/rc Beta")
        await until(pilot, lambda: app.chat.rc is not None)
        c = await connect(on, app.chat.s.id)                                  # the node connects in time
        await c.read()
        await pilot.pause(1.0)
        assert sent == []
        assert sum("no gateway is connected" in t for t in texts(app)) == 1
        c.close()


# ------------------------------------------------------------------------------------------ the mirror
async def test_a_turn_typed_here_is_mirrored_in_order_with_user_line_busy_and_the_end(home, on, prov):
    prov.turns = [[m.Text("answer from the fake"), m.Usage(10, 5)]]
    async with running(on) as (app, pilot):
        await open_chat(app, pilot)
        c = await start_rc(app, pilot)
        await say(pilot, "hello agent")
        end = await c.until(lambda x: x["type"] == "turn.end")
        types = [x["type"] for x in c.seen[1:]]
        order = [types.index(t) for t in ("text", "busy", "turn.start", "turn.end")]
        assert order == sorted(order) and types[0] == "text"
        user = c.seen[1]
        assert user["role"] == "user" and user["text"] == "hello agent" and user["from"] == "tui"
        text = next(x for x in c.seen if x["type"] == "text" and x.get("role") != "user")
        assert text["text"] == "answer from the fake" and text["turn"] == "t1"
        assert end["status"] == "done" and end["turn"] == "t1"
        busy_off = await c.until(lambda x: x["type"] == "busy" and x["busy"] is False)
        seqs = [x["seq"] for x in c.seen[1:]]
        assert seqs == sorted(set(seqs)) and busy_off["queued"] == 0
        c.close()


async def test_a_rename_is_mirrored_as_session_title(home, on, prov):
    async with running(on) as (app, pilot):
        await open_chat(app, pilot)
        c = await start_rc(app, pilot)
        await say(pilot, "/rename Neuer Name")
        t = await c.until(lambda x: x["type"] == "session.title")
        assert t["title"] == "Neuer Name"
        c.close()


async def test_a_message_from_a_device_at_rest_starts_a_turn_and_shows_as_you_and_the_device(home, on, prov):
    prov.turns = [[m.Text("pong"), m.Usage()]]
    async with running(on) as (app, pilot):
        await open_chat(app, pilot)
        c = await start_rc(app, pilot)
        r = await c.call("send", text="from my phone", **{"from": "Pixel"})
        assert r["ok"] and r["data"] == {}
        await until(pilot, lambda: len(prov.requests) == 1)
        assert "from my phone" in prov.requests[0].messages[-1].text
        await idle(pilot, app)
        who = {w.text: w.speaker for w in app.screen.query(Msg) if w.kind == "user"}
        assert who["from my phone"] == "You · Pixel"
        mirrored = [x for x in c.seen if x["type"] == "text" and x.get("role") == "user"]
        assert [(x["text"], x["from"]) for x in mirrored] == [("from my phone", "Pixel")]
        c.close()


async def test_during_a_turn_messages_become_interjections_and_the_fourth_waits_in_line(home, on, prov):
    gate = asyncio.Event()
    prov.turns = [gated(gate)]
    async with running(on) as (app, pilot):
        await open_chat(app, pilot)
        c = await start_rc(app, pilot)
        await say(pilot, "start")
        await until(pilot, lambda: has(app, "first answer") and app.chat.busy)
        got = []
        for i in range(4):
            got.append(await c.call("send", f"s{i}", text=f"extra {i}"))
        assert [g["data"]["state"] for g in got] == ["pending", "pending", "pending", "queued"]
        assert got[3]["data"]["interject"].startswith("q")
        pend = [x for x in c.seen if x["type"] == "interject" and x["state"] == "pending"]
        assert [x["text"] for x in pend] == ["extra 0", "extra 1", "extra 2"]
        assert any(x["type"] == "busy" and x["queued"] == 1 for x in c.seen)
        assert "1 queued" in status_line(app)
        gate.set()
        await c.until(lambda x: x["type"] == "interject" and x["state"] == "sent" and x["id"] == got[3]["data"]["interject"], timeout=10)
        await idle(pilot, app)
        await until(pilot, lambda: not app.chat.queue)
        c.close()


@pytest.mark.parametrize("line", ["/settings", "/config", "/login", "/key", "/accounts", "/resume", "/server", "/clear", "/new",
                                  "/quit", "/exit", "/upload", "/datei", "/rc off", "/rc x", "/remote-control",
                                  "hi /settings", "/model sonnet /quit"])
async def test_what_needs_a_screen_or_leaves_the_chat_is_refused_from_a_device(home, on, prov, line):
    async with running(on) as (app, pilot):
        await open_chat(app, pilot)
        c = await start_rc(app, pilot)
        r = await c.call("send", text=line)
        assert r["ok"] is False and "not available from a device" in r["error"]
        assert prov.requests == [] and app.chat.rc is not None
        c.close()


async def test_the_same_commands_are_fine_at_the_keyboard_and_a_setting_command_is_fine_from_a_device(home, on, prov):
    async with running(on) as (app, pilot):
        await open_chat(app, pilot)
        c = await start_rc(app, pilot)
        r = await c.call("send", text="/effort low")
        assert r["ok"] and app.chat.s.effort == "low"
        st = await c.until(lambda x: x["type"] == "status")
        assert st["effort"] == "low"
        r = await c.call("send", text="/effort nonsense", rid="r2")
        assert r["ok"] is False and "unknown" in r["error"]
        await say(pilot, "/settings")                                         # the keyboard may
        from lightmind.tui.settings import SettingsScreen
        await until(pilot, lambda: isinstance(app.screen, SettingsScreen))
        c.close()


async def test_session_set_changes_the_session_and_sends_status_and_a_bad_value_is_refused(home, on, prov):
    async with running(on) as (app, pilot):
        await open_chat(app, pilot)
        s = app.chat.s
        c = await start_rc(app, pilot)
        r = await c.call("session.set", effort="high", **{"from": "Pixel"})
        assert r["ok"] and r["data"]["status"]["effort"] == "high" and s.effort == "high"
        st = await c.until(lambda x: x["type"] == "status")
        assert st["effort"] == "high" and "effort" in st and "model" in st
        assert s.info.effort == "high"                      # the default status line has no effort field, so check what feeds it
        bad = await c.call("session.set", "r2", effort="turbo")
        assert bad["ok"] is False and s.effort == "high"
        none = await c.call("session.set", "r3")
        assert none["ok"] is False
        c.close()


async def test_cancel_stops_the_turn_and_ends_it_as_cancelled(home, on, prov):
    prov.turns = [pause_forever]
    async with running(on) as (app, pilot):
        await open_chat(app, pilot)
        c = await start_rc(app, pilot)
        await say(pilot, "go")
        await until(pilot, lambda: has(app, "thinking about it") and app.chat.busy)
        assert (await c.call("cancel"))["ok"]
        end = await c.until(lambda x: x["type"] == "turn.end", timeout=10)
        assert end["status"] == "cancelled"
        await idle(pilot, app)
        assert (await c.call("cancel", "again"))["ok"]                         # nothing runs: still fine
        c.close()


async def test_garbage_requests_to_a_chat_do_not_hurt_it(home, on, prov):
    async with running(on) as (app, pilot):
        await open_chat(app, pilot)
        c = await start_rc(app, pilot)
        for bad in ({"text": ""}, {"text": 5}, {}, {"text": "   "}):
            assert (await c.call("send", **bad))["ok"] is False
        assert (await c.call("session.nonsense"))["ok"] is False
        assert prov.requests == [] and app.chat.rc is not None
        c.close()


# ------------------------------------------------------------------------------------------ questions
async def test_a_permission_question_goes_to_the_phone_and_its_answer_closes_the_modal_and_reaches_the_rule(home, on, prov):
    async def writes(self, req):
        r = await self.dispatcher.call("write", {"path": "providers/extra.md", "content": "x\n"})
        yield m.Text("write ok" if r.ok else "write refused")
        yield m.Usage()
    prov.turns = [writes]
    target = home.root / "providers" / "extra.md"
    async with running(on) as (app, pilot):
        await open_chat(app, pilot)
        c = await start_rc(app, pilot)
        await say(pilot, "make a provider")
        await until(pilot, lambda: isinstance(app.screen, AskModal))
        q = await c.until(lambda x: x["type"] == "question")
        assert q["kind"] == "permission" and q["choices"] == ["yes", "always", "no"] and "extra.md" in q["text"]
        assert not target.exists()
        r = await c.call("answer", question=q["question"], choice="yes", **{"from": "Pixel"})
        assert r["ok"]
        await until(pilot, lambda: has(app, "write ok"))
        assert target.read_text() == "x\n" and isinstance(app.screen, ChatScreen)
        closed = await c.until(lambda x: x["type"] == "question.closed")
        assert (closed["choice"], closed["by"]) == ("yes", "Pixel")
        late = await c.call("answer", "r2", question=q["question"], choice="no")
        assert late["ok"] is False and late["error"] == "already answered"
        c.close()


async def test_a_permission_question_answered_at_the_keyboard_tells_the_phone_by_tui(home, on, prov):
    async def writes(self, req):
        r = await self.dispatcher.call("write", {"path": "providers/extra.md", "content": "x\n"})
        yield m.Text("write ok" if r.ok else "write refused")
        yield m.Usage()
    prov.turns = [writes]
    async with running(on) as (app, pilot):
        await open_chat(app, pilot)
        c = await start_rc(app, pilot)
        await say(pilot, "make a provider")
        await until(pilot, lambda: isinstance(app.screen, AskModal))
        q = await c.until(lambda x: x["type"] == "question")
        await pilot.pause(0.6)
        await pilot.press("n", "enter")
        await until(pilot, lambda: has(app, "write refused"))
        closed = await c.until(lambda x: x["type"] == "question.closed")
        assert (closed["question"], closed["choice"], closed["by"]) == (q["question"], "no", "tui")
        assert (await c.call("answer", question=q["question"], choice="yes"))["error"] == "already answered"
        c.close()


async def test_without_the_link_the_permission_modal_works_as_before_and_nothing_is_wrapped(home, on, prov):
    async with running(on) as (app, pilot):
        await open_chat(app, pilot)
        task = asyncio.create_task(app.chat.s.asker("Allow x?", None))
        await until(pilot, lambda: isinstance(app.screen, AskModal))
        await pilot.pause(0.6)
        await pilot.press("y", "enter")
        assert await task == "yes"


async def test_the_ask_tool_gets_a_text_modal_and_the_phone_may_answer_first(home, on, prov):
    from lightmind.tui.chat import TextAskModal
    async with running(on) as (app, pilot):
        await open_chat(app, pilot)
        c = await start_rc(app, pilot)
        s = app.chat.s
        task = asyncio.create_task(s.questioner("Which color?"))
        await until(pilot, lambda: isinstance(app.screen, TextAskModal))
        q = await c.until(lambda x: x["type"] == "question")
        assert q["kind"] == "ask" and q["text"] == "Which color?" and q["choices"] == []
        assert (await c.call("answer", question=q["question"], choice="green, please"))["ok"]
        assert await task == "green, please"
        await until(pilot, lambda: isinstance(app.screen, ChatScreen))
        task = asyncio.create_task(s.questioner("And the size?"))             # this time the keyboard answers
        await until(pilot, lambda: isinstance(app.screen, TextAskModal))
        q2 = await c.until(lambda x: x["type"] == "question" and x["text"] == "And the size?")
        await pilot.press(*"big", "enter")
        assert await task == "big"
        closed = await c.until(lambda x: x["type"] == "question.closed" and x["question"] == q2["question"])
        assert (closed["choice"], closed["by"]) == ("big", "tui")
        c.close()


async def test_a_stopped_turn_takes_its_question_down(home, on, prov):
    async def asks(self, req):
        r = await self.dispatcher.call("write", {"path": "providers/extra.md", "content": "x\n"})
        yield m.Text("done" if r.ok else "refused")
        yield m.Usage()
    prov.turns = [asks]
    async with running(on) as (app, pilot):
        await open_chat(app, pilot)
        c = await start_rc(app, pilot)
        await say(pilot, "write it")
        await until(pilot, lambda: isinstance(app.screen, AskModal))
        q = await c.until(lambda x: x["type"] == "question")
        assert (await c.call("cancel"))["ok"]
        closed = await c.until(lambda x: x["type"] == "question.closed")
        assert closed["question"] == q["question"] and closed["choice"] == "cancelled"
        await until(pilot, lambda: isinstance(app.screen, ChatScreen))
        c.close()


# ------------------------------------------------------------------------------------------ archive
async def test_session_archive_from_the_phone_answers_then_closes_marks_and_goes_back(home, on, prov):
    async with running(on) as (app, pilot):
        await open_chat(app, pilot)
        s = app.chat.s
        c = await start_rc(app, pilot)
        r = await c.call("session.archive")
        assert r["ok"]
        await until(pilot, lambda: runs.is_archived(on.path("runs") / s.id))
        await until(pilot, lambda: app.chat is None)
        marker = json.loads((on.path("runs") / s.id / "archived.json").read_text("utf-8"))
        assert marker["by"] == "phone" and marker["status"] in ("running", "paused", "done")
        assert s.closed and not (rc.links_dir(on) / f"{s.id}.json").exists()
        assert await c.closed()


async def test_slash_archive_at_the_keyboard_and_the_word_archive_from_a_device_do_the_same(home, on, prov):
    async with running(on) as (app, pilot):
        await open_chat(app, pilot)
        s = app.chat.s
        await say(pilot, "/archive")
        await until(pilot, lambda: runs.is_archived(on.path("runs") / s.id))
        assert json.loads((on.path("runs") / s.id / "archived.json").read_text("utf-8"))["by"] == "tui"
        await until(pilot, lambda: app.chat is None)
    async with running(on) as (app, pilot):
        await open_chat(app, pilot)
        s2 = app.chat.s
        c = await start_rc(app, pilot)
        assert (await c.call("send", text="/archive"))["ok"]
        await until(pilot, lambda: runs.is_archived(on.path("runs") / s2.id))
        c.close()


async def test_archive_stops_a_running_turn_first(home, on, prov):
    prov.turns = [pause_forever]
    async with running(on) as (app, pilot):
        await open_chat(app, pilot)
        s = app.chat.s
        await say(pilot, "go")
        await until(pilot, lambda: app.chat.busy)
        await say(pilot, "/archive")
        await until(pilot, lambda: runs.is_archived(on.path("runs") / s.id), timeout=15)
        assert s.closed and prov.closed >= 1


async def test_the_deny_list_matches_what_the_plan_names(home):
    assert set(core.RC_DENY) >= {"upload", "datei", "settings", "config", "login", "key", "accounts", "resume", "server",
                                 "clear", "new", "quit", "exit"}
    assert "archive" not in core.RC_DENY and {"rc", "remote-control", "archive"} <= {n for n, _ in core.BUILTIN_SLASH}


# ------------------------------------------------------------------------------------------ remote.auto
def with_remote(home: Home, **keys) -> Home:
    """The same tree with `remote: <key>: <value>` set in config.md."""
    cfg = home.root / "config.md"
    text = cfg.read_bytes().decode("utf-8")
    cfg.write_bytes(settings.edit_text(text, {("remote", k): v for k, v in keys.items()}).encode("utf-8"))
    return Home(home.root)


def test_the_auto_field_is_a_yes_no_choice_that_defaults_to_yes_with_enabled_and_is_read_like_enabled(home):
    field = next(f for f in settings.GENERAL if f.path == ("remote", "auto"))
    assert field.kind == "choice" and field.unset == "yes" and "Default: yes" in field.help
    assert not rc.auto_enabled(home) and not rc.auto_enabled(with_remote(home, auto=True))      # auto alone is not enough
    for value in (True, "yes"):
        assert rc.auto_enabled(with_remote(home, enabled=True, auto=value))
    assert not rc.auto_enabled(with_remote(home, enabled=True, auto=False))
    assert not rc.auto_enabled(with_remote(home, enabled=False, auto=True))
    cfg = home.root / "config.md"                                                              # unset: on with enabled
    cfg.write_bytes(cfg.read_bytes().replace(b"  auto: false", b"").replace(b"  auto: true", b"").replace(b"enabled: false", b"enabled: true"))
    assert rc.auto_enabled(Home(home.root))


async def test_auto_opens_the_link_by_itself_marked_auto_and_named_like_a_bare_rc(home, prov):
    tree = with_remote(home, enabled=True, auto=True)
    async with running(tree) as (app, pilot):
        await open_chat(app, pilot)
        await until(pilot, lambda: app.chat.rc is not None)
        link = app.chat.rc
        from lightmind.tui.data import short_id
        assert link.auto and link.name == short_id(app.chat.s.id)
        assert meta_of(tree, app.chat.s.id)["auto"] is True
        c = await connect(tree, app.chat.s.id)
        assert (await c.read())["auto"] is True                                    # the hello tells the node: no push
        c.close()


async def test_a_hand_typed_rc_is_not_marked_auto(home, on, prov):
    async with running(on) as (app, pilot):
        await open_chat(app, pilot)
        c = await start_rc(app, pilot)
        assert not app.chat.rc.auto and meta_of(on, app.chat.s.id)["auto"] is False
        c.close()


@pytest.mark.parametrize("keys", [{"enabled": True, "auto": False}, {"enabled": False, "auto": True}, {}])
async def test_without_enabled_and_auto_no_link_opens(home, prov, keys):
    async with running(with_remote(home, **keys) if keys else home) as (app, pilot):
        await open_chat(app, pilot)
        await pilot.pause(0.5)
        assert app.chat.rc is None and not rc.links_dir(home).exists()


async def test_auto_leaves_a_read_only_chat_alone(home, prov, monkeypatch):
    tree = with_remote(home, enabled=True, auto=True)
    async with running(tree) as (app, pilot):
        monkeypatch.setattr(loop.Session, "readonly", property(lambda self: True), raising=False)
        await open_chat(app, pilot)
        await pilot.pause(0.5)
        assert app.chat.s.readonly and app.chat.rc is None and not rc.links_dir(tree).exists()


async def test_rc_off_ends_an_auto_link_and_the_chat_stays_without_one(home, prov):
    tree = with_remote(home, enabled=True, auto=True)
    async with running(tree) as (app, pilot):
        await open_chat(app, pilot)
        await until(pilot, lambda: app.chat.rc is not None)
        sid = app.chat.s.id
        await say(pilot, "/rc off")
        await until(pilot, lambda: app.chat.rc is None)
        await pilot.pause(0.3)
        assert app.chat.rc is None and not (rc.links_dir(tree) / f"{sid}.json").exists()


async def test_a_failing_auto_start_is_a_quiet_note_and_never_an_error(home, prov, monkeypatch):
    tree = with_remote(home, enabled=True, auto=True)

    async def broken(*a, **k):
        raise OSError("no socket today")
    monkeypatch.setattr(rc.Link, "start", broken)
    async with running(tree) as (app, pilot):
        await open_chat(app, pilot)
        await until(pilot, lambda: has(app, "did not start by itself"))
        assert app.chat.rc is None and not app.chat.bad


async def test_the_no_gateway_fallback_is_silent_for_auto_and_still_sent_for_a_hand_typed_rc(home, prov, monkeypatch):
    from lightmind import watch
    sent = []
    monkeypatch.setattr(watch, "notify", lambda *a, **k: sent.append(a))
    tree = with_remote(home, enabled=True, auto=True)
    async with running(tree) as (app, pilot):
        await open_chat(app, pilot)
        await until(pilot, lambda: app.chat.rc is not None)
        await app.chat.rc_fallback(app.chat.rc)
        assert not sent and not has(app, "no gateway is connected")
        await say(pilot, "/rc off")
        await until(pilot, lambda: app.chat.rc is None)
        await say(pilot, "/rc")
        await until(pilot, lambda: app.chat.rc is not None)
        await app.chat.rc_fallback(app.chat.rc)
        assert len(sent) == 1 and has(app, "no gateway is connected")
