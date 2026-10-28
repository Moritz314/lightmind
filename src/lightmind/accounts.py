"""What each provider account is and whether it is ready: `lm accounts`, the TUI accounts view.

Reads the provider files, checks that a login or a key file EXISTS and reads the limits file the
chats keep in the state folder.  Never opens a credentials file and never returns a key.

May import: model, files, secrets, providers.base, providers._api_common, providers.claude_sdk (path helper only).
"""
from __future__ import annotations

import json
from typing import Any

from . import secrets as _secrets
from .files import Home
from .model import Account
from .providers.base import ADAPTERS, ProviderError

CREDENTIALS_FILE = ".credentials.json"          # what the Claude CLI writes into its config dir at login
KEY_VARS = {"anthropic-api": ("ANTHROPIC_API_KEY",), "openrouter": ("OPENROUTER_API_KEY",),
            "openai": ("OPENAI_API_KEY",), "openai-compatible": ("API_KEY", "OPENAI_API_KEY"),
            "openai-compat": ("API_KEY", "OPENAI_API_KEY")}
# states: ready | login-missing | key-missing | no-key-needed | no-adapter | config-error


def default_var(ref_or_type: str) -> str:
    """Variable name `lm secret set` uses when none is given: by secret folder or account type."""
    head = ref_or_type.partition("/")[0].casefold()
    return {"anthropic": "ANTHROPIC_API_KEY", "anthropic-api": "ANTHROPIC_API_KEY",
            "openrouter": "OPENROUTER_API_KEY", "openai": "OPENAI_API_KEY"}.get(head, "API_KEY")


def login_present(home: Home, acct: Account) -> bool | None:
    """Subscription login: does the credentials file of the account's config dir exist?  None when
    the account has no config_dir (it then runs on the system default login)."""
    from .providers.claude_sdk import account_config_dir
    cfg = account_config_dir(home, acct)
    return None if cfg is None else (cfg / CREDENTIALS_FILE).is_file()


def _key_present(home: Home, acct: Account) -> bool:
    try:
        env = _secrets.provider_env(home, acct.secret)
    except (OSError, ValueError):
        return False
    names = KEY_VARS.get(acct.type, ("API_KEY",))
    return any(env.get(n) for n in names) or len(env) == 1 and bool(next(iter(env.values())))


def _limits(home: Home, name: str) -> tuple[float | None, float | None, Any, str]:
    try:
        data = json.loads((home.path("state") / f"limits-{name}.json").read_text("utf-8"))
        lim = data.get("limits", {})
        five, seven = lim.get("five_hour"), lim.get("seven_day")
        return (float(five["utilization"]) if five else None, float(seven["utilization"]) if seven else None,
                [r.get("resets_at") for r in (five, seven) if r], str(data.get("seen", "")))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None, None, [], ""


def account_status(home: Home, name: str) -> dict[str, Any]:
    """One account as plain data (JSON-safe).  `hint` is the one line telling what to do next, or ''."""
    acct = home.load_providers().accounts[name]
    row: dict[str, Any] = {"name": name, "type": acct.type, "default_model": acct.default_model,
                           "use": acct.use, "fallback": list(acct.fallback), "state": "ready", "hint": ""}
    try:
        if acct.type == "claude-subscription":
            row["config_dir"] = acct.config_dir
            has = login_present(home, acct)
            token = bool(acct.secret) and _secrets_exists(home, acct.secret)
            row["logged_in"] = bool(has) or token if has is not None else None
            if has is None and not token:
                row["state"], row["hint"] = "config-error", f"no config_dir in providers/{name}.md; add `config_dir: system/claude/{name}`"
            elif not (has or token) and has is not None:
                row["state"], row["hint"] = "login-missing", f"not logged in: lm login {name}"
        elif acct.type not in ADAPTERS:
            row["state"] = "no-adapter"
        else:
            row["secret"] = acct.secret
            if not acct.secret:
                local = acct.type in ("openai-compatible", "openai-compat")
                row["key_present"] = False
                row["state"] = "no-key-needed" if local else "key-missing"
                if not local:
                    row["hint"] = f"key missing: no `secret:` in providers/{name}.md"
            else:
                row["key_present"] = _key_present(home, acct)
                if not row["key_present"]:
                    row["state"], row["hint"] = "key-missing", f"key missing: lm secret set {acct.secret}"
    except (OSError, ValueError, ProviderError) as e:
        row["state"], row["hint"] = "config-error", str(e)
    five, seven, resets, seen = _limits(home, name)
    row.update(five_hour=five, seven_day=seven, resets_at=resets, limits_seen=seen)
    return row


def _secrets_exists(home: Home, ref: str) -> bool:
    try:
        return _secrets._env_path(home, ref).is_file()
    except ValueError:
        return False


def list_accounts(home: Home) -> list[dict[str, Any]]:
    cfg = home.load_providers()
    return [account_status(home, n) for n in sorted(cfg.accounts, key=lambda n: (n != cfg.default, n))]


def hint(home: Home, name: str) -> str:
    """The setup hint of one account ('' when it is ready); never raises."""
    try:
        return account_status(home, name)["hint"]
    except Exception:                                                  # noqa: BLE001
        return ""


def set_account_key(home: Home, name: str, value: str) -> bool:
    """Store the API key of an account in the file its `secret:` names, like `lm secret set <that ref>` does
    (the TUI's key dialog uses it).  True when a value was replaced.  Errors name the account, never the value."""
    acct = home.load_providers().accounts.get(name)
    if acct is None:
        raise ValueError("unknown account")
    if not acct.secret:
        raise ValueError(f"account {name!r} has no `secret:` in its providers file")
    return _secrets.store_secret(home, acct.secret, default_var(acct.secret), value)
