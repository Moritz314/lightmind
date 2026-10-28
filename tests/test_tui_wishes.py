"""Orbit TUI, four wishes after the first real use: copy by mouse, five context sizes, the slash popup, a neutral line."""
from contextlib import contextmanager

import pytest
from textual import events
from textual.widgets import Static

from lightmind import model as m
from lightmind.tui import chat as chatmod
from lightmind.tui.app import Orbit
from lightmind.tui.chat import ChatScreen, Msg
from lightmind.tui.screens import SetupScreen

from test_tui_support import FakeProv, edit, idle, install, open_chat, plain, quiet, running, say, until


@pytest.fixture
def prov(monkeypatch, home):
    quiet(home)
    return install(monkeypatch, FakeProv())


@pytest.fixture
def clip(monkeypatch):
    got = {"osc": [], "sys": []}
    monkeypatch.setattr(Orbit, "copy_to_clipboard", lambda self, text: got["osc"].append(text))
    monkeypatch.setattr(chatmod, "system_clip", lambda text: got["sys"].append(text) or True)
    return got


def notes(app):
    return list(app._notifications)


def popup(app) -> str:
    box = app.screen.query_one("#pop", Static)
    return box.content.plain if box.display else ""


async def drag(pilot, widget, a, b):
    await pilot.mouse_down(widget, offset=a)
    await pilot._post_mouse_events([events.MouseMove], widget, offset=b, button=1)       # moving with the button down
    await pilot.mouse_up(widget, offset=b)
    await pilot.pause(0.1)


# --------------------------------------------------------------------- 1 copy
async def test_selecting_transcript_text_with_the_mouse_copies_it_and_ctrl_c_copies_it_again(home, prov, clip):
    prov.turns = [[m.Text("Hello the user, copy me please"), m.Usage()]]
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await say(pilot, "hi there")
        await until(pilot, lambda: any("copy me" in w.plain for w in app.chat.query(Msg)))
        await idle(pilot, app)
        await pilot.pause(0.2)
        agent = next(w for w in app.chat.query(Msg) if "copy me" in w.plain)
        await drag(pilot, agent, (6, 0), (11, 0))                      # after "lm  > " (column 2): the word Hello
        assert [t.strip() for t in clip["osc"]] == ["Hello"] and [t.strip() for t in clip["sys"]] in ([], ["Hello"])      # copied on release (clip.exe: Windows only)
        assert any("copied" in n.message for n in notes(app))
        assert app.focused is app.chat.query_one("#inp")                  # a click never takes the input's focus
        clip["osc"].clear()
        await pilot.press("ctrl+c")                                       # the selection is still there
        assert [t.strip() for t in clip["osc"]] == ["Hello"]


async def test_ctrl_c_without_a_selection_copies_nothing_and_keeps_its_meaning(home, prov, clip):
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await pilot.press("ctrl+c")
        assert clip["osc"] == [] and not any("copied" in n.message for n in notes(app))
        assert any("Ctrl+Q" in n.message or "quit" in n.message.lower() for n in notes(app))     # the old hint
        assert "Shift+drag" not in plain(app, "#hint")                    # the key hint line is gone


# ---------------------------------------------------------------- 2 contexts
async def test_the_context_card_offers_five_sizes_and_the_chosen_one_reaches_the_session(home, prov):
    async with running(home) as (app, pilot):
        await pilot.press("n")
        await until(pilot, lambda: isinstance(app.screen, SetupScreen) and hasattr(app.screen, "S"))
        await pilot.press("enter", "enter", "enter", "enter")             # server, account, model, effort
        card = plain(app, "#card")
        assert [s for s in ("100k", "200k", "500k", "800k", "1m") if s in card] == ["100k", "200k", "500k", "800k", "1m"]
        assert card.count("1m window") == 3                                  # 500k, 800k, 1m
        await pilot.press("down")                                            # 200k -> 500k
        await pilot.press("s")
        await until(pilot, lambda: isinstance(app.screen, ChatScreen) and app.chat is not None)
        assert app.chat.s.info.ctx_limit == 500_000
        assert "500k" in app.chat.query_one("#head", Static).content.plain
        await say(pilot, "hello")
        await idle(pilot, app)
        assert prov.requests[0].context == 500_000                           # the provider sees it (claude_sdk adds [1m])


async def test_context_sizes_come_from_ui_context_sizes(home, prov):
    edit(home, "context_sizes: [100k, 200k, 500k, 800k, 1m]", "context_sizes: [50k, 300k]")
    async with running(home) as (app, pilot):
        await pilot.press("n")
        await until(pilot, lambda: isinstance(app.screen, SetupScreen) and hasattr(app.screen, "S"))
        await pilot.press("enter", "enter", "enter", "enter")
        card = plain(app, "#card")
        assert "50k" in card and "300k" in card and "500k" not in card and "200k" in card     # the default stays listed


def test_claude_sdk_asks_for_the_1m_variant_above_200k(home):
    from lightmind.providers import claude_sdk
    pytest.importorskip("claude_agent_sdk")
    cls = next(v for v in vars(claude_sdk).values() if isinstance(v, type) and hasattr(v, "_options") and v.__module__ == claude_sdk.__name__)
    p = cls.__new__(cls)
    p._sdk_tools = lambda: []
    p._prompt_file = lambda s: "x"
    p._env = lambda: {}
    p._stderr = []
    p.workdir, p.home = None, home
    p._pre_tool_use = None
    for ctx, model, want in ((500_000, "sonnet", "sonnet[1m]"), (200_000, "sonnet", "sonnet"), (1_000_000, "sonnet[1m]", "sonnet[1m]")):
        assert p._options(m.Request(system="s", model=model, context=ctx), "").model == want


# ------------------------------------------------------------------ 3 slash
async def test_typing_a_slash_opens_a_popup_that_filters_and_completes(home, prov):
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        assert popup(app) == ""
        assert "/mode" not in str(app.screen.query_one("#hint", Static).content)                            # no permanent command row any more
        await pilot.press("slash")
        pop = popup(app)
        for name in ("mode", "model", "account", "effort", "tools", "server", "accounts", "clear"):
            assert f"/{name}" in pop
        assert "start a new chat" in pop                                     # the ui.slash row `/clear -> new_chat` wins
        await pilot.press("e")
        pop = popup(app)
        assert "/effort" in pop and "/mode" not in pop and "/clear" not in pop
        await pilot.press("escape")
        assert popup(app) == "" and isinstance(app.screen, ChatScreen)       # Esc closes the popup only
        await pilot.press("backspace", "backspace")
        await pilot.press("slash", "m")
        assert "/mode" in popup(app) and "/model" in popup(app) and "/tools" not in popup(app)
        await pilot.press("down", "tab")                                     # mode -> model, Tab completes
        assert app.screen.query_one("#inp").value == "/model " and popup(app) == ""
        app.screen.query_one("#inp").value = ""
        await pilot.press("slash", "t", "enter")                             # Enter completes too
        assert app.screen.query_one("#inp").value == "/tools "
        await pilot.press("enter")                                           # and the next Enter runs it
        await until(pilot, lambda: type(app.screen).__name__ == "Pick")


async def test_a_typed_command_runs_on_enter_even_with_the_popup_open(home, prov):
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await pilot.press("slash", "c", "l", "e", "a", "r")
        assert "/clear" in popup(app)
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, SetupScreen))


# ------------------------------------------------------------- 4 neutral line
async def test_the_line_under_the_input_is_neutral_in_every_mode_and_the_mode_stays_in_the_status_text(home, prov):
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        st = app.screen.query_one("#status", Static)
        look = lambda: (st.styles.background, st.styles.border_top)          # noqa: E731
        auto = look()
        await say(pilot, "/mode bypass")
        await until(pilot, lambda: app.chat.s.info.mode == "bypass")
        await pilot.pause(0.1)
        assert "Bypass" in plain(app, "#status") and look() == auto
        assert any(s.style for s in st.content.spans)                        # the mode word keeps its colour in the text


# ------------------------------------------------------- login and key from the UI
FAKE_KEY = "sk-ant-FAKE-not-a-real-key-123"


@pytest.fixture
def logins(monkeypatch):
    """`claude_sdk.login` replaced (the real one opens a browser) and suspend() a no-op: the headless driver cannot suspend."""
    from lightmind.providers import claude_sdk
    calls = []
    monkeypatch.setattr(claude_sdk, "login", lambda home, account: calls.append(account) or 0)

    @contextmanager
    def fake_suspend(self):
        yield
    monkeypatch.setattr(Orbit, "suspend", fake_suspend)
    return calls


def add_api_account(home):
    body = ["---", "type: anthropic-api", "secret: anthropic/api-9", "default_model: sonnet", "---", "# api", ""]
    (home.root / "providers" / "anthropic-api.md").write_text(chr(10).join(body), "utf-8")


async def test_the_accounts_view_logs_in_the_marked_account_with_l(home, prov, logins):
    async with running(home) as (app, pilot):
        await pilot.press("a")
        await until(pilot, lambda: type(app.screen).__name__ == "AccountsScreen" and app.screen.rows)
        assert "not logged in" in plain(app, "#acc") and "press L to log in" in plain(app, "#acc")
        await pilot.press("l")
        await pilot.pause(0.2)
        assert logins == ["claude-abo"]
        assert any("login claude-abo: done" in n.message for n in notes(app))


async def test_the_account_card_and_slash_login_use_the_same_login(home, prov, logins):
    async with running(home) as (app, pilot):
        await pilot.press("n")
        await until(pilot, lambda: isinstance(app.screen, SetupScreen) and hasattr(app.screen, "S"))
        await pilot.press("enter")                                   # server -> account card
        assert "L log in" in plain(app, "#card")
        await pilot.press("l")
        assert logins == ["claude-abo"]
        await pilot.press("s")
        await until(pilot, lambda: isinstance(app.screen, ChatScreen) and app.chat is not None)
        await say(pilot, "/login")                                   # the account of this chat
        await until(pilot, lambda: len(logins) == 2)
        await say(pilot, "/login nobody")
        await until(pilot, lambda: any("login: unknown account" in t for t in [w.plain for w in app.chat.query(Msg)]))


async def test_the_key_dialog_stores_the_key_and_never_shows_it(home, prov):
    from textual.widgets import Input
    add_api_account(home)
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await say(pilot, "/key anthropic-api")
        await until(pilot, lambda: type(app.screen).__name__ == "KeyModal")
        inp = app.screen.query_one(Input)
        assert inp.password is True
        inp.value = FAKE_KEY
        assert FAKE_KEY not in str(app.export_screenshot())          # a password field draws dots
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, ChatScreen))
        env = (home.root / "secrets" / "anthropic" / "api-9.env").read_text("utf-8")
        assert f"ANTHROPIC_API_KEY={FAKE_KEY}" in env
        assert any("stored" in n.message for n in notes(app)) and not any(FAKE_KEY in n.message for n in notes(app))
        assert not any(FAKE_KEY in w.plain for w in app.chat.query(Msg))
        await say(pilot, "/key " + FAKE_KEY)                         # a key pasted into the command line is not echoed
        await until(pilot, lambda: any("key: unknown account" in w.plain for w in app.chat.query(Msg)))
        assert not any(FAKE_KEY in w.plain for w in app.chat.query(Msg))


async def test_k_in_the_accounts_view_opens_the_key_dialog_for_a_key_missing_account(home, prov):
    add_api_account(home)
    async with running(home) as (app, pilot):
        await pilot.press("a")
        await until(pilot, lambda: type(app.screen).__name__ == "AccountsScreen" and app.screen.rows)
        assert "key missing" in plain(app, "#acc")
        while app.screen.sel != "anthropic-api":
            await pilot.press("down")
        await pilot.press("k")
        await until(pilot, lambda: type(app.screen).__name__ == "KeyModal")
        await pilot.press("escape")                                  # cancel: nothing stored
        await until(pilot, lambda: type(app.screen).__name__ == "AccountsScreen")
        assert not (home.root / "secrets" / "anthropic" / "api-9.env").exists()


async def test_login_and_key_are_in_the_slash_suggestions(home, prov):
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await pilot.press("slash", "l", "o")                         # the popup shows 12 rows: filter, as typing does
        assert "/login" in popup(app)
        for _ in range(3):
            await pilot.press("backspace")
        await pilot.press("slash", "k", "e")
        assert "/key" in popup(app)
