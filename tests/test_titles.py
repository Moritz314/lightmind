"""Chat titles: /rename and /describe, the manual lock, the update rule, cutting the model's answer, old meta.json.
The naming model is always replaced by a function: no real call."""
import json

import pytest

from lightmind import core, loop as L, runs, titles
from lightmind.model import Run, Text

from test_loop_session import events_of, fakes, script, shome  # noqa: F401 - fixtures
from test_loop_support import collect


def ask_with(title="Neue Seite", topics=("Seite", "Login")):
    calls = []

    def ask(home, account, model, text):
        calls.append((model, text))
        return json.dumps({"title": title, "topics": list(topics)})
    ask.calls = calls
    return ask


async def chat_turn(s, fakes, text="bau die Seite"):
    script(fakes["abo"], [Text("ok")])
    await collect(s.send(text))


def meta(s):
    return json.loads((s.run.path / "meta.json").read_text("utf-8"))


# ------------------------------------------------------------------ answer parsing and cutting
def test_parse_answer_cuts_title_and_topics():
    t, topics = titles.parse_answer('```json\n{"title": "one two three four five six seven.", "topics": ["a b c d e", "x"]}\n```')
    assert t == "one two three four five"
    assert topics[0] == "a b c" and topics[1] == "x"


def test_parse_answer_rejects_garbage():
    assert titles.parse_answer("no json here") is None
    assert titles.parse_answer('{"topics": ["a"]}') is None


def test_description_is_cut_to_whole_topics_at_100_chars():
    d = titles.description_of([f"topic{i} " + "x" * 20 for i in range(10)])
    assert len(d) <= titles.DESC_CHARS and d.count(titles.SEP) >= 1 and not d.endswith(titles.SEP)
    assert titles.description_of(["Seite", "Login"]) == "Seite · Login"


# ------------------------------------------------------------------ the update rule
def test_due_first_turn_then_every_n():
    assert not titles.due(0, 0, 3)
    assert titles.due(1, 0, 3)
    assert not titles.due(2, 1, 3) and not titles.due(3, 1, 3)
    assert titles.due(4, 1, 3)


def test_after_turn_starts_naming_on_rule(shome, fakes, monkeypatch):  # noqa: F811
    s = L.open_session(shome)
    s.auto_title = True
    shome.settings["sessions"] = {"title_every": 3}
    started = []
    monkeypatch.setattr(titles, "start", lambda sess, daemon: (started.append(sess._turns_done), setattr(sess, "_titled_at", sess._turns_done)))
    for _ in range(5):
        titles.after_turn(s)
    assert started == [1, 4]
    shome.settings["sessions"] = {}                              # the default: after each answer
    started.clear()
    for _ in range(2):
        titles.after_turn(s)
    assert started == [6, 7]


def test_no_naming_when_switched_off(shome, fakes, monkeypatch):  # noqa: F811
    s = L.open_session(shome)                                   # auto_title stays False: like `lm run`
    started = []
    monkeypatch.setattr(titles, "start", lambda *a, **k: started.append(1))
    titles.after_turn(s)
    titles.at_close(s)
    assert started == []


async def test_update_writes_meta_and_calls_listener(shome, fakes):  # noqa: F811
    s = L.open_session(shome)
    await chat_turn(s, fakes)
    seen = []
    s.on_title.append(lambda t, d: seen.append((t, d)))
    ask = ask_with()
    assert titles.update(s, ask) is True
    assert (meta(s)["title"], meta(s)["description"]) == ("Neue Seite", "Seite · Login")
    assert seen == [("Neue Seite", "Seite · Login")]
    assert "bau die Seite" in ask.calls[0][1] and ask.calls[0][0] == "haiku"
    assert titles.update(s, ask) is False                       # same answer: nothing changed, no event
    assert len(seen) == 1


# ------------------------------------------------------------------ slash commands and the manual lock
async def test_rename_and_describe_lock_against_automatic(shome, fakes):  # noqa: F811
    s = L.open_session(shome)
    await chat_turn(s, fakes)
    assert "Mein Titel" in s.rename("Mein Titel")
    assert s.describe("Meine Liste").endswith("Meine Liste")
    m = meta(s)
    assert (m["title"], m["title_manual"], m["description"], m["description_manual"]) == ("Mein Titel", True, "Meine Liste", True)
    ask = ask_with()
    assert titles.update(s, ask) is False and ask.calls == []   # both by hand: not even a call
    s.run.run.description_manual = False
    s.run.run.description = ""
    assert titles.update(s, ask) is True
    assert s.title == "Mein Titel" and s.description == "Seite · Login"


async def test_empty_text_goes_back_to_automatic(shome, fakes):  # noqa: F811
    s = L.open_session(shome)
    await chat_turn(s, fakes)
    s.rename("Hand")
    s.rename("")
    assert meta(s)["title_manual"] is False and meta(s)["title"] == ""
    titles.update(s, ask_with())
    assert s.title == "Neue Seite"


async def test_rename_during_the_call_wins(shome, fakes):  # noqa: F811
    s = L.open_session(shome)
    await chat_turn(s, fakes)

    def ask(home, account, model, text):
        s.rename("Hand")                                        # arrives while the model thinks
        return json.dumps({"title": "Auto", "topics": ["x"]})
    titles.update(s, ask)
    assert s.title == "Hand"


def test_core_command_routes_rename(shome, fakes):  # noqa: F811
    s = L.open_session(shome)
    r = core.command(shome, s, "rename", "Titel hier")
    assert r.ok and s.title == "Titel hier"
    assert core.command(shome, s, "describe", "A · B").ok and s.description == "A · B"
    assert "rename" in core.SERVE_SLASH and "describe" in core.SERVE_SLASH


# ------------------------------------------------------------------ old meta.json, lists
def test_old_meta_without_title_fields_loads():
    r = Run.from_dict({"id": "20260101-000000-chat-aaaa"})
    assert (r.title, r.description, r.title_manual, r.description_manual) == ("", "", False, False)


async def test_title_shows_in_session_list(shome, fakes):  # noqa: F811
    s = L.open_session(shome)
    await chat_turn(s, fakes, "erste Nachricht")
    assert runs.list_sessions(shome)[0].title == "erste Nachricht"      # fallback: first words
    s.rename("Hand")
    row = runs.list_sessions(shome)[0]
    assert row.title == "Hand"
    s.describe("A · B")
    assert runs.list_sessions(shome)[0].description == "A · B"


def test_settings_defaults_and_bad_values(shome):
    assert titles.settings(shome) == {"auto_title": True, "title_model": "haiku", "title_every": 1}
    shome.settings["sessions"] = {"title_every": "x", "auto_title": False}
    assert titles.settings(shome)["title_every"] == 1 and titles.settings(shome)["auto_title"] is False


# ------------------------------------------------------------------ naming after every answer
def test_every_answer_names_by_default():
    assert titles.DEFAULTS["title_every"] == 1
    assert [titles.due(n, n - 1, 1) for n in (2, 3, 4)] == [True, True, True]
