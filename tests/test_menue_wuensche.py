"""Three wishes of the start menu: a quick chat is named from its content like every chat, `lmr` / `lm resume` shows the
resume menu of the start menu, and the new-chat cards can start an agent.  Orbit runs in a Textual pilot with a scripted
provider; the naming model is replaced by a function (no network, no model)."""
import json

import pytest

from lightmind import cli, launch, resume, runs, titles
from lightmind.tui.app import Orbit
from lightmind.tui.chat import ChatScreen
from lightmind.tui.screens import MapScreen, ResumeScreen, SetupScreen

from test_tui_support import FakeProv, earlier_chat, idle, install, plain, quiet, running, say, until


@pytest.fixture
def prov(monkeypatch, home):
    quiet(home)
    return install(monkeypatch, FakeProv())


def make_agent(home, name, description="", **frontmatter):
    """agents/<name>/agentMaster.md with the given frontmatter keys."""
    d = home.root / "agents" / name
    d.mkdir(parents=True, exist_ok=True)
    keys = "".join(f"{k}: {v}\n" for k, v in frontmatter.items())
    (d / "agentMaster.md").write_text(f"---\ndescription: {description}\n{keys}---\nYou are {name}.\n", "utf-8")


def meta(home, sid):
    return json.loads((home.path("runs") / sid / "meta.json").read_text("utf-8"))


# ------------------------------------------------------------------ 1. quick chats are named like all chats
async def test_a_quick_chat_gets_title_and_description_from_its_content_and_no_session_name(home, prov, monkeypatch):
    monkeypatch.setattr(titles, "ask_model", lambda *a: json.dumps({"title": "Milch kaufen", "topics": ["Milch", "Einkauf"]}))
    spec = cli._quick_spec(home, cli.build_parser().parse_args(["q"]))        # exactly what `lm q` hands to Orbit
    assert "name" not in spec["new"]
    app = Orbit(home, anim=False, direct=spec)
    async with app.run_test(size=(130, 42)) as pilot:
        await until(pilot, lambda: app.chat is not None)
        sid = app.chat.s.id
        await say(pilot, "wir müssen Milch kaufen")
        await idle(pilot, app)
        await until(pilot, lambda: meta(home, sid).get("title") == "Milch kaufen")      # the naming thread of every chat
        assert meta(home, sid)["description"] == "Milch · Einkauf" and not meta(home, sid).get("title_manual")
        assert runs.session_names(home).get(sid) is None                       # no "quick-HHMM" in front of the title
        assert [r.name for r in resume.rows(home) if r.id == sid] == ["Milch kaufen"]


async def test_a_name_given_with_n_is_still_kept(home, prov):
    spec = cli._quick_spec(home, cli.build_parser().parse_args(["q", "-n", "mein-chat"]))
    app = Orbit(home, anim=False, direct=spec)
    async with app.run_test(size=(130, 42)) as pilot:
        await until(pilot, lambda: app.chat is not None)
        assert runs.session_names(home).get(app.chat.s.id) == "mein-chat"


# ------------------------------------------------------------------ 2. lmr = the resume menu of the start menu
async def test_lmr_shows_the_same_resume_screen_as_the_start_menu(home, prov):
    sid = await earlier_chat(home, prov)
    runs.set_name_on_disk(home, sid, "title", "Milch kaufen")
    async with running(home) as (app, pilot):                                  # the menu route: Resume chat
        await until(pilot, lambda: isinstance(app.screen, MapScreen))
        await pilot.press("r")
        await until(pilot, lambda: isinstance(app.screen, ResumeScreen) and app.screen.rows)
        via_menu = plain(app, "#list")
    app = Orbit(home, anim=False, direct={"screen": "resume", "label": "lm resume"})
    async with app.run_test(size=(130, 42)) as pilot:                          # the lmr route
        await until(pilot, lambda: isinstance(app.screen, ResumeScreen) and app.screen.rows)
        assert type(app.screen) is ResumeScreen and len(app.screen_stack) == 2    # no start menu below it
        assert not any(isinstance(s, MapScreen) for s in app.screen_stack)
        via_lmr = plain(app, "#list")
    assert "Milch kaufen" in via_menu and "Milch kaufen" in via_lmr
    assert via_menu.splitlines()[0] == via_lmr.splitlines()[0] and via_menu.splitlines()[-1] == via_lmr.splitlines()[-1]


async def test_lmr_opens_the_picked_chat_in_this_window_and_esc_leaves(home, prov, monkeypatch):
    launched = []
    monkeypatch.setattr(launch, "spawn", lambda plan, tree, *a, **k: launched.append(plan) or (True, ""))
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,1,0")                    # a terminal that could open a tab
    monkeypatch.delenv("WT_SESSION", raising=False)
    sid = await earlier_chat(home, prov)
    app = Orbit(home, anim=False, direct={"screen": "resume", "label": "lm resume"})
    async with app.run_test(size=(130, 42)) as pilot:
        await until(pilot, lambda: isinstance(app.screen, ResumeScreen) and app.screen.rows)
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, ChatScreen) and app.chat is not None)
        assert app.chat.s.id == sid and app.chat.s.resumed and launched == []     # `sessions.open: tab` does not apply here
    app = Orbit(home, anim=False, direct={"screen": "resume", "label": "lm resume"})
    async with app.run_test(size=(130, 42)) as pilot:
        await until(pilot, lambda: isinstance(app.screen, ResumeScreen) and app.screen.rows)
        await pilot.press("escape")
        await until(pilot, lambda: app.return_code is not None)
    assert app.return_code == 0


# ------------------------------------------------------------------ 3. New chat with an agent
async def start_setup(app, pilot):
    await pilot.press("n")
    await until(pilot, lambda: isinstance(app.screen, SetupScreen) and hasattr(app.screen, "S"))
    return app.screen


async def pick_agent(pilot, setup, name):
    """A on the cards, down to `name`, Enter."""
    await pilot.press("a")
    await until(pilot, lambda: setup.picking)
    rows = ["", *(n for n, _d, _ok in setup.agents)]
    for _ in range((rows.index(name) - setup.cur["agent"]) % len(rows)):             # the cursor starts on the chosen agent
        await pilot.press("down")
    await pilot.press("enter")
    await until(pilot, lambda: not setup.picking)


async def test_the_agent_card_lists_the_agents_and_the_chat_starts_as_the_chosen_one(home, prov):
    make_agent(home, "scout", "finds things in the tree", model="haiku", effort="low", mode="plan")
    make_agent(home, "broken", "cannot be read", context_limit="lots")
    async with running(home) as (app, pilot):
        setup = await start_setup(app, pilot)
        assert "A agent" in plain(app, "#card")
        await pilot.press("a")
        await until(pilot, lambda: setup.picking)
        card = plain(app, "#card")
        assert "(no agent)" in card and "scout" in card and "finds things in the tree" in card and "broken" in card
        await pilot.press("escape")                                             # Esc: back to the cards, the card stays
        await until(pilot, lambda: not setup.picking)
        assert isinstance(app.screen, SetupScreen) and setup.S["agent"] == ""
        await pilot.press("a")
        await until(pilot, lambda: setup.picking)
        for _ in range([n for n, _d, _ok in setup.agents].index("broken") + 1):
            await pilot.press("down")
        await pilot.press("enter")                                              # an unreadable agent cannot be taken
        await until(pilot, lambda: "not available" in plain(app, "#card"))
        assert setup.picking and setup.S["agent"] == ""
        await pilot.press("escape")
        await pick_agent(pilot, setup, "scout")
        assert setup.S["agent"] == "scout" and "agent scout" in plain(app, "#card")
        assert (setup.S["model"], setup.S["effort"], setup.S["mode"]) == ("haiku", "low", "plan")     # what the agent sets shows
        await pilot.press("s")
        await until(pilot, lambda: isinstance(app.screen, ChatScreen) and app.chat is not None)
        i = app.chat.s.info
        assert (i.agent, i.model, i.effort, i.mode) == ("scout", "haiku", "low", "plan")
        assert meta(home, i.id)["agent"] == "scout"


async def test_a_card_the_person_changed_wins_over_the_agent(home, prov):
    make_agent(home, "scout", "finds things", model="haiku", effort="low", mode="plan")
    async with running(home) as (app, pilot):
        setup = await start_setup(app, pilot)
        await pick_agent(pilot, setup, "scout")
        await pilot.press("enter", "enter", "enter")                            # server, account, model: only looked at
        await pilot.press("down")                                               # effort: low -> the next one
        want = setup.S["effort"]
        assert want != "low" and "effort" in setup.touched and "model" not in setup.touched
        await pilot.press("s")
        await until(pilot, lambda: isinstance(app.screen, ChatScreen) and app.chat is not None)
        i = app.chat.s.info
        assert (i.agent, i.effort, i.model, i.mode) == ("scout", want, "haiku", "plan")


async def test_without_an_agent_the_cards_work_as_before(home, prov):
    make_agent(home, "scout", "finds things", model="haiku", mode="plan")
    async with running(home) as (app, pilot):
        setup = await start_setup(app, pilot)
        await pick_agent(pilot, setup, "scout")
        await pick_agent(pilot, setup, "")                                      # and back to (no agent)
        assert setup.S["agent"] == "" and setup.S["model"] == setup.base["model"] and setup.S["mode"] == setup.base["mode"]
        await pilot.press("s")
        await until(pilot, lambda: isinstance(app.screen, ChatScreen) and app.chat is not None)
        assert app.chat.s.info.agent == "" and app.chat.s.info.mode == setup.base["mode"]


async def test_a_tree_without_agents_says_so_on_the_agent_card(home, prov):
    async with running(home) as (app, pilot):
        setup = await start_setup(app, pilot)
        await pilot.press("a")
        await until(pilot, lambda: setup.picking)
        card = plain(app, "#card")
        assert "(no agent)" in card and (not setup.agents) == ("no agents in this tree yet" in card)
