"""Log in to a subscription and store an API key from inside the UI (accounts view, account card, /login, /key).

The login is the same function `lm login` runs, in the terminal with the UI suspended.  The key is typed into a
password field and handed straight to `accounts.set_account_key`: it is never shown, logged or put in a message."""
from __future__ import annotations

import os
import shlex
import subprocess
import sys

from rich.text import Text
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from ..accounts import account_status, set_account_key
from . import data
from .theme import AMBER, DIM


def needs(home, name: str) -> str:
    """'login' (subscription without login), 'key' (API account without key) or ''."""
    st = data.safe(lambda: account_status(home, name), {}).get("state")
    return {"login-missing": "login", "key-missing": "key"}.get(st, "")


def account_type(home, name: str) -> str | None:
    a = data.safe(lambda: home.load_providers().accounts.get(name))
    return a.type if a else None


def login(app, name: str) -> tuple[bool, str]:
    """Suspend the UI and run `providers.claude_sdk.login`; (ok, message)."""
    t = account_type(app.home, name)
    if t is None:
        return False, "login: unknown account"
    if t != "claude-subscription":
        return False, f"login: {name} is not a subscription account"
    from ..providers import claude_sdk
    try:
        with app.suspend():
            print(f"\nLightmind: logging in {name}. The browser opens; sign in there, then come back.\n", flush=True)
            rc = claude_sdk.login(app.home, name)
    except Exception as e:                                           # noqa: BLE001 - SuspendNotSupported, ProviderError, ...
        return False, f"login {name}: {data.clean(str(e) or type(e).__name__)}"
    return rc == 0, f"login {name}: " + ("done" if rc == 0 else f"the CLI ended with code {rc}")


def open_file(app, path) -> tuple[bool, str]:
    """Open a file in an editor; (ok, message).  `$VISUAL` / `$EDITOR` when one is set: it runs in this terminal with the UI
    suspended (the way on a server over ssh).  Else the program the system has for the file: Windows `startfile`, macOS
    `open`, Linux `xdg-open`, which is not waited for."""
    editor = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    try:
        if editor:
            with app.suspend():
                rc = subprocess.call([*shlex.split(editor, posix=os.name != "nt"), str(path)])
            return rc == 0, f"{path.name}: " + ("the editor was closed" if rc == 0 else f"the editor ended with code {rc}")
        if sys.platform == "win32":
            os.startfile(str(path))                                  # type: ignore[attr-defined]
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:                                           # noqa: BLE001 - no editor, SuspendNotSupported, a bad $EDITOR
        return False, f"{path.name}: no editor could be opened ({data.clean(str(e) or type(e).__name__)}); the file is {path}"
    return True, f"{path.name} is open in the editor of the system"


def remote(app, name: str) -> tuple[bool, str]:
    """Suspend the UI and open the permanent session of server `name` (ssh + tmux); (ok, message)."""
    from .. import remote as R
    try:
        with app.suspend():
            rc = R.open_remote(app.home, name)               # says once what it opens
    except Exception as e:                                           # noqa: BLE001 - RemoteError, ssh missing, SuspendNotSupported
        return False, f"server {name}: {data.clean(str(e) or type(e).__name__)}"
    return rc == 0, f"server {name}: " + ("session closed" if rc == 0 else f"ssh ended with code {rc}")


def store_key(app, name: str, value: str) -> tuple[bool, str]:
    try:
        replaced = set_account_key(app.home, name, value)
    except Exception as e:                                           # noqa: BLE001 - messages name the account, not the value
        return False, f"key {name}: {data.clean(str(e) or type(e).__name__)}"
    return True, f"key {name}: {'replaced' if replaced else 'stored'} (value not shown)"


def ask_key(app, name: str, after=None):
    """Open the key dialog for `name`; `after()` runs once the answer is handled."""
    if account_type(app.home, name) is None:
        return app.notify("key: unknown account", severity="error")
    a = app.home.load_providers().accounts[name]
    if not a.secret:
        return app.notify(f"key: account {name!r} has no `secret:` in its providers file", severity="error")

    def done(value):
        if value:
            ok, msg = store_key(app, name, value)
            app.notify(msg, severity="information" if ok else "error")
        if after:
            after()
    app.push_screen(KeyModal(name), done)


class KeyModal(ModalScreen):
    """A password field for the API key of one account.  Enter stores, Esc cancels."""
    BINDINGS = [("escape", "cancel", "cancel")]

    def __init__(self, name: str):
        super().__init__()
        self.account = name

    def compose(self):
        yield Vertical(Static(id="keyhead"), Input(password=True, placeholder="paste the key, it is not shown", id="keyin"),
                       Static(id="keyfoot"), id="keybox")

    def on_mount(self):
        self.query_one("#keyhead", Static).update(Text(f"API key for {self.account}\n", f"bold {AMBER}"))
        self.query_one("#keyfoot", Static).update(Text("\n⏎ store · Esc cancel · nothing is displayed or logged", DIM))
        self.query_one(Input).focus()

    def on_input_submitted(self, e: Input.Submitted):
        v, e.input.value = e.value.strip(), ""
        self.dismiss(v or None)

    def action_cancel(self):
        self.query_one(Input).value = ""
        self.dismiss(None)
