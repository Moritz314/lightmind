"""Helpers shared by the HTTP API adapters (anthropic_api, openai_compat).  May import: files, model,
secrets, providers.base.  The API key is read here, per turn, from `secrets.provider_env` only; it is
never logged, never put into an event and is masked out of every error text."""
from __future__ import annotations

import json
from typing import Any, AsyncIterator

import httpx

from .. import secrets as _secrets
from ..files import Home
from ..model import Account, Error
from .base import ProviderError

TIMEOUT = httpx.Timeout(600.0, connect=15.0)


def api_key(home: Home, acct: Account, names: tuple[str, ...], required: bool) -> str:
    """Key of the account.  `names` = accepted variable names; a file with exactly one variable is
    accepted under any name.  No `secret:` configured -> "" when not required (local server)."""
    if not acct.secret:
        if required:
            raise ProviderError(f"account {acct.name!r} has no `secret:` in {acct.path or 'its provider file'}: "
                                f"set it to the secret holding {names[0]}", "auth")
        return ""
    try:
        env = _secrets.provider_env(home, acct.secret)
    except (FileNotFoundError, OSError, ValueError) as e:
        raise ProviderError(f"no API key for account {acct.name!r}: create the secret {acct.secret!r} "
                            f"(secrets/{acct.secret}.env with {names[0]}=<key>)", "auth") from e
    key = next((env[n] for n in names if env.get(n)), "") or (
        next(iter(env.values()), "") if len(env) == 1 else "")
    if not key:
        raise ProviderError(f"secret {acct.secret!r} of account {acct.name!r} has no {names[0]}=<key> line", "auth")
    return key


def mask(text: str, key: str) -> str:
    return text.replace(key, "***") if key else text


def classify(status: int, body: str, key: str = "", headers: Any = None) -> Error:
    """HTTP failure -> Error event (vocabulary of model.Error)."""
    msg, etype = body.strip()[:500], ""
    try:
        d = json.loads(body)
        e = d.get("error", d) if isinstance(d, dict) else {}
        if isinstance(e, dict):
            msg, etype = str(e.get("message") or msg), str(e.get("type") or e.get("code") or "")
    except ValueError:
        pass
    low = (msg + etype).lower()
    if status == 429 or "rate_limit" in etype:
        kind = "rate_limit"
    elif status in (529, 503) or "overloaded" in etype:
        kind = "overloaded"
    elif status == 402 or "credit balance" in low or ("insufficient" in low and "credit" in low):
        kind = "billing_error"
    elif status in (401, 403) or "authentication" in etype or "permission" in etype:
        kind = "auth"
    else:
        kind = "other"
    ra = headers.get("retry-after") if headers is not None else None
    return Error(kind, mask(f"HTTP {status}: {msg}" + (f" (retry after {ra}s)" if ra else ""), key))


async def sse(resp: httpx.Response) -> AsyncIterator[tuple[str, dict]]:
    """Server-sent events -> (event name, JSON data).  Comments and non-JSON data (`[DONE]`) skipped."""
    name = ""
    async for line in resp.aiter_lines():
        if line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            try:
                d = json.loads(line[5:].strip())
            except ValueError:
                continue
            if isinstance(d, dict):
                yield name or str(d.get("type", "")), d
        elif not line:
            name = ""


def ratio(remaining: Any, limit: Any) -> float | None:
    try:
        return max(0.0, min(1.0, 1 - float(remaining) / float(limit)))
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def meta_of(home: Home, acct: Account) -> dict:
    """Frontmatter of the account's provider file (keys the Account dataclass does not carry)."""
    try:
        return dict(home.doc(acct.path).meta) if acct.path else {}
    except OSError:
        return {}
