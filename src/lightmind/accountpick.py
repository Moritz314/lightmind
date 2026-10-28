"""Which provider account a run starts on when more than one is allowed: lists of accounts, "is it free", and the weekly start switch.

The same rule the interim tool `tools/account-picker` used, now in the core so every surface (chat, run, sub-agent) picks the same way.

May import: nothing at load time (`files` only for types; `accounts` is imported late because it imports `providers.base`).
"""
from __future__ import annotations

import json
import re
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:                  # files imports this module (for `as_text`), so the types only
    from .files import Home

WEEK_START_LIMIT = 0.95            # a new chat leaves its normal account when the 7-day window is at or over this (AN-766)
WINDOWS = ("five_hour", "seven_day")


def names_of(value: Any) -> list[str]:
    """The account names in a `provider` value: a name, `a, b` or `[a, b]` in a file, or a list.  Empty when nothing is set."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        parts = [str(v) for v in value]
    else:
        parts = re.split(r"[,\s]+", str(value).strip().strip("[]"))
    return [p.strip().strip("\"'") for p in parts if p.strip().strip("\"'")]


def as_text(value: Any) -> str:
    """A `provider` value as the one text the files store it in (`a, b`); a plain name stays as it is."""
    return ", ".join(names_of(value))


def _windows(home: Home, name: str) -> dict[str, tuple[float, float | None]]:
    """kind -> (utilization 0..1, reset time as seconds since 1970 or None) from the limits file the chats keep."""
    try:
        data = json.loads((home.path("state") / f"limits-{name}.json").read_text("utf-8"))
        out: dict[str, tuple[float, float | None]] = {}
        for kind, row in (data.get("limits") or {}).items():
            reset = row.get("resets_at")
            out[kind] = (float(row["utilization"]), float(reset) if reset else None)
        return out
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def window_over(home: Home, name: str, kind: str, limit: float, now: float | None = None) -> bool:
    """Is that window of the account at or over `limit` and not reset since?  An account that never ran here counts as not over."""
    got = _windows(home, name).get(kind)
    if got is None or got[0] < limit:
        return False
    return not (got[1] and got[1] <= (now or time.time()))


def usable(home: Home, name: str) -> bool:
    """Can the account run at all: a subscription that is logged in, an API account with its key.  A status that cannot be read counts as usable."""
    from .accounts import account_status
    try:
        return account_status(home, name)["state"] not in ("login-missing", "key-missing")
    except Exception:                                    # noqa: BLE001 - a status problem must not block a start
        return True


def free(home: Home, name: str, limit: float | None = None, now: float | None = None) -> tuple[bool, str]:
    """(free, reason) for one account: usable, and for a subscription neither window at or over `limit` (default: the
    switch point of the providers, 90 %).  A window stops counting once its reset time has passed."""
    limit = home.load_providers().switch_at if limit is None else limit
    if not usable(home, name):
        return False, "not logged in or no key"
    acct = home.load_providers().accounts.get(name)
    if acct is None:
        return False, "unknown account"
    if acct.type == "claude-subscription":
        for kind in WINDOWS:
            if window_over(home, name, kind, limit, now):
                return False, f"{kind} over {limit:.0%}"
    return True, "free"


def first_free(home: Home, names: list[str], limit: float | None = None) -> str:
    """The first account of the list that is free right now; when none is, the first one (it then runs into its limit and
    pauses like any run, instead of a start that fails)."""
    for name in names:
        if free(home, name, limit)[0]:
            return name
    return names[0]


def start_account(home: Home, name: str, chain: list[str]) -> str:
    """The account a NEW chat starts on: `name`, unless its 7-day window is at or over 95 %; then the first account of `chain`
    (its fallbacks) that is usable and under 95 % itself.  Nothing better: `name`.  `chain` is passed in so the caller decides
    what is allowed (an agent with a fixed provider passes none)."""
    if not window_over(home, name, "seven_day", WEEK_START_LIMIT):
        return name
    for alt in chain:
        if alt != name and usable(home, alt) and not window_over(home, alt, "seven_day", WEEK_START_LIMIT):
            return alt
    return name
