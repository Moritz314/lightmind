"""Slash words anywhere in a message (`slash.py`, plan/PLAN-SLASH.md): the trigger rule, the parser, what the
commands accept, and `lm chat` on top of it.  The Orbit side is in test_tui_slash.py."""
import sys

import pytest

from lightmind import cli
from lightmind import slash as S
from lightmind.context import skill_commands
from lightmind.files import Home
from lightmind.model import Text, Usage
from lightmind.slash import Act, Catalog, Take, complete, parse, summary, word_at

from test_cli import lm as lm_cli, script  # noqa: F401 - `lm_cli` is the fixture of test_cli.py: `lm chat` on a scripted provider

BUILTIN = ("mode", "model", "account", "effort", "context", "tools", "skill", "server", "accounts", "clear", "login",
           "key", "quit")
MODELS = ["opus", "sonnet", "haiku", "strong"]


@pytest.fixture
def lm(lm_cli):  # noqa: F811 - the fixture of test_cli.py, under the name the tests use
    return lm_cli


def cat(**over) -> Catalog:
    """A surface like Orbit's, with the commands the plan names: settings that run anywhere, ones that leave the chat
    (`exit new resume settings` as `ui.slash` rows), two that take the rest of the line, and skills."""
    d = dict(builtin=BUILTIN, skills={"arsenal", "vault", "cells", "skill:model"}, rows=("exit", "new", "resume", "settings"),
             takes={"effort": Take(S.efforts), "model": Take(lambda: MODELS, S.model_ok)})
    d.update(over)
    return Catalog(**d)


def P(text: str, **over):
    return parse(text, cat(**over))


def acts(p) -> list[tuple[str, str]]:
    return [(a.name, a.arg) for a in p.acts]


def plain(p) -> bool:
    """Nothing was recognized: no command, no skill, no error."""
    return not (p.acts or p.skills or p.then or p.error)


def at(text: str, pos: int | None = None):
    w = word_at(text, len(text) if pos is None else pos)
    return text[w[0]:w[1]] if w else None


# ------------------------------------------------------------------------ the trigger rule
@pytest.mark.parametrize("text, word", [("/", "/"), ("/mo", "/mo"), ("a /mo", "/mo"), ("a\n/mo", "/mo"), ("a\t/mo", "/mo"),
                                        ("compare this with /ar", "/ar"), ("/etc", "/etc")])
def test_a_slash_word_starts_at_the_start_of_the_text_or_after_a_blank(text, word):
    assert at(text) == word


@pytest.mark.parametrize("text", ["a/b", "https://x.de/y", "see C:/x", "and/or", "x/", "a /b/c d/e", "with//x", "//mo", "a //mo"])
def test_a_slash_inside_a_word_or_a_double_slash_opens_nothing(text):
    assert at(text) is None


def test_the_word_is_the_one_under_the_cursor():
    t = "compare /ar with /ce"
    assert at(t, 0) is None and at(t, 5) is None                 # in the first word
    assert (at(t, 8), at(t, 9), at(t, 11)) == ("/ar", "/ar", "/ar")        # at its start, inside it, at its end
    assert at(t, 13) is None                                     # in "with"
    assert at(t, 21) == "/ce" and at(t, 99) == "/ce"             # the end of the text (a cursor past it counts as the end)
    assert at("a  /b", 2) is None and at("", 0) is None          # between two blanks, empty text
    assert at("/run", 0) == "/run"                               # a programmatic value with the cursor at the front


def test_no_slash_word_inside_code():
    assert at("`x /mo y`", 6) is None                            # inline
    assert at("``a /mo b``", 7) is None                          # a longer run needs a run of the same length
    assert at("`x y` /mo") == "/mo"                              # closed code is over
    assert at("`a /mo ``", 6) == "/mo"                           # backticks without a partner are plain text
    assert at("```\n/mo") is None                                # an open fence runs to the end
    assert at("```py\n/mo\n```", 8) is None                      # fenced
    assert at("```\nx\n```\n/mo") == "/mo"                       # after the fence
    assert at("``` /mo") is None                                 # that is a fence with an info string
    assert at("```x /mo```", 6) is None                          # three backticks that close on the same line are inline


def test_complete_replaces_only_that_word():
    assert complete("/mo", 0, 3, "model") == ("/model ", 7)                                  # at the end: a blank behind it
    assert complete("compare /ar with", 8, 11, "arsenal") == ("compare /arsenal with", 17)    # the blank that is there is used
    assert complete("x\n/mo\ny", 2, 5, "model") == ("x\n/model\ny", 8)                      # before a newline: none added


# ----------------------------------------------------------------------------- skills
def test_skills_anywhere_stay_in_the_text_each_once_in_order():
    t = "compare this with /arsenal and /cells, then /arsenal again"
    p = P(t)
    assert p.skills == ["arsenal", "cells"] and p.text == t and plain(P("nothing here")) and not (p.acts or p.then or p.error)
    p = P("/Vault build this")                                      # at the start, any case; the token stays in the text
    assert p.skills == ["vault"] and p.text == "/Vault build this"
    assert P("use /skill:model for it").skills == ["skill:model"]    # the name a clashing skill goes by


def test_a_skill_word_may_end_in_punctuation_but_must_start_after_a_blank():
    assert P("with /arsenal, please").skills == ["arsenal"] and P("with /arsenal. Thanks").skills == ["arsenal"]
    assert P("(/arsenal)").skills == [] and P("a/arsenal").skills == []


# ---------------------------------------------------------------------------- commands
def test_commands_mid_text_with_and_without_an_argument():
    p = P("/model opus /effort high explain X")                # the plan's example
    assert acts(p) == [("model", "opus"), ("effort", "high")] and p.text == "explain X"
    p = P("explain /effort high X")
    assert acts(p) == [("effort", "high")] and p.text == "explain X"
    p = P("explain X /model")                                 # without an argument: it runs without one (a picker)
    assert acts(p) == [("model", "")] and p.text == "explain X"
    p = P("/model /effort low x")                                # a slash word is never the argument
    assert acts(p) == [("model", ""), ("effort", "low")] and p.text == "x"
    p = P("/mode plan explain X")                                 # a command that takes any word
    assert acts(p) == [("mode", "plan")] and p.text == "explain X"
    p = P("/account\nexplain X")                                  # the argument is on the same line or it is none
    assert acts(p) == [("account", "")] and p.text == "explain X"


def test_only_commands_the_surface_has_count():
    p = P("explain /context 200k", builtin=("model",))             # a skill-less surface without /context: plain text
    assert plain(p) and p.text == "explain /context 200k"


def test_commands_and_skills_keep_the_order_they_stand_in():
    p = P("/effort low compare /vault with /model sonnet and /arsenal /cells /vault")
    assert acts(p) == [("effort", "low"), ("model", "sonnet")]
    assert p.skills == ["vault", "arsenal", "cells"]
    assert p.text == "compare /vault with and /arsenal /cells /vault"


# ------------------------------------------------------------- commands that leave the chat
def test_a_command_that_leaves_the_chat_alone_or_last():
    for line, name in [("/exit", "exit"), ("  /clear  ", "clear"), ("/new", "new"), ("/resume", "resume"), ("/settings", "settings"),
                       ("/accounts", "accounts"), ("/quit", "quit")]:
        p = P(line)
        assert p.then == Act(name, "", "session") and p.text == "" and not (p.acts or p.skills or p.error), line
    p = P("explain X /exit")                                  # text before: it is sent, the command follows the answer
    assert p.text == "explain X" and p.then.name == "exit" and not p.acts
    p = P("/model opus explain X /settings")
    assert acts(p) == [("model", "opus")] and p.text == "explain X" and p.then.name == "settings"
    p = P("compare with /arsenal /clear")
    assert p.skills == ["arsenal"] and p.text == "compare with /arsenal" and p.then.name == "clear"
    assert P("/server gpubox").then == Act("server", "gpubox", "session")     # some take a name
    assert P("sag hi /key").then == Act("key", "", "session")


@pytest.mark.parametrize("line", ["/clear and then", "/exit /model opus", "/new /new", "/server gpubox and more", "x /settings y",
                                  "/exit /arsenal", "/resume\nmore"])
def test_text_after_a_command_that_leaves_the_chat_refuses_everything(line):
    p = P(line)
    assert "goes last or alone" in p.error and "nothing was sent" in p.error
    assert not (p.acts or p.skills or p.then or p.text or p.unknown)


# ------------------------------------------------------------------- unknown words, `//`
@pytest.mark.parametrize("line", ["how goes /nope this", "/etc/hosts read", "read /usr/bin/env", "/foo bar", "x /unknown", "a/b c"])
def test_unknown_words_stay_text(line):
    p = P(line)
    assert p.text == line and plain(p)


def test_an_input_of_one_unknown_word_is_refused_as_a_typo():
    p = P("/frobnicate")
    assert p.error == "unknown command /frobnicate" and p.unknown and not p.text
    assert plain(P("/etc/hosts")) and P("/etc/hosts").text == "/etc/hosts"      # not shaped like a command: just text
    assert P("/mode").error == ""                                # a known one is no typo


def test_a_double_slash_writes_a_plain_slash_word():
    p = P("//model opus")
    assert p.text == "/model opus" and plain(p)
    p = P("write //effort high and //arsenal, not /vault")
    assert p.text == "write /effort high and /arsenal, not /vault" and p.skills == ["vault"] and not p.acts
    assert P("//x").text == "/x" and not P("//x").error
    assert P("//host/share // a///b").text == "//host/share // a///b"           # not the shape of a command: left as typed
    assert P("a `//model` b").text == "a `//model` b"            # code stays as written


# ---------------------------------------------------------------------- invalid arguments
def test_an_invalid_argument_refuses_everything_and_names_the_values():
    p = P("/model opus /effort super explain X")
    assert p.error == "effort: unknown 'super'; use low, medium, high, xhigh, max, ultracode"
    assert not (p.acts or p.skills or p.then or p.text or p.unknown)
    assert P("/effort").error == "" and P("/effort high").text == ""             # no word is fine; a good one is
    assert acts(P("/effort HIGH x")) == [("effort", "high")]                     # a hit ignores case and is given as listed


def test_model_takes_an_unlisted_word_only_when_it_is_no_prose():
    p = P("/model explain X")                                 # prose follows: a refused guess, not a model called "explain"
    assert p.error == "model: unknown 'explain'; use opus, sonnet, haiku, strong"
    assert acts(P("/model grok")) == [("model", "grok")]         # alone: sent as written, as it always was
    assert acts(P("explain this /model grok")) == [("model", "grok")]
    assert acts(P("/model grok /effort low")) == [("model", "grok"), ("effort", "low")]
    assert acts(P("/model claude-opus-4-9 explain X")) == [("model", "claude-opus-4-9")] and P("/model gpt-5 x").text == "x"
    assert acts(P("/model OPUS x")) == [("model", "opus")]


def test_a_command_without_a_list_takes_any_word():
    assert acts(P("/account abo-1 hallo")) == [("account", "abo-1")]
    assert acts(P("/model x", takes={})) == [("model", "x")]                      # no check configured: any word


# --------------------------------------------------------------------- commands of one line
def test_commands_that_take_the_rest_of_the_line_work_only_first():
    assert P("/tools -pdf").acts == [Act("tools", "-pdf", "line")] and P("/tools -pdf").text == ""
    assert P("  /tools").acts == [Act("tools", "", "line")]
    p = P("/skill arsenal baue X /model opus /exit")             # everything after it belongs to it
    assert p.acts == [Act("skill", "arsenal baue X /model opus /exit", "line")] and not (p.then or p.skills or p.error)
    p = P("explain /tools")
    assert "must come first" in p.error and not p.acts
    assert P("/arsenal /tools").error                            # not first either
    p = P("//tools -pdf")
    assert p.text == "/tools -pdf" and plain(p)


# ------------------------------------------------------------------------------------ code
@pytest.mark.parametrize("line", ["run `x /model opus y` now", "```\n/model opus /exit\n```\nhello", "wie ``a /arsenal b`` goes",
                                  "`/exit`", "```py\nx = '/exit'\n``` and then /nope"])
def test_nothing_in_code_counts(line):
    p = P(line)
    assert p.text == line.strip() and plain(p)


def test_a_word_after_closed_code_counts_again():
    p = P("a `x` /model opus b")
    assert acts(p) == [("model", "opus")] and p.text == "a `x` b"


# --------------------------------------------------------------------------- what is left
@pytest.mark.parametrize("line, text", [
    ("a /model opus b", "a b"), ("a /model opus", "a"), ("/model opus a", "a"), ("a\n/model opus\nb", "a\nb"),
    ("a /model opus\nb", "a\nb"), ("a\n\n/effort low b\n\nc", "a\n\nb\n\nc"), ("x\n/model\ny", "x\ny"), ("a\r\n/model\r\nb", "a\nb"),
    ("  /effort low   \n  hallo  ", "hallo"), ("/effort low", ""),
])
def test_cutting_a_command_leaves_clean_text(line, text):
    assert P(line).text == text


def test_text_without_slash_words_is_unchanged():
    assert not S.has_slash("a/b https://x.de C:/x") and S.has_slash("a /b") and S.has_slash("/b") and S.has_slash("a\n/b")
    assert P("  just a message  ").text == "just a message"


def test_summary_is_one_line_of_what_was_found():
    p = P("/model opus /effort high compare with /arsenal /clear")
    assert summary(p) == "model opus · effort high · skills: arsenal · then clear"
    assert summary(P("hallo")) == "" and summary(P("/tools -pdf")) == "tools"
    assert summary(P("/key sk-ant-SECRET")) == "then key"         # the word of a command that leaves the chat stays out


# ---------------------------------------------------------- what the surface passes in
def test_message_puts_one_instruction_per_skill_before_the_text(home):
    sk = skill_commands(home)
    p = parse("compare /run-tracking with /example-skill", Catalog(skills=sk))
    msg = S.message(home, p, sk)
    text = "compare /run-tracking with /example-skill"
    assert msg.endswith("\n\n" + text) and msg.count('Use the skill "') == 2
    assert msg.index('Use the skill "run-tracking"') < msg.index('Use the skill "example-skill"') < msg.index(text)
    assert S.message(home, parse("just text", Catalog(skills=sk)), sk) == "just text"
    one = parse("/run-tracking make a hero", Catalog(skills=sk))                  # one skill: what skill_message gave
    assert S.message(home, one, sk) == __import__("lightmind.context").context.skill_message(home, sk["run-tracking"], "/run-tracking make a hero")


def test_models_are_the_models_the_default_the_tiers_and_what_they_point_at(home):
    assert set(S.models(home, "abo-1")) == {"sonnet", "strong", "medium", "cheap", "opus", "haiku"}
    assert S.models(home, "claude-abo")[:3] == ["opus", "sonnet", "haiku"]
    assert S.models(home, "nobody") == [] and S.EFFORTS == ("low", "medium", "high", "xhigh", "max") and S.efforts() == S.EFFORTS + ("ultracode",)


# ------------------------------------------------------------------------------ `lm chat`
def sent(lm) -> list[str]:
    """What the model was sent, turn by turn: the last message of each request."""
    return [r.messages[-1].text for r in lm.fake.requests]


def test_chat_commands_mid_text_run_and_the_rest_is_sent(lm):
    script(lm, [Text("done"), Usage()])
    code, out, err = lm("chat", stdin="/model opus /effort high explain X\n/quit\n")
    assert code == 0 and sent(lm) == ["explain X"]
    assert "model: opus" in out and "effort: high" in out
    assert "  model opus · effort high" in out                                      # what was taken out of the line
    assert (lm.fake.requests[0].model, lm.fake.requests[0].effort) == ("opus", "high")


def test_chat_loads_every_skill_named_anywhere(lm):
    script(lm, [Text("ok"), Usage()])
    code, out, err = lm("chat", stdin="compare this with /run-tracking and /example-skill\n")
    text = sent(lm)[0]
    assert text.endswith("\n\ncompare this with /run-tracking and /example-skill")
    assert text.index('Use the skill "run-tracking"') < text.index('Use the skill "example-skill"')
    assert "  skills: run-tracking, example-skill" in out


def test_chat_a_command_that_leaves_runs_after_the_answer(lm):
    script(lm, [Text("hallo!"), Usage()], [Text("never"), Usage()])
    code, out, err = lm("chat", stdin="sag hallo /quit\nnie gesendet\n")
    assert code == 0 and sent(lm) == ["sag hallo"] and "hallo!" in out and "then quit" in out


def test_chat_text_after_a_command_that_leaves_refuses_the_line(lm):
    script(lm, [Text("fine"), Usage()])
    code, out, err = lm("chat", stdin="/quit and then\nweiter\n/quit\n")
    assert code == 0 and sent(lm) == ["weiter"] and "goes last or alone" in err


def test_chat_an_invalid_argument_sends_and_runs_nothing(lm):
    script(lm, [Text("ok"), Usage()])
    code, out, err = lm("chat", stdin="/model opus /effort super explain X\n/model explain X\n/quit\n")
    assert "error: effort: unknown 'super'; use low, medium, high, xhigh, max" in err
    assert "error: model: unknown 'explain'; use " in err
    assert lm.fake.requests == [] and "model: opus" not in out


def test_chat_a_failing_command_sends_nothing(lm):
    script(lm, [Text("ok"), Usage()])
    code, out, err = lm("chat", stdin="/mode wild explain X\n/quit\n")
    assert "error: unknown mode 'wild'" in err and lm.fake.requests == []


def test_chat_unknown_words_and_double_slashes_are_text(lm):
    script(lm, [Text("a"), Usage()], [Text("b"), Usage()], [Text("c"), Usage()], [Text("d"), Usage()])
    lm("chat", stdin="how goes /nope this\n//model opus\nread /etc/hosts\nrun `x /model opus y` now\n")
    assert sent(lm) == ["how goes /nope this", "/model opus", "read /etc/hosts", "run `x /model opus y` now"]


def test_chat_the_commands_of_a_line_come_first_only_first(lm):
    script(lm, [Text("ok"), Usage()])
    code, out, err = lm("chat", stdin="explain /tools\n/tools\n/skill\n/quit\n")
    assert "must come first" in err and lm.fake.requests == []
    assert "tools: " in out and "skills (always on" in out                         # alone, they work as before
    code, out, err = lm("chat", stdin="/skill example-skill and more\n")
    assert sent(lm)[0].startswith('Use the skill "example-skill"') and sent(lm)[0].endswith("\n\nand more")


def test_chat_tab_completes_a_slash_word_where_readline_exists(lm, monkeypatch):
    class Readline:                                              # stands in for the module (there is none on Windows)
        __doc__ = "GNU readline"
        buf, end = "", 0
        get_line_buffer = lambda self: self.buf                  # noqa: E731
        get_endidx = lambda self: self.end                       # noqa: E731
        set_completer = lambda self, fn: setattr(self, "fn", fn)                # noqa: E731
        set_completer_delims = lambda self, d: setattr(self, "delims", d)       # noqa: E731
        parse_and_bind = lambda self, b: setattr(self, "bound", b)              # noqa: E731
    rl = Readline()
    monkeypatch.setitem(sys.modules, "readline", rl)
    monkeypatch.setattr(cli, "_tty", lambda stream: True)
    cli._tab(Home(lm.tree))
    assert rl.delims == " \t\n" and rl.bound == "tab: complete"

    def tab(buf: str, end: int, text: str) -> list[str]:
        rl.buf, rl.end = buf, end
        out = [rl.fn(text, n) for n in range(6)]
        return [x for x in out if x is not None]
    assert tab("compare with /run", 18, "/run") == ["/run-tracking "]
    assert tab("/mo", 3, "/mo") == ["/mode ", "/model "]
    assert tab("see a/ru", 8, "a/ru") == [] and tab("`x /ru y`", 6, "/ru") == []            # no slash word, or in code
    monkeypatch.setitem(sys.modules, "readline", None)                                      # no readline: nothing happens
    cli._tab(Home(lm.tree))


def test_chat_a_failed_answer_does_not_run_the_command_that_stood_last(lm):
    from lightmind.providers.claude_sdk import NotLoggedIn
    from test_loop_support import SdkFake
    state = {"fail": True}

    class Flaky(SdkFake):
        async def turn(self, req):
            if state["fail"]:
                state["fail"] = False
                raise NotLoggedIn("claude-abo")
            async for ev in super().turn(req):
                yield ev
    lm.fake.__class__ = Flaky
    script(lm, [Text("back again"), Usage()])
    code, out, err = lm("chat", stdin="hi /quit\nagain\n")                    # no answer, so no /quit: the chat goes on
    assert code == 0 and "not logged in" in err and "back again" in out and sent(lm) == ["again"]


def test_chat_a_refused_line_is_put_back_where_readline_exists(lm, monkeypatch):
    class Readline:                                              # stands in for the module
        inserted, hook = [], None
        insert_text = lambda self, text: self.inserted.append(text)             # noqa: E731
        def set_startup_hook(self, fn=None):                                    # noqa: E301
            self.hook = fn
    rl = Readline()
    with monkeypatch.context() as mp:
        mp.setitem(sys.modules, "readline", rl)
        mp.setattr(cli, "_tty", lambda stream: True)
        cli._prefill("/effort super x")
        assert rl.hook is not None and rl.inserted == []         # it waits for the next prompt ...
        rl.hook()
        assert rl.inserted == ["/effort super x"] and rl.hook is None        # ... fills it once and goes
    put = []
    monkeypatch.setattr(cli, "_prefill", put.append)
    script(lm, [Text("ok"), Usage()])
    lm("chat", stdin="/effort super x\n/mode wild y\n/unknown\n/quit\n")
    assert put == ["/effort super x", "/mode wild y", "/unknown"] and lm.fake.requests == []     # each refusal puts its line back
