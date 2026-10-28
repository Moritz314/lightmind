"""Live smoke test against a real API account (costs a fraction of a cent).  Skipped by default.

Needs a real tree with the account switched on and its key in secrets/.  Run by hand:

    LIGHTMIND_HOME=<tree> LM_LIVE_ACCOUNT=anthropic-api LM_LIVE_MODEL=claude-haiku-5-5 \
    UV_PROJECT_ENVIRONMENT=<venv> uv run --extra dev pytest -m live -s tests/test_provider_api_live.py
"""
import os

import pytest

from lightmind import loop as L
from lightmind.files import Home
from lightmind.model import Text, Usage

pytestmark = pytest.mark.live


async def test_one_short_turn_on_a_real_api_account():
    home = Home(Home.resolve_root(None))
    s = L.open_session(home, account=os.environ.get("LM_LIVE_ACCOUNT", "anthropic-api"),
                       model=os.environ.get("LM_LIVE_MODEL", "claude-haiku-5-5"), effort="low")
    evs = [e async for e in s.send("Reply with the single word: pong")]
    s.close()
    assert "pong" in "".join(e.text for e in evs if isinstance(e, Text)).lower()
    assert any(isinstance(e, Usage) and e.output > 0 for e in evs)
