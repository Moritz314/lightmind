"""Chat Completions over httpx (streaming SSE, no SDK): OpenRouter, OpenAI and local servers
(Ollama, LM Studio, llama.cpp ...) through `base_url`.  Account types `openrouter`, `openai`,
`openai-compatible` (alias `openai-compat`).

One model call per `turn()` (`owns_loop = False`); `loop.py` runs the tools.

* Tools go as function calling (`tools: [{type: function, function: {...}}]`); tool results are
  `role: tool` messages.  A model without tool support simply gets no `tools` when the request has none.
* Key: `OPENROUTER_API_KEY` / `OPENAI_API_KEY` / `API_KEY` (or the only variable of the secret file).
  Optional for a local server: no `secret:` -> no Authorization header.  `base_url` defaults to
  OpenRouter (type openrouter) or OpenAI.
* OpenRouter: `HTTP-Referer` / `X-Title` from `referer:` / `title:` of the provider file (optional);
  the prefix gets `cache_control` (Anthropic models cache it, others ignore it); effort goes as
  `reasoning.effort` (low | medium | high; xhigh and max are sent as high).
* Usage: prompt tokens minus cached (`prompt_tokens_details.cached_tokens`) and cache writes
  (`cache_write_tokens`, OpenRouter) = input, so prices apply per class.

May import: model, files, secrets, providers.base.
"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx

from ..files import Home
from ..model import AccountUsage, Caps, Error, Event, RateLimit, Request, Text, ToolCall, Usage
from ._api_common import TIMEOUT, api_key, classify, mask, meta_of, ratio, sse

BASE_URLS = {"openrouter": "https://openrouter.ai/api/v1", "openai": "https://api.openai.com/v1"}
KEY_VARS = ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "API_KEY")
REASONING = {"low": "low", "medium": "medium", "high": "high", "xhigh": "high", "max": "high"}


def to_api_messages(req: Request, cache_prefix: bool) -> list[dict[str, Any]]:
    """System (prefix + state) and the conversation as Chat Completions messages."""
    if cache_prefix:
        parts: list[dict[str, Any]] = [{"type": "text", "text": req.system,
                                        "cache_control": {"type": "ephemeral"}}]
        if req.state:
            parts.append({"type": "text", "text": req.state})
        out: list[dict[str, Any]] = [{"role": "system", "content": parts}]
    else:
        out = [{"role": "system", "content": req.system + (f"\n\n{req.state}" if req.state else "")}]
    for m in req.messages:
        if m.role == "tool":
            note = " (this provider cannot show pictures to the model)" if m.image else ""   # caps.images is False here
            out.append({"role": "tool", "tool_call_id": m.tool_call_id, "content": (m.text or "(no output)") + note})
        elif m.role == "assistant":
            msg: dict[str, Any] = {"role": "assistant", "content": m.text or None}
            if m.tool_calls:
                msg["tool_calls"] = [{"id": c.id, "type": "function",
                                      "function": {"name": c.name, "arguments": json.dumps(c.args or {})}}
                                     for c in m.tool_calls]
            if msg["content"] or m.tool_calls:
                out.append(msg)
        else:
            out.append({"role": "user", "content": m.text or "(empty)"})
    return out


def rate_limit(headers: Any) -> RateLimit | None:
    best: RateLimit | None = None
    for kind in ("requests", "tokens"):
        u = ratio(headers.get(f"x-ratelimit-remaining-{kind}"), headers.get(f"x-ratelimit-limit-{kind}"))
        if u is not None and (best is None or u > best.utilization):
            best = RateLimit(kind, u)
    return best


class OpenAICompatProvider:
    owns_loop = False

    def __init__(self, home: Home, account: str, dispatcher=None) -> None:
        accounts = home.load_providers().accounts
        if account not in accounts:
            raise ValueError(f"unknown account {account!r}")
        self.home, self.account, self.acct, self.dispatcher = home, account, accounts[account], dispatcher
        self.caps = Caps(tools=True, caching=self.acct.type == "openrouter", images=False, max_context=128_000)
        self.context_tokens = 0
        self.transport: httpx.AsyncBaseTransport | None = None      # tests inject httpx.MockTransport
        self.meta = meta_of(home, self.acct)
        base = self.acct.base_url or BASE_URLS.get(self.acct.type, "")
        if not base:
            raise ValueError(f"account {account!r} needs a `base_url:` in its provider file")
        self.base_url = base.rstrip("/")

    def body(self, req: Request) -> dict[str, Any]:
        router = self.acct.type == "openrouter"
        b: dict[str, Any] = {"model": req.model or self.acct.default_model, "stream": True,
                             "stream_options": {"include_usage": True},
                             "messages": to_api_messages(req, router)}
        if self.meta.get("max_tokens"):
            b["max_tokens"] = int(self.meta["max_tokens"])
        if req.tools:
            b["tools"] = [{"type": "function", "function": {
                "name": t["name"], "description": t.get("description", ""), "parameters": t["input_schema"]}}
                for t in req.tools]
        if router and req.effort in REASONING:
            b["reasoning"] = {"effort": REASONING[req.effort]}
        return b

    async def turn(self, req: Request) -> AsyncIterator[Event]:
        key = api_key(self.home, self.acct, KEY_VARS, required=self.acct.type in ("openrouter", "openai"))
        headers = {"content-type": "application/json"}
        if key:
            headers["authorization"] = f"Bearer {key}"
        if self.acct.type == "openrouter":
            for h, k in (("http-referer", "referer"), ("x-title", "title")):
                if self.meta.get(k):
                    headers[h] = str(self.meta[k])
        use = Usage()
        calls: dict[int, dict[str, str]] = {}
        try:
            async with httpx.AsyncClient(transport=self.transport, timeout=TIMEOUT) as client:
                async with client.stream("POST", self.base_url + "/chat/completions", headers=headers,
                                         json=self.body(req)) as r:
                    if r.status_code != 200:
                        yield classify(r.status_code, (await r.aread()).decode("utf-8", "replace"), key, r.headers)
                        return
                    rl = rate_limit(r.headers)
                    if rl:
                        yield rl
                    async for _, d in sse(r):
                        if d.get("error"):
                            e = d["error"]
                            code = e.get("code") if isinstance(e, dict) else 0
                            yield classify(code if isinstance(code, int) else 500, json.dumps({"error": e}), key)
                            return
                        if d.get("usage"):
                            u = d["usage"]
                            det = u.get("prompt_tokens_details") or {}
                            cr, cw = det.get("cached_tokens") or 0, det.get("cache_write_tokens") or 0
                            use = Usage(input=max(0, (u.get("prompt_tokens") or 0) - cr - cw),
                                        output=u.get("completion_tokens") or 0, cache_read=cr, cache_write=cw)
                        for ch in d.get("choices") or []:
                            delta = ch.get("delta") or {}
                            if delta.get("content"):
                                yield Text(delta["content"])
                            for tc in delta.get("tool_calls") or []:
                                c = calls.setdefault(tc.get("index", 0), {"id": "", "name": "", "args": ""})
                                fn = tc.get("function") or {}
                                c["id"] = tc.get("id") or c["id"]
                                c["name"] += fn.get("name") or ""
                                c["args"] += fn.get("arguments") or ""
        except httpx.HTTPError as e:
            yield Error("other", mask(f"{type(e).__name__}: {e}", key))
            return
        for i in sorted(calls):
            c = calls[i]
            try:
                args = json.loads(c["args"] or "{}")
            except ValueError:
                args = {}
            yield ToolCall(c["id"] or f"call_{i}", c["name"], args if isinstance(args, dict) else {})
        self.context_tokens = use.input + use.cache_read + use.cache_write
        yield use

    async def usage(self) -> AccountUsage:
        return AccountUsage(account=self.account, est=True, note="pay per use; see runs for cost")
