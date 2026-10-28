"""CLI: `lm`.  Handlers are thin: parse arguments, call the modules, print.

    lm                         the Orbit TUI
    lm init [dir] [--vault-repo URL] | update [dir] | login <account> | accounts | secret set|list
    lm chat [agent] [-p project] [-m mode] [--model M] [--account A] [--effort E]
    lm run [task] [-a agent] ...   one run: task from the argument or stdin, only the final text on stdout
                               (--detach: start it as its own process and print the run id; --max-usd,
                               --max-turns, --schema file.json: limits and a JSON answer;
                               --name N / --continue <id|name> [--create]: a fixed, named session)
    lm wait <run id>... [--timeout S]   block until those runs ended, print their results
    lm runs [list|show|cost]   run history and cost
    lm rules [agent] [-m mode] resolved rules with origin
    lm skill add <name> [--from SRC] [--force] | list [--available]   fetch a skill from a skills repository / list skills
    lm archive <chat> | list | restore <chat>   keep a chat out of the lists (a marker file; nothing is moved)
    lm node --stdio | --uplink | --relay NAME | targets   the node of Remote Control (the phone app), see node.py

`lm run` exit codes: 0 done, 1 failed or not logged in (or the answer never fit --schema), 2 usage or
configuration error (also: deeper than run.max_depth), 3 paused (limit, max turns, handover) or budget
reached (also: a --continue session that stayed open elsewhere for 10 min), 130 interrupted.  `lm wait`: 0 all done, 1 one failed, 3 one paused or over budget, 124 timeout.

May import: everything.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
import unicodedata
from contextlib import aclosing
from pathlib import Path

from . import slash, version_text
from .files import Home
from .model import Error, Text, ToolCall
from .providers.base import Notice, ProviderError, ToolDone
from .util import brief

TUI_MISSING = "the Orbit screen needs the tui extra (pip install 'lightmind[tui]'); use `lm chat` meanwhile"


class Usage(Exception):
    """A usage or configuration error: message to stderr, exit 2."""


def build_parser() -> argparse.ArgumentParser:
    def add_globals(parser: argparse.ArgumentParser, absent) -> None:
        parser.add_argument("--home", default=absent, help="tree root (default $LIGHTMIND_HOME or ~/lightmind)")
        parser.add_argument("--json", action="store_true", default=absent, help="machine-readable output")

    common = argparse.ArgumentParser(add_help=False)       # the global flags also work after the command;
    add_globals(common, argparse.SUPPRESS)                 # there they only count when given
    p = argparse.ArgumentParser(prog="lm", description="Lightmind")
    add_globals(p, None)
    p.set_defaults(json=False)
    p.add_argument("--version", action="version", version=version_text())
    sub = p.add_subparsers(dest="cmd", metavar="<command>")

    def cmd(name: str, help: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help, parents=[common])

    def session_flags(s: argparse.ArgumentParser) -> None:
        s.add_argument("-p", "--project")
        s.add_argument("-m", "--mode", help="a mode (a file in modes/)")
        s.add_argument("--model", help="a model of the account, or a tier")
        s.add_argument("--account", help="a providers/<name>.md account or alias")
        s.add_argument("--effort", help="low | medium | high | xhigh | max | ultracode (max + every substantial task as a workflow)")

    s = cmd("init", "create a tree from the shipped templates")
    s.add_argument("dir", nargs="?")
    s.add_argument("--vault-repo", help="git repository to clone as the vault")
    s = cmd("update", "add template files that are new since the tree was made")
    s.add_argument("dir", nargs="?")
    s.add_argument("--from", dest="source", metavar="FOLDER",
                   help="dev: build a wheel from the COMMITTED code (git archive HEAD) of this lightmind-core folder, keep it in system/wheels and build a new runtime from it")
    s.add_argument("--no-build", action="store_true", help="do not build the new runtime now (the next start does)")
    s.add_argument("--after-build", action="store_true", help=argparse.SUPPRESS)
    s = cmd("login", "log in a Claude subscription account once (opens the browser)")
    s.add_argument("account")
    s = cmd("open", "open a chat straight away, without the start menu: a chat id, `new`, or an agent (the tabs and windows lm opens run this)")
    s.add_argument("ref", help="a chat id (a unique prefix is enough), `new` for a new chat, or an agent name: a new chat with that agent")
    s.add_argument("-a", "--agent", help="with new: the agent of the chat")
    s.add_argument("-p", "--project", help="with new or an agent: the project of the chat")
    s.add_argument("--hold", action="store_true", help="after a failure wait for Enter before the window closes (what lm itself starts uses it)")
    s.add_argument("-n", "--name", metavar="NAME", help="with new: the name of the new chat ([A-Za-z0-9._-], up to 64 characters)")
    s.add_argument("--rc", nargs="?", const="", metavar="NAME", help="switch Remote Control on in the chat as soon as it is open (as /rc NAME does; the phone app starts chats this way)")
    s = cmd("q", "quick chat: a chat at once on this device in this folder, no start menu (defaults: config.md `quick:`; also `lm -q`, `lmq`)")
    s.add_argument("-a", "--agent", help="an agent of the tree (default: none)")
    session_flags(s)
    s.add_argument("--context", help="context limit, e.g. 200k or 1m")
    s.add_argument("-n", "--name", metavar="NAME", help="a fixed session name (default: none, the chat is named from its content like every chat)")
    s.add_argument("--dry-run", action="store_true", help="print what would open (as JSON) and open nothing")
    s.add_argument("--hold", action="store_true", help=argparse.SUPPRESS)
    s = cmd("resume", "go on in an earlier chat with its history and settings: the resume menu (a list of the last chats), a name or id, or --last (also `lm --resume`, `lmr`)")
    s.add_argument("ref", nargs="?", help="a session name, or a chat id (a unique prefix is enough); without it the resume menu to choose from")
    s.add_argument("--last", action="store_true", help="the chat used last, without the list")
    s.add_argument("--archived", action="store_true", help="choose from the archive; the chat you pick is restored and opened")
    s.add_argument("--dry-run", action="store_true", help="print the chat that would open (as JSON) and open nothing")
    s.add_argument("--hold", action="store_true", help=argparse.SUPPRESS)
    s = cmd("remote", "open the permanent session on a server of servers/ (ssh + tmux), see servers/README.md")
    s.add_argument("server")
    cmd("accounts", "every provider account: type, login or key state, last known limits")
    s = cmd("secret", "store an API key without it appearing anywhere: `secret set <name/account> [VAR]`, `secret list`")
    s.add_argument("action", choices=["set", "list"])
    s.add_argument("ref", nargs="?", help="name/account, e.g. anthropic/api-1")
    s.add_argument("var", nargs="?", help="variable name (default by service: ANTHROPIC_API_KEY, OPENROUTER_API_KEY)")
    s = cmd("sync", "tree content and encrypted secrets between instances: `sync` (pull + push) | status | init <url> | join <url> | add-device <name> <age1...> | remove-device <name> | approve <name> [fingerprint] | resolve <name/account> --keep local|remote")
    s.add_argument("action", nargs="?", default="run", choices=["run", "status", "init", "join", "add-device", "remove-device", "approve", "resolve"])
    s.add_argument("args", nargs="*", help="init|join: <url>; add-device: <name> <key>; remove-device: <name>; approve: <name> [fingerprint]; resolve: <name/account>")
    s.add_argument("--keep", choices=["local", "remote"], help="resolve: which version stays")
    s.add_argument("--machine", help="init|join: name of this instance (default: the host name)")
    s = cmd("chat", "interactive chat in the terminal, without the TUI")
    s.add_argument("agent", nargs="?")
    session_flags(s)
    s.add_argument("--context", help="context limit, e.g. 200k or 1m")
    s.add_argument("-n", "--name", metavar="NAME", help="the session name ([A-Za-z0-9._-], up to 64 characters)")
    s = cmd("run", "one run: the task is the argument or stdin; stdout holds only the final text")
    s.add_argument("task", nargs="?", help="the task; `-` or nothing: read stdin")
    s.add_argument("-a", "--agent", help="an agent of the tree (default: none)")
    session_flags(s)
    s.add_argument("--allow", action="append", default=[], metavar="RULE", help="session rule, e.g. 'bash(git status*)'")
    s.add_argument("--deny", action="append", default=[], metavar="RULE")
    s.add_argument("--detach", action="store_true", help="start the run as its own process and print the run id at once")
    s.add_argument("--max-usd", type=float, metavar="X", help="stop between turns when this run's cost reaches X (status budget)")
    s.add_argument("--max-turns", type=int, metavar="N", help="stop after N turns (status paused, max_turns)")
    s.add_argument("--schema", metavar="FILE", help="the final answer must be JSON that fits this JSON schema file")
    s.add_argument("--run-id", help=argparse.SUPPRESS)       # internal: a detached run was named by its starter
    s.add_argument("--name", metavar="NAME", help="give the new session this unique name ([A-Za-z0-9._-], up to 64 characters)")
    s.add_argument("--continue", dest="continue_", metavar="ID|NAME",
                   help="go on in an existing session (same history, agent, account, model); waits up to 10 min while it is open elsewhere")
    s.add_argument("--create", action="store_true", help="with --continue: make the session, named like that, when it does not exist yet")
    s = cmd("wait", "block until runs have ended and print their results")
    s.add_argument("run_ids", nargs="+", metavar="run_id", help="run ids (a unique prefix is enough)")
    s.add_argument("--timeout", type=float, metavar="S", help="give up after S seconds (exit 124, unfinished runs show status running)")
    s = cmd("wake", "continue a session headless with the messages waiting in its inbox (timers, the watch); one turn, then it ends")
    s.add_argument("run", help="a session name or a run id")
    s = cmd("triggers", "triggers that fire on a clock: `tick` (what the OS calls every minute), `install` / `uninstall` the OS beat, `status`, `list`")
    s.add_argument("action", choices=["tick", "install", "uninstall", "status", "list"])
    s = cmd("runs", "run history and cost")
    s.add_argument("action", nargs="?", default="list", choices=["list", "show", "cost"])
    s.add_argument("run_id", nargs="?")
    s.add_argument("-n", "--limit", type=int, default=20)
    s.add_argument("--since", help="cost: 24h, 7d, 30m or an ISO time")
    s.add_argument("--by", default="agent", help="cost: agent, model, account, project, server or status")
    s = cmd("rules", "resolved rules of all layers with origin")
    s.add_argument("agent", nargs="?")
    s.add_argument("-m", "--mode")
    s = cmd("serve", "the backend of the VS Code extension: JSON lines over stdin/stdout (plan/PROTOCOL.md)")
    s.add_argument("--stdio", action="store_true", help="talk over stdin and stdout (the only transport)")
    s.add_argument("--workdir", metavar="FOLDER", help="the folder the editor has open: the working folder of the chats")
    s = cmd("archive", "archive a chat (it leaves the resume list), `archive list` shows the archive, `archive restore <chat>` brings one back")
    s.add_argument("words", nargs="+", metavar="chat | list | restore <chat>", help="a chat id (a unique prefix is enough) or session name; or `list`; or `restore` and a chat")
    s = cmd("skill", "skills from a skills repository: `skill add <name>` (examples: programming, delegation, inquisition, browser, triggers, ultracode, skill-creator), `skill list` (`--available`: what the source offers)")
    s.add_argument("action", choices=["add", "list"])
    s.add_argument("name", nargs="?", help="add: the skill to fetch")
    s.add_argument("--from", dest="source", default="", metavar="SRC", help="git URL, repository path or folder with skills/ (default: skills.source of config.md, else the Lightmind skills repository)")
    s.add_argument("--force", action="store_true", help="add: replace an installed skill (the old one is kept in the state folder)")
    s.add_argument("--available", action="store_true", help="list: the skills the source offers instead of those of the tree")
    s = cmd("node", "the node of Remote Control: the gateway of the phone app talks to it (--stdio), or a laptop reaches the gateway through it (--uplink)")
    s.add_argument("action", nargs="?", choices=["targets"], help="targets: the machines the gateway may start a node on, as JSON lines with --json")
    s.add_argument("--stdio", action="store_true", help="talk over stdin and stdout (started by the gateway)")
    s.add_argument("--uplink", action="store_true", help="connect out to the gateway over ssh (for a machine the gateway cannot reach)")
    s.add_argument("--relay", metavar="NAME", help="on the gateway machine: join stdin/stdout to the gateway socket (what --uplink starts over ssh)")
    cmd("check", "health checklist of the tree (the boot screen's list); exit 1 when something failed")
    cmd("lint", "validate the files of the tree: the agent files so far (each reads, its project exists); exit 1 on a failure")
    return p


_CTRL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")      # C0 and C1 controls except \n and \t


def _tty(stream) -> bool:
    try:
        return stream.isatty()
    except (AttributeError, ValueError):
        return False


def _p(text: str, err: bool = False) -> None:
    """Print.  On a terminal the control characters go: model text, tool output and hook lines could
    otherwise carry escape sequences that rewrite the screen.  Into a pipe the text stays as it is."""
    stream = sys.stderr if err else sys.stdout
    print(_CTRL.sub("", text) if _tty(stream) else text, file=stream, flush=True)


_mid = False        # stdout stands in the middle of a streamed line


def _w(text: str) -> None:
    """Write assistant text without a newline (API providers stream many small deltas), cleaned
    of control characters on a terminal like `_p`."""
    global _mid
    if not text:
        return
    sys.stdout.write(_CTRL.sub("", text) if _tty(sys.stdout) else text)
    sys.stdout.flush()
    _mid = not text.endswith("\n")


def _nl() -> None:
    """End a streamed line before anything else is printed."""
    global _mid
    if _mid:
        sys.stdout.write("\n")
        sys.stdout.flush()
        _mid = False


def _out(a, data, text: str) -> None:
    _p(json.dumps(data, indent=1, ensure_ascii=False) if a.json else text)


def _brief(args: dict) -> str:
    return brief(args, 70)


def _err(text: str) -> None:
    _p(text, err=True)


# ------------------------------------------------------------------ init, update, login
def _tree_summary(created: list[str]) -> str:
    top: dict[str, int] = {}
    for path in created:
        k = path.split("/", 1)[0] + ("/" if "/" in path else "")
        top[k] = top.get(k, 0) + 1
    return "\n".join(f"  {k}" + (f" ({n} files)" if k.endswith("/") else "") for k, n in sorted(top.items()))


def cmd_init(a) -> int:
    from .init import init_tree
    r = init_tree(a.dir or a.home, vault_repo=a.vault_repo)
    for w in r.warnings:
        _err(f"warning: {w}")
    acct = Home(r.root).defaults.get("provider")
    _out(a, {"root": str(r.root), "created": r.created, "vault": r.vault, "warnings": r.warnings},
         f"created {len(r.created)} files in {r.root}\n{_tree_summary(r.created)}"
         + (f"\nnext: lm login {acct}   (once), then: lm" if acct else ""))
    _offer_tick(Home(r.root), a)
    return 0


def _offer_tick(home: Home, a) -> None:
    """Timers and the watch need the OS to call `lm triggers tick` every minute: offered here, asked on a terminal only."""
    from . import timers
    if a.json:
        return
    try:
        if timers.installed(home):
            return
    except OSError:
        return
    _err("timers and the watch over sub-runs need a beat every minute: `lm triggers install` sets it up")
    if _tty(sys.stdin) and _tty(sys.stdout):
        try:
            if input("set it up now? [y/N] ").strip().lower() in ("y", "yes"):
                _err(f"the tick runs every minute through {timers.install(home)}")
        except (EOFError, RuntimeError, OSError, subprocess.SubprocessError) as e:
            _err(f"not set up: {e}")


def cmd_update(a) -> int:
    """Bring the tree up to date: template files and launchers, then a new runtime.  `--from FOLDER` builds the wheel
    first.  The new runtime is built at once (the launcher's `build`), next to the running ones, so the next window
    opens on the new version; when that fails or is skipped the next start builds it.  When a new runtime stands, it
    runs the update once more (`--after-build`) for the template files and launchers of the NEW version."""
    from . import runtime
    from .init import install_from_folder, update_tree
    target = a.dir or a.home
    if not (a.after_build or a.no_build):
        try:
            blockers = runtime.rebuild_blockers(Home(Home.resolve_root(target)))
        except Exception:                              # noqa: BLE001 - a tree that is not set up yet has nothing to protect
            blockers = []
        if blockers:                                   # before anything is changed: the old launcher would delete a runtime that is in use
            raise Usage(f"{len(blockers)} lm process(es) still run from the old runtime (pid {', '.join(map(str, blockers[:8]))}) "
                        "and this tree's launcher would delete it while building the new one. Close those lm windows and run "
                        "`lm update` again, or use `lm update --no-build` and close them before the next start.")
    data: dict = {}
    lines: list[str] = []
    created = None
    if a.source and not a.after_build:
        try:
            wheel, _stamp = install_from_folder(a.source, target)
        except (FileNotFoundError, ValueError, RuntimeError) as e:
            raise Usage(str(e)) from None
        data["wheel"] = str(wheel)
        lines.append(f"built {wheel.name} from the committed code of {a.source}")
    else:
        created = update_tree(target, schedule_rebuild=not a.after_build)
    home = Home(Home.resolve_root(target))
    rc = 0
    if not (a.after_build or a.no_build):
        res = runtime.build_now(home)
        data["build"] = {"status": res.status, "detail": res.detail}
        if res.status == "failed":
            lines.append(f"the new runtime could not be built ({res.detail}); the next start tries again")
            rc = 1
        elif res.status == "skipped":
            lines.append(f"no runtime built now ({res.detail}); the next start builds it")
        elif (cur := runtime.current(home)) is not None and cur.resolve() != runtime.own().resolve():
            lines.append(f"runtime {cur.name} is ready; windows opened from now on use it, running ones stay on their version")
            followed = _follow_up_update(home)
            if followed is not None:
                created = followed
        if res.status != "built" and a.source:
            lines.append("run `lm update` after the next start to add template files the new code ships")
    if created is not None:
        data["created"] = list(created)
        notes, launchers = list(getattr(created, "notes", [])), list(getattr(created, "launchers", []))
        if notes:
            data["notes"] = notes
        if launchers:
            data["launchers"] = launchers
        head = f"added {len(created)} new template files" + "".join(f"\n  {c}" for c in created) + "".join(f"\n{n}" for n in notes)
        lines.insert(0, head + (f"\nlaunchers updated: {', '.join(launchers)}" if launchers else ""))
    _out(a, data, "\n".join(lines))
    if not a.source and not a.after_build:
        try:
            _offer_tick(Home.open(target), a)
        except FileNotFoundError:
            pass
    return rc


def _follow_up_update(home: Home):
    """Run `lm update --after-build` in the newest runtime, so template files and launchers come from the new version.
    Returns what it created (a list with `.notes` and `.launchers`), None when it could not run."""
    from . import runtime
    from .init import Created
    try:
        r = subprocess.run([runtime.python_for_new_process(home), "-m", "lightmind.cli", "--home", str(home.root), "--json",
                            "update", "--after-build"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
        got = json.loads(r.stdout) if r.returncode == 0 else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if not isinstance(got, dict):
        return None
    created = Created(got.get("created", []))
    created.notes, created.launchers = got.get("notes", []), got.get("launchers", [])
    return created


def cmd_login(home: Home, a) -> int:
    from .providers.base import resolve_account
    from .providers.claude_sdk import login
    try:
        resolve_account(home, requested=a.account)
    except ValueError as e:
        raise Usage(str(e)) from None
    return login(home, a.account)


def cmd_remote(home: Home, a) -> int:
    from .remote import RemoteError, open_remote, remote_command, server_of
    try:
        if a.json:                                       # only show what would run
            _out(a, {"command": remote_command(server_of(home, a.server))}, "")
            return 0
        return open_remote(home, a.server)
    except RemoteError as e:
        raise Usage(str(e)) from None
    except FileNotFoundError:
        raise Usage("ssh is not installed on this machine") from None


def _pct(v) -> str:
    return "-" if v is None else f"{v * 100:.0f}%"


def cmd_accounts(home: Home, a) -> int:
    from .accounts import list_accounts
    rows = list_accounts(home)
    lines = []
    for r in rows:
        login = ("logged in" if r.get("logged_in") else "NOT logged in") if r["type"] == "claude-subscription" else (
            "key present" if r.get("key_present") else "KEY MISSING" if r["state"] == "key-missing" else "")
        lines.append(f"{r['name']:<18}{r['type']:<20}{r['default_model'] or '-':<20}{login:<14}"
                     f"5h {_pct(r['five_hour']):<5} 7d {_pct(r['seven_day']):<5}"
                     + (f"-> {', '.join(r['fallback'])}" if r["fallback"] else ""))
        if r["hint"]:
            lines.append(f"  {r['hint']}")
    _out(a, rows, "\n".join(lines) or "no accounts: add a file to providers/")
    return 0


def _read_value() -> str:
    """The secret value: hidden prompt on a terminal (asked twice), one line from a pipe otherwise."""
    if _tty(sys.stdin):
        import getpass
        v = getpass.getpass("value (input is hidden): ")
        if v and getpass.getpass("again: ") != v:
            raise Usage("the two entries differ; nothing was stored")
        return v
    return sys.stdin.readline().rstrip("\r\n")


def cmd_secret(home: Home, a) -> int:
    from . import secrets as S
    from .accounts import default_var
    if a.action == "list":
        use = {}
        for n, acct in home.load_providers().accounts.items():
            for r in (acct.secret, acct.admin_secret):
                if r:
                    use.setdefault(S.canon_ref(r), []).append(n)
        rows = [{"ref": r, "vars": v, "used_by": use.get(S.canon_ref(r), [])} for r, v in S.secret_vars(home).items()]
        _out(a, rows, "\n".join(f"{x['ref']:<28}{', '.join(x['vars']) or '-':<24}"
                                + (f"account {', '.join(x['used_by'])}" if x["used_by"] else "") for x in rows)
             or "no secrets stored")
        return 0
    if not a.ref:
        raise Usage("usage: lm secret set <name/account> [VAR]")
    var = a.var or default_var(a.ref)
    try:
        value = _read_value()
        replaced = S.store_secret(home, a.ref, var, value)
    except ValueError as e:
        raise Usage(str(e)) from None            # these messages name the ref or variable, never the value
    _out(a, {"ref": a.ref, "var": var, "replaced": replaced},
         f"{'replaced' if replaced else 'stored'} {var} in {a.ref} (value not shown)")
    return 0


# --------------------------------------------------------------------------- sync
def cmd_sync(home: Home, a) -> int:
    from . import sync
    if a.action == "approve":                              # the fingerprint is optional: without it the device is only shown
        if len(a.args) not in (1, 2):
            raise Usage("lm sync approve <name> [fingerprint]: the device, and to confirm it the fingerprint of its key")
    else:
        need = {"init": 1, "join": 1, "add-device": 2, "remove-device": 1, "resolve": 1}.get(a.action, 0)
        if len(a.args) != need:
            raise Usage(f"lm sync {a.action}: " + {1: "one argument", 2: "two arguments (name, key)", 0: "no arguments"}[need])
    if a.action == "run":
        r = sync.run(home)
        _out(a, {"status": r.status, "lines": r.lines, "warnings": r.warnings}, r.text())
        return 1 if r.status in ("conflict", "warn", "auth") else 0
    if a.action == "resolve" and not a.keep:
        raise Usage("lm sync resolve <name/account> --keep local|remote")
    lines = {"status": lambda: sync.status(home), "init": lambda: sync.init(home, a.args[0], a.machine),
             "join": lambda: sync.join(home, a.args[0], a.machine),
             "add-device": lambda: sync.add_device(home, *a.args), "remove-device": lambda: sync.remove_device(home, a.args[0]),
             "approve": lambda: sync.approve(home, a.args[0], a.args[1] if len(a.args) > 1 else None),
             "resolve": lambda: sync.resolve(home, a.args[0], a.keep)}[a.action]()
    _out(a, lines, "\n".join(lines))
    return 0


# --------------------------------------------------------------------------- check
def cmd_check(home: Home, a) -> int:
    from . import checks
    lines = checks.run_all(home)
    color = _tty(sys.stdout) and not os.environ.get("NO_COLOR")
    _out(a, [{"status": x.status, "label": x.label, "detail": x.detail} for x in lines], checks.format_plain(lines, color))
    return 1 if checks.worst(lines) == checks.FAIL else 0


def cmd_wake(home: Home, a) -> int:
    """`lm wake <session>`: the messages in the inbox of a session that no process holds become one turn of that session
    (same history, agent and account), then the process ends.  A session that is held by a process with a screen is
    left to it: that process reads its inbox itself."""
    import argparse
    from . import runs, wake
    from .loop import resume_session
    try:
        rid = runs.resolve_session(home, a.run)
    except ValueError as e:
        raise Usage(str(e)) from None
    if rid is None:
        raise Usage(f"no session {a.run!r}")
    if not wake.pending(home, rid):
        return 0
    try:
        s = resume_session(home, rid)
    except (ValueError, NotImplementedError, OSError) as e:
        raise Usage(f"wake {a.run}: {e}") from None
    if s.readonly_by:                                    # somebody holds it: they read the inbox
        s.close()
        return 0
    entries = wake.drain(home, rid)
    if not entries:
        s.hand_over()
        return 0
    cfg = _run_cfg(home)
    from .providers.base import settings
    wcfg = settings(home).get("watch")
    wcfg = wcfg if isinstance(wcfg, dict) else {}
    ns = argparse.Namespace(max_turns=int(wcfg.get("wake_max_turns") or 8), max_usd=None, json=False)
    if not _slot(home, rid, cfg):
        s.hand_over()
        wake.restore(home, rid, entries)                 # back into the inbox: the next try may have a slot
        return 3
    t0 = time.time()
    try:
        code = _run(home, ns, wake.frame(entries), None, rid, cfg, s, origin="subagent")
    except BaseException:
        wake.restore(home, rid, entries)                 # the turn did not happen: nothing is lost
        raise
    finally:
        runs.release_slot(home, rid)
    _reply_telegram(home, rid, wcfg, t0)
    return code


def _reply_telegram(home: Home, rid: str, wcfg: dict, since: float = 0.0) -> None:
    """The answer of a woken turn of a Telegram session goes to the user on Telegram: nobody reads a headless run's stdout."""
    from . import runs, wake, watch
    names = wcfg.get("reply_telegram", [])
    names = [names] if isinstance(names, str) else [str(n) for n in names or []]
    if not any(runs.find_name(home, n) == rid for n in names):
        return
    f = home.path("runs") / rid / "result.json"
    try:
        if f.stat().st_mtime < since:                    # the turn wrote no result: that file is an older turn's
            return
        text = str(json.loads(f.read_text("utf-8")).get("text") or "").strip()
    except (OSError, ValueError):
        return
    if text and text.rstrip(".! ").lower() != wake.REPLY_NOTHING:
        watch.send(home, "telegram", text[:3500])


def cmd_triggers(home: Home, a) -> int:
    from . import timers
    if a.action == "tick":
        rep = timers.tick(home)
        _out(a, {"fired": rep.fired, "retired": rep.retired, "errors": rep.errors, "skipped": rep.skipped},
             "\n".join([*(f"fired {x}" for x in rep.fired), *(f"ended {x}" for x in rep.retired), *(f"error {x}" for x in rep.errors)]))
        return 1 if rep.errors else 0
    if a.action == "install":
        try:
            way = timers.install(home)
        except (RuntimeError, OSError, subprocess.SubprocessError) as e:
            raise Usage(f"could not set up the beat: {e}") from None
        note = " (a systemd user timer runs only while you are logged in; `loginctl enable-linger` keeps it running)" if way == "systemd" else ""
        _out(a, {"installed": way}, f"the tick runs every minute through {way}{note}")
        return 0
    if a.action == "uninstall":
        way = timers.uninstall(home)
        _out(a, {"removed": way}, f"removed the {way} beat" if way else "no beat was installed")
        return 0
    if a.action == "status":
        way = timers.installed(home)
        _out(a, {"installed": way}, f"the tick runs through {way}" if way else "no beat is installed: `lm triggers install`")
        return 0 if way else 1
    rows = [{"name": i.name, "when": i.when, "kind": i.kind or "file", "session": i.session, "until": i.until, "error": i.error}
            for i in timers.load_items(home)]
    _out(a, rows, "\n".join(f"{r['name']}  {r['when'] or '-'}  {r['kind']}" + (f"  -> {r['session']}" if r["session"] else "")
                           + (f"  until {r['until']}" if r["until"] else "") + (f"  ERROR {r['error']}" if r["error"] else "") for r in rows) or "no triggers")
    return 0


def cmd_lint(home: Home, a) -> int:
    from . import checks
    lines = checks.lint_all(home)
    color = _tty(sys.stdout) and not os.environ.get("NO_COLOR")
    _out(a, [{"status": x.status, "label": x.label, "detail": x.detail} for x in lines], checks.format_plain(lines, color))
    return 1 if checks.worst(lines) == checks.FAIL else 0


# --------------------------------------------------------------------------- the TUI
def _migrate(home: Home) -> None:
    """A tree older than config.md: guard it in permissions.md and fill it from lightmind.md.  Only for a person starting
    lm at the top level, never in a run an agent started; what was done is said on stderr."""
    if _env_depth() > 0 or os.environ.get("LM_RUN"):
        return
    from .init import migrate_config
    try:
        for note in migrate_config(home):
            _err(note)
    except Exception as e:                                   # noqa: BLE001 - the start goes on, and says why this did not run
        _err(f"config.md: not prepared ({e.__class__.__name__}: {e})")


def cmd_tui(home: Home) -> int:
    _migrate(home)
    boot = False
    if _tty(sys.stdout) and not os.environ.get("LM_NO_BOOT"):
        from . import checks
        if checks.boot_mode(home) != "false":
            boot = checks.start(home)                    # the checks run while Textual is imported: most are done at the first paint
    try:
        from .tui.app import main as tui_main
    except ModuleNotFoundError as e:
        if not (e.name or "").startswith(("textual", "lightmind.tui")):
            raise                                        # some other import broke: not "the extra is missing"
        _err(TUI_MISSING)
        return 2
    return tui_main(home, boot=boot)


def _hold(a) -> None:
    """`--hold`: a window that lm opened itself closes with the process, so after a failure it waits for Enter first."""
    if getattr(a, "hold", False) and _tty(sys.stdin) and _tty(sys.stdout):
        try:
            input("Enter closes this window ")
        except (EOFError, KeyboardInterrupt):
            pass


def _person_only(what: str) -> None:
    """The commands that open the Orbit screen are for a person at a terminal."""
    if _env_depth() > 0 or os.environ.get("LM_RUN"):
        raise Usage(f"{what} is for a person at a terminal, not for a run an agent started")
    if not (_tty(sys.stdin) and _tty(sys.stdout)):
        raise Usage(f"{what} needs a terminal")


def _check_name(home: Home, name: str) -> None:
    """A session name given with -n: valid and not taken (a taken name would only be found out when the chat is open)."""
    from . import runs
    if not runs.valid_name(name):
        raise Usage(f"--name: {name!r} is no session name (letters, digits, . _ - and at most 64 characters)")
    if runs.find_name(home, name):
        raise Usage(f"--name: a session called {name!r} exists; continue it with `lm resume {name}`")


def _open_spec(home: Home, a) -> dict:
    """What `lm open` opens: `{"id": <chat id>}` or `{"new": {"agent": ..., "project": ...}}`.  Everything typed is checked
    first (ids and names are `[A-Za-z0-9._-]`), and an agent never starts it."""
    from . import launch, runs
    for what, v in (("chat or agent", a.ref), ("agent", a.agent), ("project", a.project)):
        if v and not launch.SAFE.fullmatch(v):
            raise Usage(f"{what} {v!r}: only letters, digits, . _ - are allowed, and it starts with a letter or digit")
    _person_only("lm open")
    if a.rc and not launch.SAFE.fullmatch(a.rc):
        raise Usage(f"--rc {a.rc!r}: only letters, digits, . _ - are allowed, and it starts with a letter or digit")
    rc = {} if a.rc is None else {"rc": a.rc}                  # the app switches Remote Control on once the chat is open
    if a.ref.lower() == "new":
        new = {"agent": a.agent or "", "project": a.project or ""}
        if a.name is not None:
            _check_name(home, a.name)
            new["name"], new["name_fixed"] = a.name, True
        return {"new": new, **rc}
    if a.name is not None:
        raise Usage("--name only goes with `new` (a chat that exists keeps its name)")
    hits = runs.find_runs(home, a.ref)
    if len(hits) == 1:
        if a.agent or a.project:
            raise Usage("--agent and --project only go with `new` (a chat that exists keeps its own)")
        return {"id": hits[0].name, **rc}
    if len(hits) > 1:
        raise Usage(f"{a.ref!r} fits {len(hits)} chats: give more of the id")
    if a.ref in home.list_agents() and not a.agent:
        return {"new": {"agent": a.ref, "project": a.project or ""}, **rc}
    raise Usage(f"no chat starts with {a.ref!r}, and there is no agent of that name")


def cmd_open(home: Home, a) -> int:
    """`lm open <chat id|new|agent> [-a agent] [-p project] [--hold]`: the Orbit screen straight in one chat.  No start
    checklist and no menu, so Esc in the chat ends the app.  The tabs and windows that `sessions.open` asks for run this
    (`launch.py`); a chat that another process has open shows read only, with the hint."""
    try:
        spec = _open_spec(home, a)
    except Usage as e:
        _err(f"lm: {e}")
        _hold(a)
        return 2
    return _run_direct(home, a, spec)


def _run_direct(home: Home, a, spec: dict) -> int:
    """The Orbit screen straight in the chat `spec` names (`Orbit(direct=...)`); no start menu below it."""
    try:
        from .tui.app import main as tui_main
    except ModuleNotFoundError as e:
        if not (e.name or "").startswith(("textual", "lightmind.tui")):
            raise
        _err(TUI_MISSING)
        _hold(a)
        return 2
    code = tui_main(home, boot=False, open=spec)
    if code:
        _hold(a)
    return code


def _quick_spec(home: Home, a) -> dict:
    """What `lm q` opens: a new chat on this device (no remote) in the current folder, with the settings of
    `quick.quick_settings` (flags, else `quick:` of config.md, else the built-in values) and the usual account choice.  It has a
    name only when `-n` gives one."""
    from . import launch, quick
    for what, v in (("agent", a.agent), ("project", a.project)):
        if v and not launch.SAFE.fullmatch(v):
            raise Usage(f"{what} {v!r}: only letters, digits, . _ - are allowed, and it starts with a letter or digit")
    try:
        st = quick.quick_settings(home, model=a.model, effort=a.effort, context=a.context, mode=a.mode, agent=a.agent)
    except ValueError as e:
        raise Usage(f"quick: {e}") from None
    if a.name is not None:
        _check_name(home, a.name)
    new = {"agent": a.agent or "", "project": a.project or "", "account": a.account or "", "model": st["model"] or "",
           "effort": st["effort"] or "", "context": st["context"] or "", "mode": st["mode"] or "", "workdir": os.getcwd()}
    if a.name is not None:                  # no default name: title and description come from the content, as in every chat
        new["name"], new["name_fixed"] = a.name, True       # a name given with -n is kept, never renamed from the content
    return {"new": new, "label": "lm q"}


def cmd_q(home: Home, a) -> int:
    """`lm q [-n name] [flags]`: the quick chat.  Nothing is asked: the chat opens on this device in the current folder
    with the settings of `quick:` (config.md), and Esc leaves it like a chat from `lm open`."""
    try:
        spec = _quick_spec(home, a)
        if a.dry_run:
            _p(json.dumps(spec["new"], indent=1, ensure_ascii=False))
            return 0
        _person_only("lm q")
    except Usage as e:
        _err(f"lm: {e}")
        _hold(a)
        return 2
    return _run_direct(home, a, spec)


def _resume_spec(home: Home, a) -> dict:
    """What `lm resume` opens: `{"id": <run id>}`, or without a name, id or --last `{"screen": "resume"}`: the resume menu of
    the start menu (`ResumeScreen`), the same screen and code; with `--archived` it starts on the archive tab.  The chat keeps
    its own settings (model, effort, mode, device, account); `loop.resume_session` reads them from the trusted record, together
    with the id of the SDK conversation.  An archived chat that is asked for (by name or id) is restored first: wanting to go on
    in it means wanting it back in the list; `--dry-run` changes nothing."""
    from . import resume, runs
    if a.ref and a.last:
        raise Usage("give a chat or --last, not both")
    if a.ref:
        try:
            rid = resume.lookup(home, a.ref)
        except ValueError as e:
            raise Usage(str(e)) from None
    elif a.last:
        rid = resume.latest(home)
        if rid is None:
            raise Usage("there is no chat to resume yet")
    else:
        if not a.dry_run:
            _person_only("lm resume")
        return {"screen": "resume", **({"archived": True} if getattr(a, "archived", False) else {}), "label": "lm resume"}
    if not a.dry_run and runs.is_archived(home.path("runs") / rid):
        _p(f"{rid}: {runs.unarchive(home, rid)} (it was in the archive)")
    return {"id": rid, "label": "lm resume"}


def cmd_resume(home: Home, a) -> int:
    """`lm resume [name|id] [--last]`: the Orbit screen in an earlier chat, with its history; without an argument the resume
    menu of the start menu (also `lmr`)."""
    try:
        spec = _resume_spec(home, a)
        if a.dry_run and "screen" in spec:
            _p(json.dumps({k: spec[k] for k in ("screen", "archived") if k in spec}))
            return 0
        if a.dry_run:
            from . import runs
            rec = runs.read_trusted(home, spec["id"]) or {}
            keys = ("model_req", "effort", "mode", "server", "account", "workdir", "sdk_session_id")
            _p(json.dumps({"id": spec["id"], **{k: rec.get(k, "") for k in keys}}, indent=1, ensure_ascii=False))
            return 0
        _person_only("lm resume")
    except Usage as e:
        _err(f"lm: {e}")
        _hold(a)
        return 2
    return _run_direct(home, a, spec)


# --------------------------------------------------------------------------- sessions
def _open(home: Home, a, **kw):
    from .loop import open_session
    try:
        return open_session(home, agent=a.agent, project=a.project, account=a.account, model=a.model,
                            effort=a.effort, mode=a.mode, context=getattr(a, "context", None), **kw)
    except (ValueError, NotImplementedError, OSError) as e:
        raise Usage(str(e)) from None


def _start(home: Home, s) -> None:
    from . import sync
    from .loop import run_on_start
    sync.start_pull(home)                                # a sync repo: pull in the background; silent otherwise
    for line in run_on_start(home, s):
        _err(line)


def _rules(a):
    from .rules import parse_rule
    try:
        return [parse_rule(t, eff, origin="session:cli") for eff, texts in (("allow", a.allow), ("deny", a.deny))
                for t in texts]
    except ValueError as e:
        raise Usage(str(e)) from None


async def _run_once(s, task: str, origin: str = "") -> str:
    """The final answer: the text after the last tool call.  Notices and errors go to stderr."""
    parts: list[str] = []
    async with aclosing(s.send(task, origin=origin)) as events:         # closing the iterator ends the CLI process
        async for ev in events:
            if isinstance(ev, ToolCall):
                parts.clear()
            elif isinstance(ev, Text):
                parts.append(ev.text)
            elif isinstance(ev, Notice):
                _err(f"[{ev.kind}] {ev.text}")
            elif isinstance(ev, Error):
                _err(f"error ({ev.kind}): {ev.message}")
    return "".join(parts)


SCHEMA_RETRIES = 3          # corrections asked for when the answer does not fit --schema


def _run_cfg(home: Home) -> dict:
    from .providers.base import settings
    cfg = settings(home).get("run")
    return cfg if isinstance(cfg, dict) else {}


def _env_depth() -> int:
    try:
        return max(int(os.environ.get("LM_DEPTH") or 0), 0)
    except ValueError:
        return 0


def _load_schema(path: str | None):
    if not path:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            schema = json.loads(f.read())
    except (OSError, ValueError) as e:
        raise Usage(f"--schema {path}: {e}") from None
    if not isinstance(schema, dict):
        raise Usage(f"--schema {path}: a JSON schema is an object")
    return schema


async def _run_task(s, task: str, schema, origin: str = "") -> tuple[str, object, str]:
    """(final text, parsed answer, error).  With a schema the answer is checked and, when it does not fit,
    corrected in the same session up to SCHEMA_RETRIES times; `error` is the last problem when it never fit."""
    from .schema import check_answer
    if schema:
        task += ("\n\nAnswer with one JSON value and nothing else, valid against this JSON schema:\n"
                 + json.dumps(schema, ensure_ascii=False))
    text = await _run_once(s, task, origin)
    if not schema:
        return text, None, ""
    for n in range(SCHEMA_RETRIES + 1):
        if not s.last_result or s.last_result.status != "done":
            return text, None, ""                        # a limit or an error ended it: that is the outcome
        value, errs = check_answer(text, schema)
        if not errs:
            return text, value, ""
        if n == SCHEMA_RETRIES:
            return text, None, "; ".join(errs[:4])
        text = await _run_once(s, "Your answer does not fit the schema:\n- " + "\n- ".join(errs[:8])
                               + "\nAnswer again with only the corrected JSON.")
    return text, None, ""


def _name_session(home: Home, s, name: str) -> None:
    """Give the session just opened its name; when another one got it first, the session ends and the call fails."""
    from . import runs
    try:
        runs.claim_name(home, name, s.id)
    except ValueError as e:
        s.close("failed", str(e))
        raise Usage(str(e)) from None
    s.run.set_name("title", name)                         # a name given with -n is the title, and the automatic naming leaves it


def _end_unstarted(home: Home, rid: str, status: str, reason: str) -> None:
    """A detached run whose process could not start its session (or wait for a slot): close the run folder
    its starter made, so `lm wait` does not wait for it."""
    from . import runs
    from .model import Run
    try:
        d = runs.resolve_run(home, rid)
        rd = runs.RunDir(home, Run.from_dict(json.loads((d / "meta.json").read_text("utf-8"))))
    except (FileNotFoundError, OSError, ValueError):
        return
    rd.write_result({"run_id": rid, "status": status, "exit_reason": reason, "text": "",
                     "cost_usd": 0.0, "cost_est": False})
    rd.finish(status, reason)


SLOT_POLL = 1.0             # seconds between two looks for a free slot


def _slot(home: Home, rid: str, cfg: dict) -> bool:
    """Wait for one of run.max_parallel slots (shared by all processes of this tree through the state folder)."""
    from . import runs
    limit = int(cfg.get("max_parallel") or 0)
    end = time.monotonic() + runs.duration(cfg.get("slot_timeout"), 600)
    while not runs.claim_slot(home, rid, limit):
        if time.monotonic() >= end:
            return False
        time.sleep(SLOT_POLL)
    return True


def _detach(home: Home, a, task: str) -> int:
    """Start this run as a process of its own that outlives this one and print its id.  The run folder exists
    when the id is printed (status running, no process yet), so `lm wait` can start at once; the task goes
    into `brief.md` and from there to the child's stdin; its stdout and stderr to `stdout.txt` / `stderr.txt`."""
    from . import runs
    from .model import Run
    rid = runs.new_run_id(a.agent or "")
    rd = runs.RunDir(home, Run(id=rid, agent=a.agent or "", project=a.project or "", status="running",
                               parent=os.environ.get("LM_PARENT", ""), depth=_env_depth()))
    (rd.path / "brief.md").write_text(task, "utf-8")
    from . import runtime
    argv = [runtime.python_for_new_process(home), "-m", "lightmind.cli", "--home", str(home.root), "--json", "run", "-", "--run-id", rid]
    for flag, v in (("-a", a.agent), ("-p", a.project), ("-m", a.mode), ("--model", a.model), ("--account", a.account),
                    ("--effort", a.effort), ("--max-usd", a.max_usd), ("--max-turns", a.max_turns), ("--name", a.name),
                    ("--schema", os.path.abspath(a.schema) if a.schema else None)):
        if v is not None:
            argv += [flag, str(v)]
    for flag, rules in (("--allow", a.allow), ("--deny", a.deny)):
        for r in rules:
            argv += [flag, r]
    from .proc import popen_detached
    with open(rd.path / "brief.md", "rb") as fin, open(rd.path / "stdout.txt", "wb") as fout, \
            open(rd.path / "stderr.txt", "wb") as ferr:
        try:
            popen_detached(argv, cwd=str(home.root), stdin=fin, stdout=fout, stderr=ferr)
        except OSError as e:
            _end_unstarted(home, rid, "failed", f"could not start: {e}")
            raise Usage(f"could not start the run: {e}") from None
    if rd.run.parent:
        from . import wake, watch
        if wake.verify_parent(home, rd.run.parent, os.environ.get("LM_PARENT_SIG", "")):     # LM_PARENT alone could be forged
            watch.register_child(home, rd.run.parent, rid, title=" ".join(task.split())[:80])
    _out(a, {"run_id": rid, "status": "running"}, rid)
    return 0


def _ask_command(cfg: dict) -> str:
    """`run.ask_channel` as a command line; a plain word (tui, telegram ...) names a channel of a screen and is none."""
    v = str(cfg.get("ask_channel") or "").strip()
    return v if re.search(r"[\s/\\.]", v) else ""


def _command_asker(home: Home, s, command: str, cfg: dict):
    """An asker for runs without a screen: starts the configured command, gives it the question as JSON on stdin
    and reads yes / always / no from the last line of its stdout.  No answer within run.ask_timeout, a failing
    command or any other word is no.  The question is masked first."""
    from . import runs
    from .providers.base import settings
    from .tools import NEW_GROUP, kill_tree, resolve_shell

    async def ask(question: str, check) -> str:
        mask = s.dispatcher.masker.mask
        payload = json.dumps({"question": mask(question), "tool": check.tool, "arg": mask(check.arg or ""),
                              "run": s.id, "agent": s.info.agent}, ensure_ascii=False)
        env = {**home.runtime_env(), "LM_HOME": str(home.root), "LM_RUN": str(s.run.path),
               "LM_RESULTS": str(s.run.results_path())}
        shell = resolve_shell(str(settings(home).get("shell") or "bash"), home.root)
        try:
            p = await asyncio.create_subprocess_exec(
                shell, "-c", home.fill(command), cwd=str(home.root), env=env, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, **NEW_GROUP)
        except OSError:
            return "no"
        try:
            out, _ = await asyncio.wait_for(p.communicate(payload.encode()), runs.duration(cfg.get("ask_timeout"), 1800))
        except (asyncio.TimeoutError, asyncio.CancelledError) as e:
            kill_tree(p.pid)
            p.kill()
            await p.wait()
            if isinstance(e, asyncio.CancelledError):
                raise
            return "no"
        lines = out.decode("utf-8", "replace").strip().splitlines()
        return ANSWERS.get(lines[-1].strip().lower(), "no") if lines and p.returncode == 0 else "no"
    return ask


def cmd_wait(home: Home, a) -> int:
    from . import runs
    try:
        res = runs.wait_runs(home, a.run_ids, a.timeout)
    except FileNotFoundError as e:
        raise Usage(str(e)) from None

    def line(r):
        return (f"{r['run_id']}  {r['status']}" + (f" ({r['exit_reason']})" if r["exit_reason"] else "")
                + f"  ${r['cost_usd']:.4f}{'~' if r['cost_est'] else ''}"
                + ("\n" + r["text"].strip() if r["text"].strip() else ""))
    _out(a, res, "\n\n".join(line(r) for r in res))
    st = {r["status"] for r in res}
    return 124 if "running" in st else 1 if "failed" in st else 0 if st == {"done"} else 3


def cmd_run(home: Home, a) -> int:
    task = a.task
    if task in (None, "-"):
        if task is None and sys.stdin.isatty():
            raise Usage("no task: give it as an argument or on stdin")
        task = sys.stdin.read()
    if not task.strip():
        raise Usage("the task is empty")
    schema = _load_schema(a.schema)
    cfg = _run_cfg(home)
    depth = _env_depth()
    if depth > int(cfg.get("max_depth", 3)):
        raise Usage(f"this run is {depth} runs deep; run.max_depth is {cfg.get('max_depth', 3)}: do the work here "
                    "instead of starting another run")
    _session_args(home, a)
    if a.detach:
        return _detach(home, a, task)
    from . import runs
    rid = a.run_id or runs.new_run_id(a.agent or "")
    session = None
    if a.continue_:
        session = _continue_open(home, a)                 # None: the session does not exist and --create makes it
        if session is False:
            _err(f"paused: session {a.continue_} stayed open in another process for {CONTINUE_WAIT // 60} min")
            return 3
        rid = session.id if session else rid
    if not _slot(home, rid, cfg):
        if session:
            session.hand_over()                           # parked, the lock is free again
        else:
            _end_unstarted(home, rid, "paused", "no free slot")
        _err(f"paused: no free slot within run.slot_timeout (run.max_parallel is {cfg.get('max_parallel')})")
        return 3
    try:
        return _run(home, a, task, schema, rid, cfg, session)
    finally:
        runs.release_slot(home, rid)


CONTINUE_WAIT = 600         # seconds `--continue` waits for a session that another process has open
CONTINUE_POLL = 1.0


def _session_args(home: Home, a) -> None:
    """The combinations of --name, --continue, --create and --detach that make sense; a bad or taken name stops here."""
    from . import runs
    if a.create and a.continue_ is None:
        raise Usage("--create goes with --continue")
    if a.name is not None and a.continue_ is not None:
        raise Usage("--name names a new session; `--continue <name> --create` makes one when it is missing")
    if a.continue_ is not None and a.detach:
        raise Usage("--continue cannot be combined with --detach: the call lasts as long as the turn, so that the next one can follow")
    must_be_a_name = [("--name", a.name)] + ([("--continue ... --create", a.continue_)] if a.create else [])
    for flag, name in must_be_a_name + [("--continue", a.continue_ if a.continue_ == "" else None)]:    # an id prefix may look like anything, but not like nothing
        if name is not None and not runs.valid_name(name):
            raise Usage(f"{flag}: {name!r} is no session name (letters, digits, . _ - and at most 64 characters)")
    if a.name is not None and runs.find_name(home, a.name):
        raise Usage(f"--name: a session called {a.name!r} exists; continue it with --continue {a.name}")


def _continue_open(home: Home, a):
    """The existing session `--continue` names, open for writing: waits up to CONTINUE_WAIT seconds while another process
    has it open.  None when there is no such session and --create may make it (`a.name` is set then); False when it stayed
    busy."""
    from . import runs
    from .loop import resume_session
    ref = a.continue_
    try:
        rid = runs.resolve_session(home, ref)
    except ValueError as e:
        raise Usage(str(e)) from None
    if rid is None:
        if not a.create:
            raise Usage(f"--continue: no session {ref!r} (a name, or the start of a run id); --create makes it")
        a.name = ref
        return None
    end = time.monotonic() + CONTINUE_WAIT
    while True:
        try:
            s = resume_session(home, rid)
        except (ValueError, NotImplementedError, OSError) as e:
            raise Usage(f"--continue {ref}: {e}") from None
        if not s.readonly_by:
            break
        s.close()                                         # a chat that is only read: nothing was written
        if time.monotonic() >= end:
            return False
        time.sleep(CONTINUE_POLL)
    i = s.info
    given = [flag for flag, v, have in (("-a", a.agent, i.agent), ("-p", a.project, i.project), ("-m", a.mode, i.mode),
                                         ("--account", a.account, i.account), ("--effort", a.effort, i.effort),
                                         ("--model", a.model, i.model)) if v and v != have]
    given += ["--allow"] * bool(a.allow) + ["--deny"] * bool(a.deny)
    if given:
        _err(f"note: {', '.join(given)} not used, session {ref} keeps its own history, agent, account and model")
    return s


def _run(home: Home, a, task: str, schema, rid: str, cfg: dict, session=None, origin: str = "") -> int:
    """One run.  `session`: the open session of `--continue` (its own run folder, history and limits stay)."""
    try:
        if session is not None:
            s = session
            s.max_turns, s.max_usd = a.max_turns, a.max_usd
        else:
            s = _open(home, a, rules=_rules(a), run_id=rid, max_turns=a.max_turns, max_usd=a.max_usd)
            if a.name:
                _name_session(home, s, a.name)
    except Usage as e:
        _end_unstarted(home, rid, "failed", str(e))
        raise
    command = _ask_command(cfg)
    if command and not _tty(sys.stdout):                 # nobody to ask on a screen: the configured channel, else a deny
        s.asker = _command_asker(home, s, command, cfg)
    _start(home, s)
    text, status, reason, code, value = "", "failed", "", 1, None
    try:
        text, value, bad = asyncio.run(_run_task(s, task, schema, origin))
        res = s.last_result
        status, reason = (res.status, res.exit_reason) if res else ("failed", "no result")
        if bad and status == "done":
            status, reason = "failed", "schema"
            _err(f"failed: the answer did not fit the schema after {SCHEMA_RETRIES} corrections: {bad}")
        code = 0 if status == "done" else 1 if status == "failed" else 3
    except ProviderError as e:                           # e.g. not logged in: the message says what to do
        reason = str(e)
        _err(reason)
    except KeyboardInterrupt:
        status, reason, code = "paused", "interrupted", 130
    finally:
        i = s.info
        out = {"run_id": i.id, "status": status, "exit_reason": reason, "text": text,
               "cost_usd": i.cost_usd, "cost_est": i.cost_est, **({"result": value} if schema else {})}
        s.run.write_result(out)                          # before `close`: a run that is over always has it
        for line in s.close(status, reason):
            _err(line)
        from . import sync
        sync.session_end(home)
    if code == 130:
        return code
    if status != "done" and not text:
        _err(f"{status}: {reason}" if reason else status)
    if a.json:
        _out(a, out, "")
    elif text:
        _p(text.removesuffix("\n"))
    return code


# ----------------------------------------------------------------------------- chat
def _esc(text: str) -> str:
    """One line, everything invisible or deceptive made visible: control characters as \\xNN, format,
    separator, private-use and surrogate characters (bidi overrides, zero-width characters ...) as
    \\u{XXXX}.  What is shown is what would run."""
    def one(c: str) -> str:
        if c in "\t\n" or unicodedata.category(c) not in ("Cf", "Zl", "Zp", "Cc", "Co", "Cs"):
            return c
        return f"\\x{ord(c):02x}" if ord(c) < 0x100 else f"\\u{{{ord(c):04x}}}"
    return "".join(one(c) for c in text)


ANSWERS = {"y": "yes", "yes": "yes", "a": "always", "always": "always", "n": "no", "no": "no"}


async def _ask(question: str, check) -> str:
    """The console asker.  Shows the whole command, one escaped line per line, and wants an explicit
    y / a (always, this session) / n and Enter: an empty or other answer is asked again."""
    print(f"\n? {_esc(question)}", flush=True)
    for n, line in enumerate((check.arg or "").split("\n"), 1):
        tool = _esc(check.tool)
        print(f"  {tool} | {_esc(line)}" if n == 1 else f"  {' ' * len(tool)} | {_esc(line)}", flush=True)
    while True:
        try:
            ans = input("  [y]es / [a]lways this session / [n]o + Enter > ").strip().lower()
        except EOFError:
            return "no"
        if ans in ANSWERS:
            return ANSWERS[ans]


def _show(ev) -> None:
    if isinstance(ev, Text):
        _w(ev.text)
        return
    _nl()                                                # a streamed line ends before anything else is shown
    if isinstance(ev, ToolCall):
        _p(f"  . {ev.name} {_brief(ev.args)}".rstrip())
    elif isinstance(ev, ToolDone):
        if not ev.ok:
            _p(f"  ! {ev.text.splitlines()[0] if ev.text else 'failed'}"[:120])
    elif isinstance(ev, Notice):
        _err(f"[{ev.kind}] {ev.text}")
    elif isinstance(ev, Error):
        _err(f"error ({ev.kind}): {ev.message}")


async def _turn(s, text: str) -> None:
    try:
        async with aclosing(s.send(text)) as events:
            async for ev in events:
                _show(ev)
    finally:
        _nl()                                            # also after Ctrl+C or an error: the prompt starts on a fresh line


SLASH = "/mode /model /account /effort /ultracode /tools /skill /rename /describe /upload /reload /<skill> /quit"
BUILTIN_SLASH = ("mode", "model", "account", "effort", "ultracode", "tools", "skill", "rename", "describe", "upload", "datei", "reload", "quit")


_RELOAD_SIG: dict | None = None       # what the last load of the chat looked like (`/reload` compares with it)
_ATTACHED: list[str] = []             # the `[Anhang: path]` lines of /upload, put in front of the next message sent
_UPLOAD_DIR: list[Path] = []          # the folder of the last file chosen: the dialog opens there next time (0 or 1 entries)


def _upload(s, home: Home, line: str) -> None:
    """`/upload` or `/datei` (anywhere in `line`): open the file dialog, copy the files into the run folder and keep their
    marker lines for the next message.  The rest of the line is put back in the input to be finished."""
    from . import upload
    text, _ = slash.take_out(line)
    got = upload.pick(upload.start_folder(_UPLOAD_DIR[0] if _UPLOAD_DIR else None))
    if got.notice:
        _p(got.notice)
    elif got.files:
        _UPLOAD_DIR[:] = [got.files[0].parent]
        done = upload.attach(got.files, s.run.path, home.root)
        for note in done.notes:
            _p(note)
        _ATTACHED.extend(done.lines)
        if done.lines:
            _p(f"{upload.confirmation(len(done.lines))} (they go in front of your next message)")
    if text.strip():
        _prefill(text)


def _slash(s, line: str) -> bool | None:
    """Run a slash command; True = leave the chat, None = it failed."""
    cmd, _, arg = line[1:].partition(" ")
    arg = arg.strip()
    try:
        if cmd == "quit":
            return True
        if cmd == "ultracode" or (cmd == "effort" and arg == "ultracode"):
            from . import core
            r = core.ultracode_command(s.home, s, "on" if cmd == "effort" else arg)
            (_p if r.ok else _err)(r.text if r.ok else f"error: {r.text}")
            return None if not r.ok else False
        if cmd in ("mode", "model", "account", "effort"):
            if arg:
                s.set(**{cmd: arg})
            _p(f"{cmd}: {getattr(s.info, cmd)}")
        elif cmd == "tools":
            verb, _, name = arg.partition(" ")
            if verb in ("on", "off") and name.strip():
                s.set(**{"tools_" + verb: [name.strip()]})
            _p("tools: " + ", ".join(t["name"] for t in s.dispatcher.schemas()))
        elif cmd == "reload":
            global _RELOAD_SIG
            from . import core
            r, _RELOAD_SIG = core.reload(s.home, s, _RELOAD_SIG if _RELOAD_SIG is not None else core.signature(s.home))
            _p(r.text)
        elif cmd in ("rename", "describe"):
            _p((s.rename if cmd == "rename" else s.describe)(arg))
        else:
            _p(f"unknown command /{cmd}; commands: {SLASH}")
    except (ValueError, NotImplementedError, RuntimeError) as e:
        _err(f"error: {e}")
        return None
    return False


def _skill_line(home: Home, line: str) -> str | None:
    """`/<skill> <request>` or `/skill <name> <request>` -> the user message to send; `/skill` alone prints the
    list (and returns None, as does anything that is not a skill command)."""
    from .context import skill_commands, skill_message
    cmd, _, arg = line[1:].partition(" ")
    cmds = skill_commands(home, BUILTIN_SLASH)
    if cmd == "skill":
        name, _, request = arg.strip().partition(" ")
        if not name:
            _p("skills (always on; /<name> <request> uses one):")
            for c, sk in cmds.items():
                _p(f"  /{c}  {' '.join(sk.description.split())[:90]}")
            return None
        sk = cmds.get(name.lower()) or cmds.get("skill:" + name.lower())
        if sk is None:
            _err(f"error: no skill named {name!r}")
            return None
        return skill_message(home, sk, request)
    sk = cmds.get(cmd.lower()) if cmd not in BUILTIN_SLASH else None
    return skill_message(home, sk, arg) if sk else None


def _slash_line(home: Home, s, line: str) -> tuple[str | None, str | None]:
    """The slash words of one input line (slash.py): the commands run, the skills are loaded.  -> (the message to
    send, if any; a command that stood last, to run after the answer).  A line that is refused sends nothing and is
    put back in the input where readline exists (elsewhere input() has taken it, the way a shell does)."""
    from .context import skill_commands
    skills = skill_commands(home, BUILTIN_SLASH)
    p = slash.parse(line, slash.Catalog(BUILTIN_SLASH, skills, takes={
        "effort": slash.Take(slash.efforts), "ultracode": slash.Take(lambda: slash.ULTRACODE_WORDS),
        "model": slash.Take(lambda: slash.models(home, s.account), slash.model_ok)}))
    if p.error:
        if p.unknown:
            _p(f"{p.error}; commands: {SLASH}")
        else:
            _err(f"error: {p.error}")
        _prefill(line)
        return None, None
    if any(a.kind == "insert" for a in p.acts):          # /upload: nothing is sent, the files wait for the next message
        _upload(s, home, line)
        return None, None
    send = bool(p.text or p.skills)
    if send and (said := slash.summary(p)):
        _p(f"  {said}")                                   # what was taken out of the line, before it is sent
    for a in p.acts:
        if a.name == "skill":                             # the one command of a line that sends
            return _skill_line(home, a.line), None
        if _slash(s, a.line) is None:                     # a command failed: nothing is sent
            _prefill(line)
            return None, None
    return (slash.message(home, p, skills) if send else None), (p.then.line if p.then else None)


def _prefill(text: str) -> None:
    """The next input() starts with `text` in the buffer, where readline exists (a refused line stays to be corrected)."""
    if not _tty(sys.stdin):
        return
    try:
        import readline
    except ImportError:
        return

    def once() -> None:
        readline.insert_text(text)
        readline.set_startup_hook()                       # for this one prompt only

    readline.set_startup_hook(once)


_TAB_NAME = ""          # the session name given with -n: the tab until the chat has a title


def _window_title(title: str, description: str = "") -> None:
    """The terminal's tab/window title: "{title} · Lightmind" (termtitle: the console title on Windows, else OSC 0)."""
    from . import termtitle
    termtitle.set_title(termtitle.tab_text(title, _TAB_NAME))


def _tab(home: Home) -> None:
    """Tab completes the slash word under the cursor where `readline` exists (not on Windows)."""
    if not _tty(sys.stdin):
        return
    try:
        import readline
    except ImportError:
        return
    from .context import skill_commands
    names = [*BUILTIN_SLASH, *skill_commands(home, BUILTIN_SLASH)]

    def hits(text: str, state: int):
        before = readline.get_line_buffer()[:readline.get_endidx()]
        if m := slash.ULTRACODE_ARG.search(before):                       # the word after /ultracode: on, off, status
            found = [f"{n} " for n in slash.ULTRACODE_WORDS if n.startswith(m[1].lower())]
            return found[state] if state < len(found) else None
        w = slash.word_at(readline.get_line_buffer(), readline.get_endidx())
        found = [f"/{n} " for n in names if n.startswith(text[1:].lower())] if w else []
        return found[state] if state < len(found) else None

    readline.set_completer(hits)
    readline.set_completer_delims(" \t\n")                 # a `/` is part of the word
    readline.parse_and_bind("bind ^I rl_complete" if "libedit" in (readline.__doc__ or "") else "tab: complete")


def cmd_chat(home: Home, a) -> int:
    if a.name is not None:
        _check_name(home, a.name)
    s = _open(home, a, asker=_ask)
    if a.name is not None:
        _name_session(home, s, a.name)
    i = s.info
    from . import core
    global _RELOAD_SIG
    _RELOAD_SIG = core.signature(home)
    s.enable_titles()
    global _TAB_NAME
    _TAB_NAME = a.name or ""
    s.on_title.append(_window_title)                     # runs in the naming thread: it only sets the title
    _p(f"lm chat {i.id}{' . ' + i.title if i.title else ''} . {i.model} . {i.account} . mode {i.mode} . effort {i.effort}   ({SLASH}; Ctrl+D leaves)")
    _window_title(i.title, i.description)
    _start(home, s)
    _tab(home)
    try:
        while True:
            try:
                line = input("> ").strip()
            except EOFError:
                break
            except KeyboardInterrupt:                    # at the prompt: drop the line, like a shell
                print()
                continue
            if not line:
                continue
            then = None
            if slash.has_slash(line):                    # slash words anywhere in the line (slash.py)
                line, then = _slash_line(home, s, line)
                if line is None:                         # nothing to send: only commands ran
                    if then and _slash(s, then):
                        break
                    continue
            if _ATTACHED:                                # the files picked with /upload go in front of the message
                line = "\n".join([*_ATTACHED, line])
                _ATTACHED.clear()
            try:
                asyncio.run(_turn(s, line))
            except ProviderError as e:                   # not logged in ...: say it, the chat goes on
                _err(str(e))
                continue
            except KeyboardInterrupt:                    # Ctrl+C in a turn: that turn stops, the session stays
                _err("(stopped)")
                continue
            finally:
                _window_title(s.title)                   # claude.exe renames the shared console during a turn
            if then and _slash(s, then):                 # a command that stood last runs after the answer
                break
    finally:
        for text in s.close():
            _p(text)
        from . import sync
        sync.session_end(home)
    return 0


def cmd_serve(home: Home, a) -> int:
    """`lm serve --stdio --workdir <folder>`: the backend of the VS Code extension (serve.py, plan/PROTOCOL.md).  For a person's
    editor, not for a run an agent started."""
    if _env_depth() > 0 or os.environ.get("LM_RUN"):
        raise Usage("lm serve is for an editor a person works in, not for a run an agent started")
    if not a.stdio:
        raise Usage("lm serve needs --stdio (the only transport)")
    workdir = None
    if a.workdir:
        p = os.path.abspath(os.path.expanduser(a.workdir))
        if not os.path.isdir(p):
            raise Usage(f"--workdir {a.workdir!r} is not a folder")
        workdir = p
    from . import serve
    _migrate(home)
    return serve.serve_stdio(home, workdir)


def cmd_node(home: Home, a) -> int:
    """`lm node --stdio|--uplink|--relay NAME|targets`: see node.py.  Exit 3 when another node of this tree runs."""
    from . import node
    chosen = [bool(a.action), a.stdio, a.uplink, bool(a.relay)]
    if sum(chosen) != 1:
        raise Usage("lm node needs exactly one of: targets, --stdio, --uplink, --relay NAME")
    if a.action == "targets":
        rows = node.targets(home)
        _out(a, rows, "\n".join(f"{r['name']:14} {r['transport']:7} {'on ' if r['enabled'] else 'off'}{' default' if r['default'] else ''}"
                                + (f"  ({r['note']})" if r.get("note") else "") for r in rows) or "no server has an lm_home")
        return 0
    if a.stdio:
        return node.serve_stdio(home)
    if a.uplink:
        return node.serve_uplink(home)
    return node.relay(home, a.relay)


def _chat_ref(home: Home, ref: str) -> str:
    """The run id a chat reference means (session name or unique id prefix); a Usage error says what is wrong with it."""
    from . import runs
    try:
        sid = runs.resolve_session(home, ref)
    except ValueError as e:
        raise Usage(str(e)) from None
    if sid is None:
        raise Usage(f"no chat or session name matches {ref!r}")
    return sid


def cmd_skill(home: Home, a) -> int:
    """`lm skill add <name>` and `lm skill list [--available]`: the logic is in skillrepo.py."""
    from . import skillrepo
    try:
        if a.action == "add":
            if not a.name:
                raise Usage("lm skill add <name> [--from SRC] [--force]")
            r = skillrepo.add(home, a.name, skillrepo.default_source(home, a.source), a.force)
            _out(a, {"name": r.name, "path": str(r.path), "source": r.source, "commit": r.commit, "backup": r.backup},
                 f"added skills/{r.name} from {r.source}" + (f" ({r.commit[:10]})" if r.commit else "")
                 + (f"\nthe old one is in {r.backup}" if r.backup else ""))
        elif a.available:
            rows = skillrepo.available_rows(skillrepo.default_source(home, a.source))
            _out(a, rows, "\n".join(f"{r['name']:24} {r['description'][:100]}" for r in rows) or "the source offers no skills")
        else:
            rows = skillrepo.tree_rows(home)
            _out(a, rows, "\n".join(f"{r['name']:24} {r['description'][:80]}" + (f"  [{r['source']}]" if r["source"] else "") for r in rows)
                 or "no skills in the tree")
    except skillrepo.SkillRepoError as e:
        _err(f"lm: {e}")
        return 1
    return 0


def cmd_archive(home: Home, a) -> int:
    """`lm archive <chat>`, `lm archive list`, `lm archive restore <chat>`: the archive is a marker file in the chat's run
    folder (runs.archive); archived chats leave the resume list and the phone app's list and show here and in the archive
    view of the resume menu (Tab)."""
    from . import runs
    words = list(a.words)
    if words[0] == "list" and len(words) == 1:
        names = runs.session_names(home)
        rows = [{"id": r.id, "name": names.get(r.id, ""), "title": r.title, "agent": r.agent, "last_active": r.last_active,
                 **_archive_marker(home, r.id)} for r in runs.list_sessions(home, 200, archived=True)]
        _out(a, rows, "\n".join(f"{r['id']}  {r['name'] or '-':14} {r['title'][:40]}" for r in rows) or "the archive is empty")
        return 0
    restore = words[0] == "restore"
    if len(words) != (2 if restore else 1) or (not restore and words[0] in ("list",)):
        raise Usage("lm archive <chat> | lm archive list | lm archive restore <chat>")
    sid = _chat_ref(home, words[-1])
    _p(f"{sid}: " + (runs.unarchive(home, sid) if restore else runs.archive(home, sid, by="cli")))
    return 0


def _archive_marker(home: Home, run_id: str) -> dict:
    """When and by whom a chat was archived (`archived_at`, `archived_by`), for `lm archive list`."""
    try:
        marker = json.loads((home.path("runs") / run_id / "archived.json").read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return {"archived_at": str(marker.get("at") or ""), "archived_by": str(marker.get("by") or "")}


# ------------------------------------------------------------------------ runs, rules
def cmd_runs(home: Home, a) -> int:
    from . import runs
    if a.action == "list":
        rows = runs.list_runs(home, a.limit)
        names = runs.session_names(home)
        _out(a, [{**r.to_dict(), **({"name": names[r.id]} if r.id in names else {})} for r in rows], "\n".join(
            f"{r.id}  {r.status:8} {r.agent or '-':12} {r.model:10} ${r.cost_usd:.4f}{'~' if r.cost_est else ''}"
            + (f"  {names[r.id]}" if r.id in names else "") + (f"  {r.title}" if r.title else "")
            for r in rows) or "no runs yet")
    elif a.action == "show":
        if not a.run_id:
            raise Usage("runs show needs a run id (a unique prefix is enough)")
        d = runs.show_run(home, runs.find_name(home, a.run_id) or a.run_id)
        r = d["run"]
        d["name"] = runs.session_names(home).get(r["id"], "")
        _out(a, d, f"{r['id']}  {r['status']}  {r.get('agent') or '-'}  {r.get('model')}  ${r.get('cost_usd', 0):.4f}\n"
                   f"{d['path']}\n{d['events']} events, {len(d['tool_out'])} tool outputs\n"
                   + "\n".join(d["log"].splitlines()[-20:]))
    else:
        c = runs.cost_summary(home, a.since, a.by)
        _out(a, c, "\n".join([f"{k:20} ${v:.4f}" for k, v in sorted(c.items(), key=lambda kv: -kv[1])]
                             + [f"{'total':20} ${sum(c.values()):.4f}"]))
    return 0


def cmd_rules(home: Home, a) -> int:
    from . import rules
    agent = home.load_agent(a.agent) if a.agent else None
    layers = rules.build_layers(home, agent, a.mode or str(home.defaults.get("mode") or ""), None, None)
    rows = [{"layer": n, "locked": r.locked, "effect": r.effect, "rule": r.text(), "origin": r.origin}
            for n, r in rules.resolved_rules(layers)]
    _out(a, rows, "\n".join(f"{x['layer']:14} {'locked ' if x['locked'] else '       '}{x['effect']:5} {x['rule']}   {x['origin']}"
                            for x in rows))
    return 0


# ---------------------------------------------------------------------------- main
def _utf8() -> None:
    """UTF-8 on the standard streams: a pipe on Windows would use the ANSI code page and fail on a letter."""
    for st in (sys.stdin, sys.stdout, sys.stderr):
        try:
            st.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def _no_console_windows() -> None:
    """Windows: a process without a console (started by VS Code, a service, a detached run) makes every console
    program it starts (git, bash, python) open a visible window.  Then each child gets a hidden one instead."""
    if os.name != "nt":
        return
    import ctypes
    if ctypes.windll.kernel32.GetConsoleWindow():
        return                                            # a terminal: children share it, nothing pops up
    real = subprocess.Popen.__init__
    if getattr(real, "_lm_hidden", False):
        return
    shown = subprocess.CREATE_NEW_CONSOLE | subprocess.DETACHED_PROCESS

    def init(self, *args, creationflags: int = 0, **kw):
        if not creationflags & shown:
            creationflags |= subprocess.CREATE_NO_WINDOW
        real(self, *args, creationflags=creationflags, **kw)
    init._lm_hidden = True
    subprocess.Popen.__init__ = init


# Flag spellings of a command: `lm -q` is `lm q`, `lm --resume` and `lm -resume` are `lm resume`.
FLAG_COMMANDS = {"-q": "q", "--quick": "q", "-resume": "resume", "--resume": "resume"}


def expand_flag_commands(argv: list[str]) -> list[str]:
    """Turn a flag spelling that stands where the command would stand (behind the global --home and --json) into the command."""
    out, i = list(argv), 0
    while i < len(out):
        if out[i] == "--home":
            i += 2
        elif out[i] == "--json" or out[i].startswith("--home="):
            i += 1
        else:
            out[i] = FLAG_COMMANDS.get(out[i], out[i])
            break
    return out


def main(argv: list[str] | None = None) -> int:
    p = build_parser()
    a = p.parse_args(expand_flag_commands(sys.argv[1:] if argv is None else argv))
    _utf8()
    _no_console_windows()
    from . import runtime
    runtime.hold()                     # this process counts as using its runtime, so no cleanup removes it
    try:
        if a.cmd == "init":
            return cmd_init(a)
        if a.cmd == "update":
            return cmd_update(a)
        try:
            home = Home.open(a.home)
        except FileNotFoundError:
            raise Usage(f"no Lightmind tree at {Home.resolve_root(a.home)}; create one with `lm init`") from None
        handler = {"lint": cmd_lint, "open": cmd_open, "accounts": cmd_accounts, "secret": cmd_secret, "login": cmd_login, "remote": cmd_remote, "chat": cmd_chat, "q": cmd_q, "resume": cmd_resume, "run": cmd_run, "wait": cmd_wait, "runs": cmd_runs, "rules": cmd_rules, "check": cmd_check, "sync": cmd_sync, "serve": cmd_serve, "node": cmd_node, "archive": cmd_archive, "skill": cmd_skill, "wake": cmd_wake, "triggers": cmd_triggers}
        return handler[a.cmd](home, a) if a.cmd else cmd_tui(home)
    except Usage as e:
        _err(f"lm: {e}")
        return 2
    except ProviderError as e:                           # login without config_dir and the like
        _err(str(e))
        return 1
    except (FileNotFoundError, FileExistsError) as e:
        _err(f"lm: {e}")
        return 1
    except ValueError as e:
        _err(f"lm: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
