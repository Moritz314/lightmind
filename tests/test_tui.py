"""Orbit TUI: Textual Pilot, the fake provider, no network.  The tree is the shipped template plus the overlay."""
import asyncio
import json
import subprocess

import pytest
from lightmind.tui.chatinput import ChatInput

from lightmind import loop as L
from lightmind import model as m
from lightmind.providers.base import Notice, ToolDone
from lightmind.tui import app as tuiapp
from lightmind.tui import data
from lightmind.tui.chat import ChatScreen
from lightmind.tui.screens import AccountsScreen, AskModal, DashScreen, MapScreen, Pick, ResumeScreen, SetupScreen

from test_tui_support import (FakeProv, earlier_chat, edit, idle, install, open_chat, pause_forever, plain, quiet,
                              running, say, texts, until)


@pytest.fixture
def prov(monkeypatch, home):
    quiet(home)
    return install(monkeypatch, FakeProv())


def has(app, needle: str) -> bool:
    return any(needle in t for t in texts(app))


# ------------------------------------------------------------------ landing
async def test_landing_renders_the_menu_from_the_settings_and_follows_an_edit(home):
    async with running(home) as (app, pilot):
        assert isinstance(app.screen, MapScreen)
        menu = plain(app, "#menu")
        for label in ("New chat", "Resume chat", "Accounts", "Dashboard", "Quit"):
            assert label in menu
        edit(home, '"Dashboard | d | dash"', '"Dashboard | d | dash"\n    - "Journal | j | resume"')
        await until(pilot, lambda: "Journal" in plain(app, "#menu"), timeout=4)
        await pilot.press("j")                                      # the new row works at once, with its own key and action
        await until(pilot, lambda: isinstance(app.screen, ResumeScreen))


async def test_an_unknown_layout_falls_back_to_orbit_with_a_notice(home):
    async with running(home) as (app, pilot):
        edit(home, "layout: orbit", "layout: dashboard")
        mv = app.screen.query_one("#map")
        await until(pilot, lambda: any("dashboard" in t for t, _ in mv.evlog), timeout=4)
        assert any("showing orbit" in t for t, _ in mv.evlog)


async def test_a_half_edited_lightmind_md_does_not_stop_the_landing(home):
    async with running(home) as (app, pilot):
        edit(home, "layout: orbit", "layout: [orbit")                # broken YAML while the user is typing
        await pilot.pause(1.3)
        assert isinstance(app.screen, MapScreen) and "New chat" in plain(app, "#menu")


async def test_the_map_is_made_of_the_servers_and_open_chats_of_the_tree(home, prov):
    for name in ("buildbox", "gpubox"):
        (home.root / "servers" / name).mkdir()
        (home.root / "servers" / name / "info.md").write_text(f"---\nhost: {name}.lan\n---\n# {name}\n", "utf-8")
    sid = await earlier_chat(home, prov, "landing page copy")
    s = L.resume_session(home, sid)                                  # open again: status running
    async with running(home) as (app, pilot):
        mv = app.screen.query_one("#map")
        assert {x.name for x in mv.D.servers} == {"laptop", "buildbox", "gpubox"} and mv.D.hub == "laptop"
        assert [x.id for x, _ in mv.live] == [sid]
        assert "3 servers" in mv.evlog[0][0] and "1 open chat" in mv.evlog[0][0]
        picture = mv.render().plain
        for word in ("laptop", "buildbox", "gpubox", "landing page"):
            assert word in picture
        await pilot.press("tab")                                     # the node to dive into
        assert mv.sel != "laptop"
    s.close()


async def test_the_animated_landing_sends_packets_between_servers_and_a_key_skips_the_intro(home, prov):
    (home.root / "servers" / "buildbox").mkdir()
    (home.root / "servers" / "buildbox" / "info.md").write_text("---\nhost: buildbox.lan\n---\n# p\n", "utf-8")
    sid = await earlier_chat(home, prov)
    L.resume_session(home, sid)
    async with running(home, anim=True) as (app, pilot):
        scr, mv = app.screen, app.screen.query_one("#map")
        assert scr.intro > 0
        await pilot.press("x")                                       # any key: skip the intro, nothing else happens
        assert scr.intro == 0 and isinstance(app.screen, MapScreen)
        mv.next_pulse = 0
        await pilot.pause(0.3)
        assert mv.pulses and mv.pulses[0]["src"] != mv.pulses[0]["dst"]
        assert "⇄" in mv.evlog[-1][0] and "remember the m" in mv.evlog[-1][0]
        assert "◆" in mv.render().plain


def test_animation_is_off_without_a_terminal_or_with_no_color(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("LM_NO_ANIM", raising=False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True, raising=False)
    assert tuiapp.anim_allowed()
    monkeypatch.setenv("LM_NO_ANIM", "1")
    assert not tuiapp.anim_allowed()
    monkeypatch.delenv("LM_NO_ANIM")
    monkeypatch.setenv("NO_COLOR", "1")
    assert not tuiapp.anim_allowed()
    monkeypatch.delenv("NO_COLOR")
    monkeypatch.setattr("sys.stdout.isatty", lambda: False, raising=False)
    assert not tuiapp.anim_allowed()


# ----------------------------------------------------------------- new chat
async def test_the_setup_cards_open_a_session_with_the_chosen_settings(home, prov):
    async with running(home) as (app, pilot):
        await pilot.press("n")
        await until(pilot, lambda: isinstance(app.screen, SetupScreen) and hasattr(app.screen, "S"))
        card = lambda: plain(app, "#card")                           # noqa: E731
        assert "laptop" in card() and "default" in card()
        await pilot.press("enter")                                   # server: laptop
        assert "claude-abo" in card() and "claude-subscription" in card()
        await pilot.press("enter")                                   # account
        assert "sonnet" in card() and "tier → opus" in card()
        await pilot.press("down", "enter")                           # model: sonnet -> haiku
        assert "xhigh" in card() and "medium" in card()
        await pilot.press("down", "enter")                           # effort: medium -> high
        assert "200k" in card()
        await pilot.press("enter")                                   # context
        t0 = data.items(home)[0].name
        assert t0 in card() and "skills, always on:" in card() and "example-skill" in card()
        await pilot.press("space", "enter")                          # tools: the first one off
        assert "Plan" in card() and "asks:" in card()
        await pilot.press("down", "enter")                           # mode: auto -> plan, the last card starts the chat
        await until(pilot, lambda: isinstance(app.screen, ChatScreen) and app.chat is not None)
        i = app.chat.s.info
        assert (i.server, i.account, i.model, i.effort, i.mode, i.ctx_limit) == ("laptop", "claude-abo", "haiku", "high", "plan", 200_000)
        assert app.chat.s.state.tools_removed == [t0]
        assert not any(r.kind == "skill" for r in app.chat.s.state.tools_added)
        assert "Plan" in plain(app, "#status") and "haiku" in plain(app, "#status")


async def test_the_setup_defaults_come_from_lightmind_md(home, prov):
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)                                  # n, then "start now": nothing was chosen
        i = app.chat.s.info
        assert (i.server, i.model, i.effort, i.mode, i.ctx_limit) == ("laptop", "sonnet", "medium", "auto", 200_000)
        assert app.chat.s.state.tools_removed == [] and app.chat.s.state.tools_added == []


async def test_api_accounts_are_listed_with_their_models_and_the_missing_adapter_is_reported(home, prov):
    (home.root / "providers" / "api-1.md").write_text(
        "---\ntype: anthropic-api\ndefault_model: gpt-x\nmodels:\n  gpt-x: {input: 1, output: 2}\ntiers: {cheap: gpt-x}\n---\n# api\n", "utf-8")
    async with running(home) as (app, pilot):
        await pilot.press("n")
        await until(pilot, lambda: isinstance(app.screen, SetupScreen) and hasattr(app.screen, "S"))
        await pilot.press("enter")                                   # server
        assert "api-1" in plain(app, "#card") and "anthropic-api" in plain(app, "#card")
        await pilot.press("down", "down", "enter")                   # claude-abo -> abo-1 -> api-1
        assert app.screen.S["account"] == "api-1" and app.screen.S["model"] == "gpt-x"
        assert "gpt-x" in plain(app, "#card") and "tier → gpt-x" in plain(app, "#card")
        await pilot.press("s")                                       # the chat opens on the API account (the fake stands in for its adapter)
        await until(pilot, lambda: isinstance(app.screen, ChatScreen) and app.chat is not None)
        assert (app.chat.s.info.account, app.chat.s.info.model) == ("api-1", "gpt-x")


async def test_an_account_whose_adapter_is_missing_says_so_on_the_card(home, monkeypatch):
    quiet(home)
    (home.root / "providers" / "or.md").write_text("---\ntype: future-type\ndefault_model: m1\nmodels:\n  m1: {input: 1, output: 2}\n---\n# or\n", "utf-8")
    async with running(home) as (app, pilot):
        await pilot.press("n")
        await until(pilot, lambda: isinstance(app.screen, SetupScreen) and hasattr(app.screen, "S"))
        app.screen.S["account"] = "or"
        await pilot.press("s")
        await until(pilot, lambda: "phase 2" in plain(app, "#card"))
        assert isinstance(app.screen, SetupScreen)


# --------------------------------------------------------------------- chat
async def test_a_message_shows_the_answer_and_updates_the_status_line(home, prov):
    prov.context_tokens = 40_000
    prov.turns = [[m.ToolCall("t1", "bash", {"command": "ls -la"}), ToolDone("t1", "bash", True, "total 4\nfile"),
                   m.Text("Hello **the user**\n\n- one\n- two"), m.Usage(100_000, 20_000)]]
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        before = plain(app, "#status")
        assert "sonnet" in before and "claude-abo" in before and "Auto" in before and "0%" in before
        await say(pilot, "hi there")
        await until(pilot, lambda: has(app, "Hello the user") or has(app, "Hello"))
        await idle(pilot, app)
        after = plain(app, "#status")
        assert after != before and "20%" in after and "$0.60~" in after
        lines = texts(app)
        assert any(t.startswith("bash > ls -la") and "✓ total 4" in t for t in lines)       # one dim line, the result on it
        assert any("hi there" in t for t in lines)
        assert prov.requests[0].messages[-1].text == "hi there"


async def test_streamed_deltas_grow_one_message_and_keep_paragraph_breaks(home, prov):
    """API adapters yield one Text per token delta, whitespace-only ones included."""
    from lightmind.tui.chat import Msg
    deltas = ["First", " para", "graph.", "\n", "\n", "Second", " paragraph", " with ", "**bold**", "."] * 3
    prov.turns = [[*(m.Text(d) for d in deltas), m.Usage()]]
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await say(pilot, "stream it")
        await until(pilot, lambda: has(app, "bold"))
        await idle(pilot, app)
        agent = [w for w in app.chat.query(Msg) if w.plain.startswith("First")]
        assert len(agent) == 1 and agent[0].plain == "".join(deltas)       # ONE message, the blank line kept
        assert sum("paragraph" in w.plain for w in app.chat.query(Msg)) == 1


def test_markdown_answers_render_without_their_markers():
    from rich.console import Console
    from lightmind.tui.chat import Md
    c = Console(width=60, record=True, color_system=None)
    c.print(Md("# Title\n\nsome **bold** and `code`\n\n```py\nprint(1)\n```\n\n- a\n- b"))
    out = c.export_text()
    assert "Title" in out and "bold" in out and "print(1)" in out and "• a" in out
    assert "**" not in out and "```" not in out and "# Title" not in out


async def test_the_vault_line_comes_first_and_the_first_message_waits_for_it(home, monkeypatch):
    quiet(home, "on_start:\n  - run: sleep 1 && echo Vault connected - 3 notes\n    show: true\n")
    prov = install(monkeypatch, FakeProv())
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await say(pilot, "hello")
        await pilot.pause(0.3)
        assert prov.requests == []                                   # the hook is still running
        await until(pilot, lambda: len(prov.requests) == 1, timeout=10)
        await idle(pilot, app)
        assert has(app, "Vault connected - 3 notes")


async def test_an_ask_opens_the_modal_and_the_answer_reaches_the_dispatcher(home, prov):
    async def writes(self, req):
        r = await self.dispatcher.call("write", {"path": "providers/extra.md", "content": "x\n"})
        yield m.Text("write ok" if r.ok else "write refused")
        yield m.Usage()
    target = home.root / "providers" / "extra.md"
    prov.turns = [writes, writes]
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await say(pilot, "make a provider")
        await until(pilot, lambda: isinstance(app.screen, AskModal))
        assert "providers" in app.screen.question and "extra.md" in app.screen.question
        assert not target.exists()
        box = app.screen.query_one("#ask").region                    # the request is pinned at the bottom, not in the middle
        assert box.bottom >= app.screen.size.height - 1
        await pilot.pause(0.6)
        await pilot.press("n", "enter")                              # no: the call comes back refused
        await until(pilot, lambda: has(app, "write refused"))
        await idle(pilot, app)
        assert not target.exists() and isinstance(app.screen, ChatScreen)
        await say(pilot, "again")
        await until(pilot, lambda: isinstance(app.screen, AskModal))
        await pilot.pause(0.6)
        await pilot.press("y", "enter")                              # yes: the file is written
        await until(pilot, lambda: has(app, "write ok"))
        assert target.read_text() == "x\n"


async def test_slash_mode_changes_the_status_line_and_a_bad_value_shows_the_cores_error(home, prov):
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await say(pilot, "/mode plan")
        await until(pilot, lambda: "Plan" in plain(app, "#status"))
        assert app.chat.s.info.mode == "plan" and has(app, "mode: Plan")
        await say(pilot, "/mode wild")
        await until(pilot, lambda: has(app, "unknown mode 'wild'"))
        assert "Plan" in plain(app, "#status")
        await say(pilot, "/effort turbo")
        await until(pilot, lambda: has(app, "effort: unknown 'turbo'"))
        await say(pilot, "/effort high")
        await until(pilot, lambda: has(app, "effort: high"))
        assert app.chat.s.info.effort == "high"
        await say(pilot, "/model haiku")
        await until(pilot, lambda: app.chat.s.info.model == "haiku" and "haiku" in plain(app, "#status"))
        await say(pilot, "/account nobody")
        await until(pilot, lambda: has(app, "unknown account or alias 'nobody'"))
        await say(pilot, "/frobnicate")
        await until(pilot, lambda: has(app, "unknown command /frobnicate"))


async def test_slash_without_an_argument_opens_a_picker(home, prov):
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await say(pilot, "/mode")
        await until(pilot, lambda: isinstance(app.screen, Pick))
        await pilot.press("down", "enter")                           # auto -> plan
        await until(pilot, lambda: app.chat.s.info.mode == "plan" and "Plan" in plain(app, "#status"))
        await say(pilot, "/tools")
        await until(pilot, lambda: isinstance(app.screen, Pick))
        t0 = data.items(home)[0].name
        await pilot.press("space", "enter")                          # the first tool off
        await until(pilot, lambda: app.chat.s.state.tools_removed == [t0])
        assert has(app, "tools:") and not any(f"tools: {t0}," in t or t == f"tools: {t0}" for t in texts(app))


async def test_slash_tools_plus_minus_and_the_server_note(home, prov):
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        t0 = next(i.name for i in data.items(home) if i.usable)
        await say(pilot, f"/tools -{t0}")
        await until(pilot, lambda: app.chat.s.state.tools_removed == [t0])
        await say(pilot, f"/tools +{t0}")
        await until(pilot, lambda: app.chat.s.state.tools_removed == [])
        await say(pilot, "/tools +ghost")
        await until(pilot, lambda: has(app, "no tool named 'ghost'"))
        await say(pilot, "/server")
        await until(pilot, lambda: has(app, "opens the permanent session on another server"))


async def test_the_status_line_follows_the_template_and_keeps_unknown_placeholders(home, prov):
    edit(home, 'statusline: "{server} · {model}', 'statusline: "{nope} {server} · {effort} · {model}')
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        s = plain(app, "#status")
        assert "{nope}" in s and "laptop · medium · sonnet" in s


async def test_clear_and_ui_slash_rows_close_the_session_and_go_back_to_setup(home, prov):
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        sid = app.chat.s.id
        await say(pilot, "/clear")                                   # `/clear → new_chat` of the shipped lightmind.md
        await until(pilot, lambda: isinstance(app.screen, SetupScreen))
        meta = json.loads((home.root / "runs" / sid / "meta.json").read_text())
        assert meta["status"] == "done" and app.chat is None


async def test_esc_stops_a_running_turn_and_the_session_stays_usable(home, prov):
    prov.turns = [pause_forever, [m.Text("back again"), m.Usage()]]
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await say(pilot, "do it")
        await until(pilot, lambda: has(app, "thinking about it") and app.chat.busy)
        await pilot.press("escape")
        await until(pilot, lambda: "stopping" in plain(app, "#hint"))
        assert app.chat.busy and isinstance(app.screen, ChatScreen)  # the close takes a moment, the UI answers meanwhile
        await say(pilot, "/effort low")                              # ... and still takes commands
        await until(pilot, lambda: app.chat.s.info.effort == "low")
        await idle(pilot, app)
        assert prov.closed == 1 and has(app, "stopped") and not app.chat.s._busy
        await say(pilot, "second try")
        await until(pilot, lambda: has(app, "back again"))
        await idle(pilot, app)
        assert prov.requests[-1].messages[-1].text == "second try"


async def test_esc_in_an_idle_chat_closes_it_cleanly_and_goes_back(home, prov):
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        sid = app.chat.s.id
        await pilot.press("escape")
        await until(pilot, lambda: isinstance(app.screen, MapScreen))
        assert json.loads((home.root / "runs" / sid / "meta.json").read_text())["status"] == "done"


async def test_not_logged_in_shows_the_hint_and_the_session_stays_usable(home, prov):
    from lightmind.providers.claude_sdk import NotLoggedIn

    async def nobody(self, req):
        raise NotLoggedIn("claude-abo")
        yield
    prov.turns = [nobody, [m.Text("now it works"), m.Usage()]]
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await say(pilot, "hello")
        await until(pilot, lambda: has(app, "not logged in"))
        assert "✗ not logged in: run `lm login claude-abo`" in texts(app)
        await idle(pilot, app)
        assert app.chat.s.info.status == "running"
        await say(pilot, "again")
        await until(pilot, lambda: has(app, "now it works"))


async def test_notices_errors_and_provider_warnings_are_shown_by_kind(home, prov):
    prov.turns = [[Notice("warning", "account has no config_dir"), m.Error("overloaded", "busy"), m.Text("late"), m.Usage()]]
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await say(pilot, "x")
        await idle(pilot, app)
        assert "! account has no config_dir" in texts(app) and "✗ overloaded: busy" in texts(app)


# ------------------------------------------------------------------- resume
async def test_resume_lists_an_earlier_session_and_continues_it(home, prov):
    sid = await earlier_chat(home, prov)
    async with running(home) as (app, pilot):
        await pilot.press("r")
        await until(pilot, lambda: isinstance(app.screen, ResumeScreen) and app.screen.rows)
        listing = plain(app, "#list")
        for want in ("remember the milk", "claude-abo", "sonnet", "auto", "done"):
            assert want in listing
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, ChatScreen) and app.chat is not None)
        assert app.chat.s.id == sid and app.chat.s.resumed
        assert has(app, "remember the milk") and has(app, "noted, milk it is") and has(app, "resumed")
        prov.turns.append([m.Text("more milk"), m.Usage()])
        await say(pilot, "and eggs")
        await until(pilot, lambda: has(app, "more milk"))
        await idle(pilot, app)
        assert prov.requests[-1].messages[-1].text == "and eggs"
        assert [x.role for x in app.chat.s.messages] == ["user", "assistant", "user", "assistant"]


async def test_the_resume_list_is_empty_without_chats(home, prov):
    async with running(home) as (app, pilot):
        await pilot.press("r")
        await until(pilot, lambda: isinstance(app.screen, ResumeScreen) and app.screen.rows == [])
        assert "no chats yet" in plain(app, "#list")
        await pilot.press("escape")
        await until(pilot, lambda: isinstance(app.screen, MapScreen))


# ------------------------------------------------------ accounts, dash, quit
async def test_the_accounts_view_shows_the_last_limits_and_starts_no_cli(home, prov, monkeypatch):
    import time
    state = home.path("state")
    state.mkdir(parents=True, exist_ok=True)
    (state / "limits-claude-abo.json").write_text(json.dumps({"seen": "2026-10-20T10:00:00", "limits": {
        "five_hour": {"utilization": 0.42, "resets_at": time.time() + 2 * 3600 + 600},
        "seven_day": {"utilization": 0.8, "resets_at": time.time() + 3 * 86400}}}), "utf-8")

    def refuse(*a, **k):
        raise AssertionError("a process was started")
    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", refuse)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", refuse)
    async with running(home) as (app, pilot):
        await pilot.press("a")
        await until(pilot, lambda: isinstance(app.screen, AccountsScreen) and app.screen.rows)
        view = plain(app, "#acc")
        assert "claude-abo" in view and "claude-subscription" in view and "default sonnet" in view
        assert "42%" in view and "80%" in view and "resets in 2h" in view and "resets in 2d 23h" in view
        assert "abo-1" in view and "no limits seen yet" in view      # an account that never ran
        await pilot.press("escape")
        await until(pilot, lambda: isinstance(app.screen, MapScreen))


async def test_the_dashboard_lists_runs_and_cost(home, prov):
    sid = await earlier_chat(home, prov, "price check")
    async with running(home) as (app, pilot):
        await pilot.press("d")
        await until(pilot, lambda: isinstance(app.screen, DashScreen) and app.screen.view is not None)
        await pilot.pause(0.6)
        view = plain(app, "#dash")
        assert "price check" in view and "claude-abo" in view and "laptop" in view and "COST 24H" in view and "$" in view


async def test_quit_closes_the_open_chat_and_collects_the_on_end_lines(home, monkeypatch):
    quiet(home)
    edit(home, "on_end: []", "on_end:\n  - run: echo bye from the hook\n    show: true")
    prov = install(monkeypatch, FakeProv())
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        sid = app.chat.s.id
        await pilot.press("ctrl+q")
        await until(pilot, lambda: app.exit_lines)
    assert app.exit_lines[0] == "bye from the hook"                        # Ctrl+Q archives the chat, which adds its own line
    assert len(app.exit_lines) == 2 and app.exit_lines[1].startswith("archived:")
    assert json.loads((home.root / "runs" / sid / "meta.json").read_text())["status"] == "done"


# ------------------------------------------------------------------ skills: always on, and slash commands
def _ctx_skills(app):
    return {s.name for s in app.chat.s.context.skills} if hasattr(app.chat.s, "context") else None


async def test_skills_cannot_be_switched_off_in_a_chat(home, prov):
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await say(pilot, "/tools -example-skill")
        await until(pilot, lambda: has(app, "is a skill; skills are always on"))
        assert app.chat.s.state.tools_removed == []
        await say(pilot, "/tools")
        await until(pilot, lambda: isinstance(app.screen, Pick))
        assert not any(o[0] in ("example-skill", "run-tracking") for o in app.screen.opts)


async def test_the_skill_index_stays_complete_with_tools_off(home, prov):
    from lightmind import context as C
    s = L.open_session(home, tools_off=["pdf", "example-skill"])
    ctx = C.build_context(home, agent=None, mode="auto", task=None, session=s.state)
    assert "- example-skill:" in ctx.prefix and "- run-tracking:" in ctx.prefix
    s.close()


async def test_a_skill_is_a_slash_command_that_sends_a_user_message(home, prov):
    sk = {s.name: s for s in home.list_skills()}["run-tracking"]
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await say(pilot, "/run-tracking make a hero")
        await until(pilot, lambda: prov.requests)
        await idle(pilot, app)
        text = prov.requests[-1].messages[-1].text
        assert text.startswith('Use the skill "run-tracking"') and text.endswith("\n\n/run-tracking make a hero")      # the slash word stays in the sentence
        assert (sk.body.strip() in text) or (sk.path in text)
        assert has(app, "/run-tracking make a hero")
        await say(pilot, "/skill example-skill and more")
        await until(pilot, lambda: len(prov.requests) == 2)
        assert prov.requests[-1].messages[-1].text.endswith("\n\nand more")
        await idle(pilot, app)
        await say(pilot, "/skill")
        await until(pilot, lambda: has(app, "/run-tracking") and has(app, "always on"))


async def test_a_large_skill_goes_by_path_and_a_colliding_name_by_skill_prefix(home, prov):
    d = home.path("skills") / "big"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: big\ndescription: huge one\n---\n" + "x" * 40000, "utf-8")
    d = home.path("skills") / "model"
    d.mkdir()
    (d / "SKILL.md").write_text("---\nname: model\ndescription: clashes\n---\nsmall body\n", "utf-8")
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        await say(pilot, "/big do it")
        await until(pilot, lambda: prov.requests)
        text = prov.requests[-1].messages[-1].text
        assert text == f'Use the skill "big" (read {d.parent / "big" / "SKILL.md"} first).\n\n/big do it'
        await idle(pilot, app)
        await say(pilot, "/skill:model go")
        await until(pilot, lambda: len(prov.requests) == 2)
        assert 'Use the skill "model"' in prov.requests[-1].messages[-1].text and "small body" in prov.requests[-1].messages[-1].text


async def test_the_popup_lists_the_skills_after_the_builtins(home, prov):
    names = [n for n, _ in data.slash_commands(home)]
    assert names.index("clear") < names.index("run-tracking") and names.index("key") < names.index("example-skill")
    assert dict(data.slash_commands(home))["run-tracking"]
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        app.screen.query_one(ChatInput).value = "/run"
        await pilot.pause(0.2)
        assert [n for n, _ in app.chat.pop] == ["run-tracking"]


async def test_a_reader_who_scrolled_up_is_not_pulled_down_by_new_messages(home, prov):
    async with running(home) as (app, pilot):
        await open_chat(app, pilot)
        chat = app.screen
        for i in range(60):
            await chat.add("note", f"line {i}")
        await idle(pilot, app)
        box = chat.query_one("#msgs")
        assert box.scroll_y >= box.max_scroll_y - 1                  # at the end, new lines follow
        box.scroll_to(y=0, animate=False)
        await pilot.pause(0.1)
        await chat.add("note", "late line")
        await pilot.pause(0.1)
        assert box.scroll_y == 0                                     # scrolled up: stays where he reads
        await chat.add("user", "my own message")
        await pilot.pause(0.1)
        assert box.scroll_y >= box.max_scroll_y - 1                  # his own message jumps to the end
