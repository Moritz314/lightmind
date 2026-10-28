"""Anthropic Messages API over httpx (streaming SSE, no SDK).  Account type `anthropic-api`.

The adapter answers ONE model call per `turn()` (`owns_loop = False`): text arrives as it streams, tool
calls are yielded as ToolCall events and `loop.py` executes them and calls `turn()` again.

* Request mapping: `Request.tools` (already {name, description, input_schema}) go as `tools`;
  assistant messages become text + `tool_use` blocks, consecutive `tool` messages one user message of
  `tool_result` blocks.
* Caching: order is tools -> system prefix -> state.  The prefix block carries `cache_control`, so the
  tools and the master prompt are read from cache on every later turn; the volatile state block comes
  after it, uncached.
* Effort: sent as `output_config.effort` for models that take it (Claude 4.6+ / 5.x); `thinking` is never
  sent (current models think adaptively, `budget_tokens` is rejected there).  Older models: no effort.
* max_tokens: `max_tokens:` of the provider file, default 32000.  Base URL: `base_url:` of the file.
* Rate limits: the `anthropic-ratelimit-*` headers become one RateLimit event (highest utilization).

May import: model, files, secrets, providers.base.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, AsyncIterator

import httpx

from ..files import Home
from ..model import AccountUsage, Caps, Error, Event, RateLimit, Request, Text, ToolCall, Usage
from ._api_common import TIMEOUT, api_key, classify, mask, meta_of, ratio, sse

BASE_URL = "https://api.anthropic.com"
VERSION = "2023-06-01"
DEFAULT_MAX_TOKENS = 32_000
KEY_VARS = ("ANTHROPIC_API_KEY",)
# models that accept output_config.effort (fable/mythos/opus/sonnet/haiku 5.x, opus 4.6-4.8, sonnet 4.6)
_EFFORT_MODELS = re.compile(r"(fable|mythos|opus|sonnet|haiku)-5|opus-4-[678]|sonnet-4-6")


def to_api_messages(messages) -> list[dict[str, Any]]:
    """model.Message list -> Messages API `messages`."""
    out: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "tool":
            content: Any = m.text or "(no output)"
            if m.image:
                content = [{"type": "text", "text": content},
                           {"type": "image", "source": {"type": "base64", "media_type": m.image["media_type"],
                                                        "data": m.image["data"]}}]
            block = {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": content}
            if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list) \
                    and out[-1]["content"][-1].get("type") == "tool_result":
                out[-1]["content"].append(block)
            else:
                out.append({"role": "user", "content": [block]})
        elif m.role == "assistant":
            blocks: list[dict[str, Any]] = [{"type": "text", "text": m.text}] if m.text else []
            blocks += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.args or {}}
                       for c in m.tool_calls]
            if blocks:
                out.append({"role": "assistant", "content": blocks})
        elif (m.origin == "interject" and out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list)
                and out[-1]["content"][-1].get("type") == "tool_result"):
            out[-1]["content"].append({"type": "text", "text": m.text})     # an interjection rides in the user turn of the tool results
        else:
            out.append({"role": "user", "content": m.text or "(empty)"})
    return out


def mark_history(messages: list[dict[str, Any]]) -> None:
    """Rolling cache breakpoint: last content block of the second-to-last message, so each tool round
    reads the history so far from the cache (the newest message is the only new part)."""
    if len(messages) < 2:
        return
    m = messages[-2]
    if isinstance(m["content"], str):
        m["content"] = [{"type": "text", "text": m["content"]}]
    m["content"][-1]["cache_control"] = {"type": "ephemeral"}


def _reset(value: str) -> float | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def rate_limit(headers: Any) -> RateLimit | None:
    """Highest utilization of the anthropic-ratelimit-<kind>-limit/-remaining/-reset headers."""
    best: RateLimit | None = None
    for kind in ("requests", "tokens", "input-tokens", "output-tokens"):
        p = f"anthropic-ratelimit-{kind}-"
        u = ratio(headers.get(p + "remaining"), headers.get(p + "limit"))
        if u is not None and (best is None or u > best.utilization):
            best = RateLimit(kind.replace("-", "_"), u, _reset(headers.get(p + "reset", "")))
    return best


class AnthropicApiProvider:
    owns_loop = False

    def __init__(self, home: Home, account: str, dispatcher=None) -> None:
        accounts = home.load_providers().accounts
        if account not in accounts:
            raise ValueError(f"unknown account {account!r}")
        self.home, self.account, self.acct, self.dispatcher = home, account, accounts[account], dispatcher
        self.caps = Caps(tools=True, caching=True, images=False, max_context=200_000)
        self.context_tokens = 0
        self.transport: httpx.AsyncBaseTransport | None = None      # tests inject httpx.MockTransport
        self.max_tokens = int(meta_of(home, self.acct).get("max_tokens") or DEFAULT_MAX_TOKENS)

    def body(self, req: Request) -> dict[str, Any]:
        system: list[dict[str, Any]] = [{"type": "text", "text": req.system,
                                         "cache_control": {"type": "ephemeral"}}]
        if req.state:
            system.append({"type": "text", "text": req.state})
        messages = to_api_messages(req.messages)
        mark_history(messages)
        b: dict[str, Any] = {"model": req.model or self.acct.default_model, "max_tokens": self.max_tokens,
                             "stream": True, "system": system, "messages": messages}
        if req.tools:
            b["tools"] = [{"name": t["name"], "description": t.get("description", ""),
                           "input_schema": t["input_schema"]} for t in req.tools]
        if _EFFORT_MODELS.search(b["model"]) and req.effort:
            b["output_config"] = {"effort": req.effort}
        return b

    async def turn(self, req: Request) -> AsyncIterator[Event]:
        key = api_key(self.home, self.acct, KEY_VARS, required=True)
        url = (self.acct.base_url or BASE_URL).rstrip("/") + "/v1/messages"
        headers = {"x-api-key": key, "anthropic-version": VERSION, "content-type": "application/json"}
        use = Usage()
        blocks: dict[int, dict[str, Any]] = {}
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=TIMEOUT) as client:
                async with client.stream("POST", url, headers=headers, json=self.body(req)) as r:
                    if r.status_code != 200:
                        yield classify(r.status_code, (await r.aread()).decode("utf-8", "replace"), key, r.headers)
                        return
                    rl = rate_limit(r.headers)
                    if rl:
                        yield rl
                    async for ev, d in sse(r):
                        if ev == "message_start":
                            u = d.get("message", {}).get("usage", {})
                            use.input = u.get("input_tokens", 0)
                            use.cache_read = u.get("cache_read_input_tokens", 0)
                            use.cache_write = u.get("cache_creation_input_tokens", 0)
                            use.output = u.get("output_tokens", 0)
                        elif ev == "content_block_start":
                            cb = d.get("content_block", {})
                            blocks[d["index"]] = {"type": cb.get("type"), "id": cb.get("id"),
                                                  "name": cb.get("name"), "json": ""}
                        elif ev == "content_block_delta":
                            delta = d.get("delta", {})
                            if delta.get("type") == "text_delta" and delta.get("text"):
                                yield Text(delta["text"])
                            elif delta.get("type") == "input_json_delta":
                                blocks.setdefault(d["index"], {"json": ""})["json"] += delta.get("partial_json", "")
                        elif ev == "content_block_stop":
                            b = blocks.pop(d.get("index"), None)
                            if b and b.get("type") == "tool_use":
                                try:
                                    args = json.loads(b["json"] or "{}")
                                except ValueError:
                                    args = {}
                                yield ToolCall(b["id"], b["name"], args if isinstance(args, dict) else {})
                        elif ev == "message_delta":
                            use.output = d.get("usage", {}).get("output_tokens", use.output)
                        elif ev == "error":
                            e = d.get("error", {})
                            yield classify(529 if e.get("type") == "overloaded_error" else 500,
                                           json.dumps({"error": e}), key)
                            return
        except httpx.HTTPError as e:
            yield Error("other", mask(f"{type(e).__name__}: {e}", key))
            return
        self.context_tokens = use.input + use.cache_read + use.cache_write
        yield use

    async def usage(self) -> AccountUsage:
        return AccountUsage(account=self.account, est=True, note="pay per use; see runs for cost")
