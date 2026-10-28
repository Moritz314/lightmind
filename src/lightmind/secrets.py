"""Secrets: .env parsing, injection, masking, provider credentials, service token.

May import: model, files, sync (late, at the write point).  (rules decisions arrive as arguments, never imported.)
Locations come from home.path('secrets') / home.path('state'); no folder names here.
"""
from __future__ import annotations

import hmac
import json
import re
import secrets as _rnd
import string
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .files import Home, write_atomic

END_HEADER = "# --- END HEADER ---"
ENV_SUFFIX = ".env"
SERVICE_TOKEN_VAR = "LM_SERVICE_TOKEN"
_REF = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9_-])?")   # never ends in a dot (Windows drops it)
_VAR = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_PLACEHOLDER = re.compile(r"<secret:([^:>]+/[^:>]+):(\w+)>")
_ALPHABET = string.ascii_letters + string.digits + "!#%+-_"


def parse_env(path: Path) -> dict[str, str]:
    """Parse `<account>.env`: comments up to '# --- END HEADER ---', then KEY=value."""
    out: dict[str, str] = {}
    for ln in Path(path).read_text(encoding="utf-8").splitlines():
        ln = ln.strip()
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        k, v = ln.split("=", 1)
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        out[k.removeprefix("export ").strip()] = v
    return out


def env_header(path: Path) -> str:
    """Header comment block of an .env file (never values)."""
    head = []
    for ln in Path(path).read_text(encoding="utf-8").splitlines():
        if ln.strip() == END_HEADER or not ln.strip().startswith("#"):
            break
        head.append(ln)
    return "\n".join(head)


def canon_ref(ref: str) -> str:
    """The form refs are compared in: always case-folded.  Windows and macOS read Stripe/Live and
    stripe/live as one folder; on a case-sensitive system the extra folding only blocks look-alikes."""
    return ref.casefold()


def _split(ref: str) -> tuple[str, str]:
    """'name/account' -> (name, account); 'name' -> (name, '').  Rejects anything path-like and
    names ending in a dot or space (Windows drops those: another spelling of the same folder)."""
    name, sep, acc = ref.partition("/")
    if not _REF.fullmatch(name) or (sep and not _REF.fullmatch(acc)) or ".." in ref:
        raise ValueError(f"bad secret reference {ref!r}")
    return name, acc


def _env_path(home: Home, ref: str) -> Path:
    name, acc = _split(ref)
    if not acc:
        raise ValueError(f"secret reference {ref!r} needs an account (name/account)")
    return home.path("secrets") / name / (acc + ENV_SUFFIX)


def list_secrets(home: Home) -> list[str]:
    """['name/account', ...] discovered under paths.secrets."""
    root = home.path("secrets")
    if not root.is_dir():
        return []
    return sorted(f"{d.name}/{f.stem}" for d in root.iterdir() if d.is_dir() and not d.name.startswith("_")
                  for f in d.glob("*" + ENV_SUFFIX))


def secret_info(home: Home, name: str) -> str:
    """info.md text + each .env header and variable names; never values (`lm secrets info`)."""
    n, acc = _split(name)
    d = home.path("secrets") / n
    if not d.is_dir():
        raise ValueError(f"no secret {n!r}")
    info = d / home.filename("info")
    parts = [info.read_text(encoding="utf-8").strip() if info.exists() else f"(no {info.name})"]
    for f in sorted(d.glob("*" + ENV_SUFFIX)):
        if acc and f.stem != acc:
            continue
        parts.append(f"## {n}/{f.stem}\n{env_header(f)}\nvariables: {', '.join(parse_env(f)) or '-'}")
    return "\n\n".join(parts)


def reserved_refs(home: Home) -> set[str]:
    """Refs no tool may ever receive: provider credentials and the service token (also for `lm lint`)."""
    out: set[str] = set()
    try:
        for a in home.load_providers().accounts.values():
            out |= {r for r in (a.secret, a.admin_secret) if r}
    except (OSError, KeyError, ValueError):
        pass
    r = _service_ref(home)
    return out | ({r} if r else set())


def _blocked(home: Home) -> set[str]:
    """Reserved refs and their bare folder names, canonical (see `canon_ref`)."""
    refs = {canon_ref(r) for r in reserved_refs(home)}
    return refs | {r.partition("/")[0] for r in refs}


@dataclass
class InjectedEnv:
    env: dict[str, str] = field(default_factory=dict)
    uses: list[dict] = field(default_factory=list)        # {"secret","vars","tool"} for audit (caller fills tool)
    values: list[str] = field(default_factory=list)       # for the masker
    named: list[tuple[str, str, str]] = field(default_factory=list)   # (ref, var, value): Masker.add(*item)


def build_tool_env(home: Home, wanted: list[str], allowed: Callable[[str], bool],
                   base_env: dict[str, str] | None = None) -> InjectedEnv:
    """Fresh environment = base_env + vars of every wanted 'name/account' for which
    allowed(ref) is True.  Denied secrets are silently skipped.  Provider credentials are
    never part of this."""
    inj = InjectedEnv(env=dict(base_env or {}))
    blocked = _blocked(home)
    for ref in dict.fromkeys(wanted):
        try:
            p = _env_path(home, ref)
        except ValueError:
            continue
        if canon_ref(ref) in blocked or not p.is_file() or not allowed(ref):
            continue
        vals = parse_env(p)
        inj.env.update(vals)
        inj.uses.append({"secret": ref, "vars": list(vals), "tool": ""})
        inj.values += list(vals.values())
        inj.named += [(ref, k, v) for k, v in vals.items()]
    return inj


class Masker:
    """Replace secret values (len >= MIN_LEN) in text with <secret:name/account:VAR>."""

    MIN_LEN = 6

    def __init__(self) -> None:
        self._items: dict[str, str] = {}

    def add(self, ref: str, var: str, value: str) -> None:
        """Also registers the forms JSON writes the value in (escaped quotes, backslashes and control
        characters; non-ASCII letters raw or as u-escapes), so a secret inside a JSON text is masked too."""
        if len(value) >= self.MIN_LEN:
            for form in (value, json.dumps(value, ensure_ascii=False)[1:-1], json.dumps(value)[1:-1]):
                self._items[form] = f"<secret:{ref}:{var}>"

    def mask(self, text: str) -> str:
        for v in sorted(self._items, key=len, reverse=True):
            text = text.replace(v, self._items[v])
        return text


def provider_env(home: Home, secret_ref: str) -> dict[str, str]:
    """Credentials of a provider account for the core's own provider call only."""
    p = _env_path(home, secret_ref)
    if not p.is_file():
        raise FileNotFoundError(f"no credentials for {secret_ref}")
    return parse_env(p)


def _service_ref(home: Home) -> str | None:
    v = home.key_doc("permissions").meta.get("service_token")
    return None if v in (None, False, "", "off") else str(v)


def service_token(home: Home) -> str | None:
    """Shared token for lmd/pcd/browserd per the permissions file; None if switched off."""
    ref = _service_ref(home)
    return None if ref is None else provider_env(home, ref)[SERVICE_TOKEN_VAR]


def check_service_token(home: Home, presented: str | None) -> bool:
    """Switched off -> True.  Switched on -> constant-time compare; any problem -> False."""
    if _service_ref(home) is None:
        return True
    try:
        tok = service_token(home)
    except (OSError, KeyError, ValueError):
        return False
    return bool(presented) and bool(tok) and hmac.compare_digest(presented, tok)


# ------------------------------------------------------------ secrets tool
def _write_env(path: Path, var: str, value: str, mode: str, header: str = "# created by the secrets tool",
               home: Home | None = None) -> None:
    """Set var in the .env file (atomic).  mode new: var must be new; rotate: must exist; set: either."""
    if not _VAR.fullmatch(var) or "\n" in value or "\r" in value:
        raise ValueError(f"bad variable name or value for {var!r}")
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else \
        [header, END_HEADER]
    idx = next((i for i, ln in enumerate(lines) if ln.split("=", 1)[0].strip() == var and "=" in ln
                and not ln.lstrip().startswith("#")), None)
    if mode == "new" and idx is not None:
        raise ValueError(f"{var} already exists in {path.stem}; use rotate")
    if mode == "rotate" and idx is None:
        raise ValueError(f"{var} does not exist in {path.stem}; use new")
    if idx is None:
        lines.append(f"{var}={value}")
    else:
        lines[idx] = f"{var}={value}"
    write_atomic(path, "\n".join(lines) + "\n", mode=0o600)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    if home is not None:                       # a sync repo gets the .age beside it at once (silent otherwise)
        from . import sync
        sync.on_secret_written(home, path)


def secrets_tool(home: Home, action: str, ref: str, **kw) -> str:
    """Built-in `secrets` tool: new | set | rotate | describe.  Returns only a
    <secret:ref:VAR> placeholder, never a value.  Whether the caller may write is decided by
    the `secret-write` rule before this runs.  `captured` (a value the core itself obtained)
    must never appear in the model-visible schema; `from_ref` copies another secret's var by
    placeholder (the dispatcher also checks `secret(<name/account>)` of the parsed placeholder).
    For every action a `ref` or `from_ref` that is reserved (provider credentials, service
    token) or a `from_ref` that does not parse is rejected before anything is read or written."""
    name, acc = _split(ref)
    from_name = from_var = None
    if kw.get("from_ref") is not None and kw.get("from_ref") != "":
        m = _PLACEHOLDER.fullmatch(str(kw["from_ref"]).strip())     # malformed -> reject the call
        if not m:
            raise ValueError("from_ref must look like <secret:name/account:VAR>")
        from_name, from_var = m.group(1), m.group(2)
        _split(from_name)
    blocked = _blocked(home)
    for r in (ref, from_name):
        if r and canon_ref(r) in blocked:
            raise ValueError(f"secret reference {r!r} is reserved (provider credentials / service token)")
    if action == "describe":
        info = home.path("secrets") / name / home.filename("info")
        raw = info.read_text(encoding="utf-8") if info.exists() else "---\nservers: []\n---\n"
        end = raw.find("\n---", 3) if raw.startswith("---\n") else -1
        head = raw[:end + 5] if end >= 0 else ""
        info.parent.mkdir(parents=True, exist_ok=True)
        info.write_text(head + str(kw.get("text", "")).strip() + "\n", encoding="utf-8")
        return f"info updated: {name}"
    if action not in ("new", "set", "rotate"):
        raise ValueError(f"unknown secrets action {action!r}")
    var, path = kw.get("var", ""), _env_path(home, ref)
    value = kw.get("captured")
    if value is None and from_name:
        value = parse_env(_env_path(home, from_name))[from_var]
    if value is None:
        if action == "set" or not kw.get("generate", action != "set"):
            raise ValueError(f"secrets.{action} needs from_ref (or generate=true)")
        value = "".join(_rnd.choice(kw.get("alphabet", _ALPHABET)) for _ in range(int(kw.get("length", 24))))
    if action == "new" and not (path.parent / home.filename("info")).exists():
        secrets_tool(home, "describe", ref, text=kw.get("text", f"Created by an agent. Account: {acc}."))
    _write_env(path, var, value, action, home=home)
    return f"<secret:{ref}:{var}>"


def store_secret(home: Home, ref: str, var: str, value: str) -> bool:
    """`lm secret set`: the human's way to put a value into `<secrets>/<name>/<account>.env` (the same
    file format the secrets tool writes).  Unlike the tool this may write provider credentials (that is
    its purpose); it refuses malformed refs, the service-token ref and an empty value.  Returns True when
    an existing value was replaced.  The value never leaves this function."""
    _split(ref)
    if canon_ref(ref) == canon_ref(_service_ref(home) or "-"):
        raise ValueError(f"secret reference {ref!r} is the service token: not set from here")
    value = value.strip()
    if not value:
        raise ValueError("empty value")
    path = _env_path(home, ref)
    replaced = path.is_file() and var in parse_env(path)
    _write_env(path, var, value, "set", header="# created by `lm secret set`; the value is read by the core only", home=home)
    return replaced


def secret_vars(home: Home) -> dict[str, list[str]]:
    """{'name/account': [variable names]} of every secret file; never values."""
    out: dict[str, list[str]] = {}
    for ref in list_secrets(home):
        try:
            out[ref] = list(parse_env(_env_path(home, ref)))
        except (OSError, ValueError):
            out[ref] = []
    return out
