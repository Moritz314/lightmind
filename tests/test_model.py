import json
from dataclasses import asdict

from lightmind.model import (Caps, Rule, Run, Text, ToolCall, Tokens, Usage)
from lightmind.providers.base import Provider
from lightmind.providers.fake import FakeProvider, call, say
from lightmind.model import Request


def test_rule_text():
    assert Rule("bash", "git push*").text() == "bash(git push*)"
    assert Rule("category").text() == "category"


def test_run_roundtrip():
    r = Run(id="x", agent="a", tokens=Tokens(1, 2, 3, 4), cost_usd=0.5)
    r2 = Run.from_dict(json.loads(json.dumps(r.to_dict())))
    assert r2 == r


def test_run_from_dict_ignores_unknown():
    assert Run.from_dict({"id": "x", "future": 1}).id == "x"


def test_tokens_add():
    t = Tokens()
    t.add(Usage(input=5, output=2, cache_read=1))
    t.add(Usage(input=1))
    assert asdict(t) == {"input": 6, "output": 2, "cache_read": 1, "cache_write": 0}


def test_caps_frozen_defaults():
    assert Caps().tools and Caps().max_context == 200_000


async def test_fake_provider_scripted_and_protocol():
    fp = FakeProvider([say("hi"), call("bash", {"command": "ls"})])
    assert isinstance(fp, Provider)
    ev1 = [e async for e in fp.turn(Request(system="s"))]
    ev2 = [e async for e in fp.turn(Request(system="s"))]
    ev3 = [e async for e in fp.turn(Request(system="s"))]
    assert ev1[0] == Text("hi") and isinstance(ev1[1], Usage)
    assert ev2[0] == ToolCall("c1", "bash", {"command": "ls"})
    assert ev3[0] == Text("") and len(fp.requests) == 3
    assert (await fp.usage()).account == "fake"
