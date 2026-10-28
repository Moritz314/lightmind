"""Child process of tests/test_serve.py (no tests in here): `lm serve --stdio` with a scripted provider instead of a model.

    python serve_runner.py <script.json> <lm arguments...>

The script is a list of turns, or {"main": [turns], "sub": [turns]} when sub-agents (`spawn`) are used; the turns of "sub"
are played by every sub-agent in the order they ask.  The script is a list of turns; a turn is a list of steps: ["say", text] | ["deltas", [text, ...]] |
["call", name, args, id] | ["sleep", seconds].  A turn after the end of the script answers "ok"."""
from __future__ import annotations

import asyncio
import json
import sys

from lightmind import cli, loop, spawn
from lightmind import model as m
from lightmind.providers.fake import FakeProvider


class Scripted(FakeProvider):
    async def turn(self, req):
        self.requests.append(req)
        steps = self.script.pop(0) if self.script else [["say", "ok"]]
        for step in steps:
            if step[0] == "say":
                yield m.Text(step[1])
            elif step[0] == "deltas":
                for d in step[1]:
                    yield m.Text(d)
            elif step[0] == "call":
                yield m.ToolCall(step[3], step[1], step[2])
            elif step[0] == "sleep":
                await asyncio.sleep(step[1])
        yield m.Usage(input=10, output=5)


if __name__ == "__main__":
    raw = json.loads(open(sys.argv[1], encoding="utf-8").read())
    main_script, sub_script = (raw["main"], raw.get("sub", [])) if isinstance(raw, dict) else (raw, [])
    prov = Scripted(main_script)
    sub = Scripted(sub_script)

    def factory(home, account, dispatcher=None):
        prov.dispatcher = dispatcher
        return prov

    def sub_factory(home, account, dispatcher=None):
        sub.dispatcher = dispatcher
        return sub
    loop.make_provider = factory
    spawn.make_provider = sub_factory
    sys.exit(cli.main(sys.argv[2:]))
