"""Rule checker: reads rules from Markdown files and applies them.  No rule lives in code.

May import: model, files.  Nothing else.  Matching semantics live here; every rule, limit and
default comes from permissions.md / lightmind.md.  Patterns may use placeholders: `{key}` for
any `paths:` key of lightmind.md (`{secrets}`), `{file_<key>}` for its `files:` keys, `{entry}`
(lightmind.md itself), `{config}` (config.md, also in a tree whose lightmind.md has no `paths.config`), `{run}` / `{project}` (or `$RUN` / `$PROJECT`) from the RuleContext.
The checker only has to be correct: how paths, shell lines, URLs and match values are read is
fixed here, what is allowed never is.
"""
from __future__ import annotations

import asyncio
import json
import math
import os
import posixpath
import re
import sys
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import replace
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlparse

from .files import CONFIG, ENTRY, MODES, RIGHTS, Home
from .model import Agent, Check, Decision, Layer, Rule, RuleContext, SessionState

PATH_TOOLS = {"read", "write", "edit"}
_COVERS = {"write": {"edit"}}                 # a write rule also governs edit checks
_RANK = {"allow": 0, "ask": 1, "deny": 2}
_RULE_RE = re.compile(r"^\s*([\w*-]+)\s*(?:\((.*)\))?\s*(?:until\s+(\d{4}-\d{2}-\d{2}))?\s*$", re.S)
_PERIODS = {"day": "%Y-%m-%d", "week": "%G-W%V", "month": "%Y-%m"}
_ABS = re.compile(r"(?:/|[A-Za-z]:[/\\])")
_WIN = sys.platform == "win32"
_BAD = "\0invalid"                              # host / argument that no pattern can match
_NAME = r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9_-])?"      # never ends in a dot (same host name on Windows)
_VAL_SEP = r"""\s,;<>"'"""                          # characters that end a `tool(x:match)` value
_SECRET_PH = re.compile(rf"<secret:({_NAME}/{_NAME}):(\w+)>")      # same shape as secrets.py
_SECRET_REF = re.compile(rf"{_NAME}(?:/{_NAME})?")
_GLOB = re.compile(r"[*?{$\[]")
_UNSAFE_TOOLS = {"secret-malformed"}               # checks that are refused whatever the rules say
_ASK_TOOLS = {"shell-unparsed"}                    # checks that ask when no rule names them (a line we cannot read safely)


def parse_rule(text: str, effect: str = "allow", *, locked: bool = False,
               origin: str = "") -> Rule:
    """'bash(git push*) until 2026-12-01' -> Rule.  Raises ValueError on bad syntax."""
    m = _RULE_RE.match(text) if isinstance(text, str) else None
    if not m or effect not in _RANK:
        raise ValueError(f"bad rule {text!r} (effect {effect!r}){' at ' + origin if origin else ''}")
    until = date.fromisoformat(m.group(3)) if m.group(3) else None
    return Rule(m.group(1), m.group(2), effect, locked, until, origin)


# ----------------------------------------------------------------- matching
def _unsafe_path(p: str) -> bool:
    """Windows traps: alternate data streams (`:`), 8.3 short names (`~1`), device/UNC prefixes,
    trailing dots or spaces on a segment.  Never true off Windows."""
    if not _WIN:
        return False
    s = str(p).replace("\\", "/")
    rest = s[2:] if re.match(r"[A-Za-z]:", s) else s
    return (":" in rest or bool(re.search(r"~\d", s)) or s.startswith("//") or "\0" in s
            or any(seg not in (".", "..") and seg != seg.rstrip(". ") for seg in s.split("/")))


unsafe_path = _unsafe_path                       # public: the dispatcher refuses the same paths


_REAL_MEMO: ContextVar[dict[str, str] | None] = ContextVar("lm_real_memo", default=None)


@contextmanager
def real_memo():
    """Inside the block `_real` remembers what it resolved.  For a batch of decisions made in one go, where the same few
    paths come up again and again (the guard check of config.md asks 6 times about 20 rules); never around a running
    chat, where a link may change between two calls."""
    token = _REAL_MEMO.set({})
    try:
        yield
    finally:
        _REAL_MEMO.reset(token)


def _real(p: str) -> str:
    memo = _REAL_MEMO.get()
    if memo is not None and p in memo:
        return memo[p]
    try:
        r = os.path.realpath(p).replace("\\", "/")
    except (OSError, ValueError):
        r = p
    if memo is not None:
        memo[p] = r
    return r


def _canon(p: str, home: str, resolve: bool = True) -> str:
    """Path relative to home (posix, `..` and links resolved, trailing dots/spaces stripped) if
    inside it, else normalised absolute.  `resolve=False` leaves links alone (spelling only)."""
    p, h = str(p).replace("\\", "/"), posixpath.normpath(str(home).replace("\\", "/"))
    if not _ABS.match(p):
        p = h + "/" + p
    if _WIN:
        p = "/".join(seg if seg in (".", "..") else seg.rstrip(". ") for seg in p.split("/"))
    p = posixpath.normpath(p)
    if resolve:
        p, h = _real(p), _real(h)
    a, b = (p.lower(), h.lower()) if _WIN else (p, h)
    return "." if a == b else p[len(h) + 1:] if a.startswith(b + "/") else p


def unsafe_check(c: Check) -> bool:
    """A check whose argument cannot be judged safely (path trap, ambiguous URL)."""
    if c.tool in PATH_TOOLS:
        return _unsafe_path(c.arg or "")
    if c.tool in _UNSAFE_TOOLS:
        return True
    return c.tool == "web" and _web_host(c.arg or "") == _BAD


def _idna(host: str) -> str:
    host = host.strip().lower()
    host = host[:-1] if host.endswith(".") else host      # one trailing root dot is the same host
    if not host or any(ord(ch) < 33 or ch in "\\@/?#" for ch in host) or "" in host.split("."):
        return _BAD
    try:
        return ".".join(l if "*" in l else l.encode("idna").decode("ascii") for l in host.split("."))
    except UnicodeError:
        return _BAD


def _web_host(arg: str) -> str:
    """Normalised hostname of a URL (or `domain:x`), or _BAD when ambiguous."""
    arg = arg.strip()
    if arg.startswith("domain:"):
        return _idna(arg[7:])
    if "\\" in arg or any(ord(ch) < 33 for ch in arg):
        return _BAD
    try:
        u = urlparse(arg)
        if "@" in u.netloc or not u.hostname:
            return _BAD
        return _idna(u.hostname)
    except ValueError:
        return _BAD


_HV: dict[str, tuple[int, dict, dict]] = {}


def _home_vars(home: str) -> tuple[dict[str, str], dict[str, str]]:
    """(canonical, as-written) placeholder maps from lightmind.md; a paths entry that
    resolves outside the home root -> ValueError."""
    try:
        mt = (Path(home) / ENTRY).stat().st_mtime_ns
    except OSError:
        return {}, {}
    hit = _HV.get(home)
    if hit and hit[0] == mt:
        return hit[1], hit[2]
    h = Home(home)
    fixed = {"entry": ENTRY, "config": CONFIG, "modes": MODES, "rights": RIGHTS}     # `paths.<key>` below may move them
    canon, raw = {k: _canon(v, home) for k, v in fixed.items()}, dict(fixed)          # links resolved, as a checked path
    for k in h.paths:
        lex = _canon(str(h.path(k)), home, resolve=False)          # as written: must stay inside
        if _ABS.match(lex) or lex == ".." or lex.startswith("../"):
            raise ValueError(f"lightmind.md paths.{k} resolves outside the home root")
        canon[k], raw[k] = _canon(str(h.path(k)), home), h.rel(k)   # as a checked path will look: links resolved
    for k, v in h.files.items():
        canon["file_" + k] = raw["file_" + k] = v
    _HV[home] = (mt, canon, raw)
    return canon, raw


def _subst(pat: str, ctx: RuleContext, pathy: bool) -> str:
    canon, raw = _home_vars(ctx.home)
    vals = dict(canon if pathy else raw)
    for k, v in (("run", ctx.run_dir), ("project", ctx.project_dir)):
        vals[k] = (_canon(v, ctx.home) if pathy else v) if v else "\0unset"
    return re.sub(r"\{(\w+)\}|\$(RUN|PROJECT)\b",
                  lambda m: vals.get((m.group(1) or m.group(2)).lower(), m.group(0)), pat)


@lru_cache(maxsize=2048)
def _rx(pat: str, pathy: bool, ci: bool, star: str = "") -> re.Pattern:
    """Glob -> regex.  `[c]` for c in `*?{$[` is the literal c (escape used by 'always' rules).
    `star` = regex a single `*` becomes outside paths (default: anything)."""
    out, i = [], 0
    while i < len(pat):
        if pat.startswith("[", i) and pat[i + 2:i + 3] == "]" and pat[i + 1:i + 2] and pat[i + 1] in "*?{$[":
            out.append(re.escape(pat[i + 1]))
            i += 3
            continue
        if pathy and pat.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if pathy and pat.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        c = pat[i]
        out.append((("[^/]*" if pathy else (star or ".*")) if c == "*"
                    else "[^/]" if pathy and c == "?" else re.escape(c)))
        i += 1
    return re.compile("".join(out), re.S | (re.I if ci else 0))


def literal(text: str) -> str:
    """Escape glob metacharacters and placeholders so `text` matches only itself."""
    return _GLOB.sub(lambda m: "[" + m.group() + "]", text)


def match_rule(rule: Rule, check: Check, ctx: RuleContext) -> bool:
    """Does `rule` apply to `check`?  Handles tool equality, `*`, glob (`*` one path
    segment / `**` any depth for read|write|edit), $RUN, $PROJECT, `web(domain:..)`,
    `tool(x:match)`, and `until` expiry (expired rules never match).

    Paths are compared in canonical form (links resolved, `..` and trailing dots gone, case
    folded on Windows); a Windows alias (stream, 8.3 name) never matches.  A shell line is
    judged per simple command (see `split_bash`): an allow rule must cover every command, a
    deny / ask rule fires on any one.  Host names are lowercased, IDNA-encoded and stripped of
    one trailing dot; ambiguous URLs never match.  `*` in a `tool` pattern stops at whitespace
    and commas, in a secret pattern at `/`.  `[*]` `[?]` `[{]` `[$]` `[[]` are literal
    characters (what an 'always' answer stores)."""
    if rule.until and (ctx.now or date.today()) >= rule.until:
        return False
    if rule.exclude and any(match_rule(x, check, ctx) for x in rule.exclude):
        return False
    if rule.tool == "*":
        return check.tool not in _ASK_TOOLS           # an unreadable line is let through only by a rule that names it
    if rule.tool != check.tool and check.tool not in _COVERS.get(rule.tool, ()):
        return False
    if rule.pattern is None:
        return True
    pathy, arg = check.tool in PATH_TOOLS, check.arg or ""
    pat = _subst(rule.pattern, ctx, pathy)
    ci = _WIN and (pathy or check.tool.startswith("secret"))        # Windows folds case in both
    star = ""
    if pathy:
        if _unsafe_path(arg):
            return False
        arg = _canon(arg, ctx.home)
        pat = _canon(pat, ctx.home) if _ABS.match(pat) else pat
    elif check.tool in ("bash", "ssh") and (check.tool == "bash" or ":" in pat):
        # a shell line is judged per simple command: an allow rule must cover every one of
        # them, a deny / ask rule fires when it covers any one (`;` `&&` `|` `$()` are never
        # swallowed by a `*`)
        head, cmd = ("", arg) if check.tool == "bash" else arg.partition(":")[::2]
        if check.tool == "ssh":
            head += ":"
        rx = _rx(pat, False, False)
        hits = [rx.fullmatch(head + s) is not None for s in split_bash(cmd) or [cmd.strip()]]
        return all(hits) if rule.effect == "allow" else any(hits)
    elif check.tool == "web" and pat.startswith("domain:"):
        arg, pat = _web_host(arg), _idna(pat[7:])
        if arg == _BAD:
            return False
        star = r"[^/\\@\s:\0]*"
    elif check.tool not in ("web", "ssh", "shell-unparsed"):     # an unreadable line is matched whole, `:` and all
        star = (rf"[^{_VAL_SEP}\0]*" if check.tool == "tool"  # a `tool(x:match)` value is one element
                else r"[^\s,/\0]*" if check.tool.startswith("secret") else "")
        sep = "/" if check.tool.startswith("secret") else ":"
        if sep not in pat:
            arg = arg.split(sep, 1)[0]            # `tool(send-mail)` covers `send-mail:value`
    elif check.tool == "ssh":
        arg = arg.split(":", 1)[0]                # `ssh(buildbox)` covers every command there
    return _rx(pat, pathy, ci, star).fullmatch(arg) is not None


# ------------------------------------------------------------------- layers
def _lines(doc: Any) -> list[str]:
    """The file's lines, read once per document (for the `file:line` origin of a rule)."""
    try:
        return Path(doc.path).read_text(encoding="utf-8").splitlines()
    except (OSError, AttributeError):
        return []


def _line(lines: list[str], text: str) -> int:
    for n, ln in enumerate(lines, 1):
        if text in ln:
            return n
    return 0


def _block(block: dict, locked: bool, origin: str, doc: Any = None, lines: list[str] | None = None) -> list[Rule]:
    out = []
    if doc and lines is None:
        lines = _lines(doc)
    for eff in ("allow", "ask", "deny"):
        items = block.get(eff) or []
        for t in [items] if isinstance(items, str) else items:
            if not isinstance(t, str):
                raise ValueError(f"{origin}: rule must be text, got {t!r}")
            ln = _line(lines, t) if lines else 0
            out.append(parse_rule(t, eff, locked=locked, origin=f"{origin}:{ln}" if ln else origin))
    if not locked and block.get("locked"):
        out += _block(block["locked"], True, origin, doc, lines)
    return out


_MODE_GRANTS = ("write({modes}/**)", "write({rights})")    # same as modes.ALWAYS_GRANT (rules may not import modes)
_LAYER_CACHE: dict[tuple[str, str], tuple[tuple[Any, Any], list[Layer]]] = {}


def defaults_layers(home: Home, mode: str) -> list[Layer]:
    """Layer 1 (permissions file: allow/ask/deny/locked/fallback) and layer 2 (the mode: its mode file, built by
    `modes.py` from the numbered rights, or `modes.<mode>` of the permissions file in an older tree).  Unknown mode,
    or a mode file that cannot be used -> ValueError with the reason."""
    doc, name = home.key_doc("permissions"), home.rel("permissions")
    modes = home.modes()
    if mode not in modes:
        raise ValueError(f"unknown mode {mode!r}; modes: {', '.join(modes) or 'none'}")
    preset = modes[mode] or {}
    if preset.get("error"):
        raise ValueError(f"mode {mode!r} cannot be used: {preset['error']}")
    fb = doc.meta.get("fallback")
    if fb is not None and fb not in _RANK:
        raise ValueError(f"{name}: fallback must be allow, ask or deny")
    # `home.key_doc` and `home.modes` hand out the same objects until a file's mtime/size changes, so keeping the
    # built layers per pair of objects means an edit takes effect on the very next call.
    key = (str(home.root), mode)
    hit = _LAYER_CACHE.get(key)
    if hit is None or hit[0][0] is not doc or hit[0][1] is not preset:
        lines = _lines(doc)
        if "compiled" in preset:
            origin = preset.get("path") or mode
            rules_ = list(preset["compiled"]) + _block(preset.get("extra") or {}, False, f"{origin}: extra")
        else:
            # creating a mode file would switch an older tree to them: it holds grants even where grant_files lacks it
            rules_ = _block(preset, False, name, doc, lines) + [
                parse_rule(t, "ask", locked=True, origin=f"{name}: mode files hold grants") for t in _MODE_GRANTS]
        built = [Layer("defaults", _block(doc.meta, False, name, doc, lines), fb),
                 Layer(f"mode:{mode}", rules_, no_ask=bool(preset.get("no_ask", mode == "bypass")))]
        hit = _LAYER_CACHE[key] = ((doc, preset), built)
    return [Layer(l.name, list(l.rules), l.fallback, l.no_ask) for l in hit[1]]


def _grants(p: dict[str, Any], origin: str) -> list[Rule]:
    names = lambda k: [p[k]] if isinstance(p.get(k), str) else list(p.get(k) or [])
    sh: list[tuple[str, str]] = []
    for s in names("secrets"):
        sh.append(("secret", s if "/" in s else s + "/*"))
    for s in names("secrets_write"):
        sh.append(("secret-write", s if "/" in s else s + "/*"))
    for t in names("tools"):
        sh += [(t, "*")] if t in ("browser", "pc-control") else [("tool", t)]
    for s in names("servers"):
        sh += [("server", s), ("ssh", s)]
    return [Rule(t, pat, "allow", origin=origin) for t, pat in sh] + _block(p, False, origin)


def agent_layer(agent: Agent) -> Layer:
    """Layer 3 from `agent.permissions`, expanding shorthands secrets / secrets_write /
    tools / servers into rules, plus allow/ask/deny lists."""
    return Layer(f"agent:{agent.name}", _grants(agent.permissions, agent.path or agent.name))


def session_layer(session: SessionState) -> Layer:
    """Layer 4 from `session.rules` (--allow/--deny, /allow, 'always this session')."""
    rs = [r if isinstance(r, Rule) else parse_rule(r, origin="session") for r in session.rules]
    return Layer("session", [replace(r, locked=False) for r in rs])


def build_layers(home: Home, agent: Agent | None, mode: str,
                 session: SessionState | None = None,
                 project_permissions: dict[str, Any] | None = None) -> list[Layer]:
    """All four layers in order [defaults, mode, agent(+project), session]."""
    la = agent_layer(agent) if agent else Layer("agent")
    if project_permissions:
        la.rules += _grants(project_permissions, "project")
    return [*defaults_layers(home, mode), la, session_layer(session) if session else Layer("session")]


# ----------------------------------------------------------------- deciding
def _strictest(rules: list[Rule], check: Check, ctx: RuleContext) -> Rule | None:
    best = None
    for r in rules:
        if match_rule(r, check, ctx) and (best is None or _RANK[r.effect] > _RANK[best.effect]):
            best = r
    return best


def _dec(r: Rule, layer: Layer, kind: str) -> Decision:
    return Decision(r.effect, r, layer.name,
                    f"{kind} {r.effect} {r.text()} ({layer.name}, {r.origin or 'no origin'})")


_SECRETISH = re.compile(r"secrets|\.env|claude", re.I)    # an unreadable shell line that may reach a locked bash deny
BYPASS_LAYER = "mode:bypass"                    # the layer `defaults_layers` builds for the mode named bypass


def is_bypass(layers: list[Layer]) -> bool:
    """Do the layers belong to a mode that never asks (bypass, or a mode file with `fragt-nie: ja`)?"""
    return any(L.no_ask or L.name == BYPASS_LAYER for L in layers)


def mode_never_asks(home: Home, mode: str) -> bool:
    """Does `mode` never ask (bypass, or a mode file with `fragt-nie: ja`)?  A tree whose modes cannot be read
    answers by the name."""
    try:
        return bool((home.modes().get(mode) or {}).get("no_ask", mode == "bypass"))
    except (ValueError, OSError):
        return mode == "bypass"


def decide(layers: list[Layer], check: Check, ctx: RuleContext) -> Decision:
    """The merge of `_decide`, with two fixed limits no mode can switch.  A mode that never asks (bypass) asks nothing:
    every ask (a rule, a locked ask, the fallback, a line that cannot be read safely) becomes allow there, a deny,
    locked or not, stays.  Refused instead: a locked ask on writing a file (the files that hold grants, config.md among
    them).  And an unreadable shell line that names secrets is never let through unasked: denied in a mode that never
    asks, asked in the others even where a rule allows unreadable lines."""
    d = _decide(layers, check, ctx)
    never = is_bypass(layers)
    if check.tool in _ASK_TOOLS and d.rule is not None:
        d = replace(d, reason=f"{d.reason}; {check.arg!r} cannot be read safely (quoting, escape or blank)")
    if check.tool in _ASK_TOOLS and d.effect != "deny" and _SECRETISH.search(check.arg or ""):
        if never:
            return replace(d, effect="deny", reason=f"{d.reason}; bypass refuses an unreadable line that names secrets")
        if d.effect == "allow":
            return replace(d, effect="ask", reason=f"{d.reason}; an unreadable line that names secrets is always asked")
    if d.effect == "ask" and never:
        r = d.rule
        if r and r.locked and r.tool in ("write", "edit"):
            # writing permissions.md, a tools.md or a hook script unasked could lift every locked deny (secret values),
            # and config.md must stay guarded: a mode that lists these files under `fragt` gets a refusal here
            return replace(d, effect="deny", reason=f"{d.reason}; bypass never writes a file that holds grants")
        return replace(d, effect="allow", reason=f"{d.reason}; bypass asks nothing")
    return d


def _decide(layers: list[Layer], check: Check, ctx: RuleContext) -> Decision:
    """3.7 merge: locked rules first (earliest layer wins), then normal rules latest layer
    first (deny > ask > allow inside a layer), else the fallback of layer 1 (no fallback in
    the file -> deny, fail closed).  Unsafe path/URL arguments are denied outright; a line that
    cannot be read safely asks when no rule names it (`shell-unparsed`, catalog number 22)."""
    if unsafe_check(check):
        return Decision("deny", None, layers[0].name if layers else "",
                        f"unsafe argument in {check.tool}({check.arg!r}); refused")
    for L in layers:
        r = _strictest([x for x in L.rules if x.locked], check, ctx)
        if r:
            return _dec(r, L, "locked")
    for L in reversed(layers):
        r = _strictest([x for x in L.rules if not x.locked], check, ctx)
        if r:
            return _dec(r, L, "rule")
    if check.tool in _ASK_TOOLS:
        return Decision("ask", None, layers[0].name if layers else "",
                        f"{check.arg!r} cannot be read safely (quoting, escape or blank); asking")
    fb = next((L.fallback for L in layers if L.fallback), "deny")
    return Decision(fb, None, layers[0].name if layers else "",
                    f"no rule matches {check.tool}({check.arg}); fallback {fb}")


def decide_all(layers: list[Layer], checks: list[Check], ctx: RuleContext) -> Decision:
    """Strictest of several checks: any deny -> deny, else any ask -> ask, else allow.
    Returns the Decision that determined the result."""
    ds = [decide(layers, c, ctx) for c in checks]
    return max(ds, key=lambda d: _RANK[d.effect]) if ds else Decision("allow", reason="no checks")


def parse_bash(cmd: str) -> tuple[list[str], bool]:
    """(simple commands, parsed cleanly) of a shell line in POSIX grammar: split on ; & && || | newlines
    and ( ) plus the bodies of $(...) and backticks; quotes respected; `#` after an unescaped
    space / tab or an operator comments out the line.  Not clean (the caller asks): an
    unbalanced quote, an open substitution, a trailing backslash, a `#` right after an escape,
    an unquoted `#` inside `$(...)` or backticks (shells disagree where such a comment ends), or a
    blank other than space / tab outside quotes (\\v \\f \\r, NBSP: ordinary text to bash)."""
    out: list[str] = []
    ok = True

    def scan(t: str, i: int, close: str | None) -> tuple[str, int]:
        nonlocal ok
        cur, quote, ws = [], "", True                       # ws: at the start of a word
        while i < len(t):
            c, w, ws = t[i], ws, False                      # w: the word started right before c
            if not quote and ((c == "#" and (close or cur and cur[-1][0] == "\\"))   # in `$( )` or backticks the shells
                              or (c not in " \t\n" and (c.isspace() or ord(c) < 32))):   # disagree where a comment ends; `\ #`
                ok = False                                  # ... too; \v \f \r NBSP are no blank to bash: ask
            if quote == "'":
                cur.append(c)
                quote = "" if c == "'" else quote
            elif c == "\\":
                ok = ok and i + 1 < len(t)                  # a trailing backslash escapes nothing
                cur.append(t[i:i + 2])
                i += 1
            elif c == "$" and t[i + 1:i + 2] == "(" or c == "`" and close != "`":
                end = ")" if c == "$" else "`"
                inner, i = scan(t, i + (2 if c == "$" else 1), end)
                flush(inner)
                cur.append("$()")
                continue
            elif quote == '"':
                cur.append(c)
                quote = "" if c == '"' else quote
            elif c == "#" and w:                            # only after unescaped blank / operator
                while i < len(t) and t[i] != "\n":          # comment: the newline still separates
                    i += 1
                continue
            elif close and c == close:
                return "".join(cur), i + 1
            elif c in "'\"":
                cur.append(c)
                quote = c
            elif c in ";\n|()" or (c == "&" and t[i - 1:i] not in (">", "<") and t[i + 1:i + 2] != ">"):
                flush("".join(cur))
                cur, ws = [], True
            else:
                cur.append(c)
                ws = c in " \t"                             # the only blanks bash knows (newline is an operator)
            i += 1
        ok = ok and not quote and not close                 # ran out of text inside a quote / $( / `
        return "".join(cur), i

    def flush(x: str) -> None:
        if x.strip():
            out.append(x.strip())

    rest, _ = scan(cmd, 0, None)
    flush(rest)
    return out, ok


def split_bash(cmd: str) -> list[str]:
    """The simple commands of a shell line (see `parse_bash`)."""
    return parse_bash(cmd)[0]


def match_values(v: Any) -> list[str]:
    """Normalise a `match` argument to separate plain values: lists are walked, strings split
    on whitespace, commas, `;` `<` `>` and quotes, control characters dropped; nothing usable
    -> [].  The dispatcher hands a tool this same list when tools run as functions."""
    items = v if isinstance(v, (list, tuple, set)) else [v]
    out: list[str] = []
    for x in items:
        if isinstance(x, (list, tuple, set)):
            out += match_values(x)
        elif x is not None:
            out += [t for t in re.split(f"[{_VAL_SEP}]+", re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", str(x))) if t]
    return out


def _secret_ref(v: Any) -> str | None:
    m = _SECRET_PH.fullmatch(str(v).strip())
    return m.group(1) if m else None


def checks_for_call(name: str, args: dict[str, Any], *, tool_meta: Any = None,
                    hash_approved: bool = True) -> list[Check]:
    """Table 4.4: dispatcher call -> checks.  Built-in argument names: bash `command`,
    read/write/edit `path`, web `url`, ssh `server`+`command`, spawn `agent`, skill_load `name`,
    secrets `ref` (+ `from_ref`), `<browser|pc-control>.<action>` with `account`.
    `tool_meta` is a model.Tool for external tools.

    A bash / ssh command becomes one check per simple command (plus a `shell-unparsed` check,
    which asks unless a rule names it, when the line does not parse cleanly).  `from_ref` is checked as the
    `name/account` of its parsed placeholder; a ref or placeholder that does not parse adds a
    `secret-malformed` check, which is refused whatever the rules say.  A tool's `match`
    argument yields one check per value (lists, commas, whitespace and newlines separate)."""
    a = args or {}
    s = lambda k: str(a.get(k, ""))
    if name in ("bash", "ssh"):
        subs, clean = parse_bash(s("command"))
        pre = f"{s('server')}:" if name == "ssh" else ""
        cs = [Check(name, pre + c) for c in subs or [s("command").strip()]]
        cs += [] if clean else [Check("shell-unparsed", s("command"))]       # asks unless a rule names it
        return cs + ([Check("server", s("server"))] if name == "ssh" else [])
    if name in PATH_TOOLS:
        return [Check(name, s("path"))]
    if name == "web":
        return [Check("web", s("url"))]
    if name == "spawn":
        return [Check("spawn", s("agent").strip())]     # '' = a copy of oneself: same rights, but a mode may still block it
    if name == "workflow":
        return [Check("spawn", "")]     # it starts copies of oneself, so a mode that blocks sub-agents blocks it too; named agent types are checked when they start (orchestrate.py)
    if name == "skill_load":
        return [Check("skill", s("name"))]
    if name == "secrets":
        ref = s("ref")
        cs = [Check("secret-write", ref)]
        if not _SECRET_REF.fullmatch(ref) or ".." in ref:
            cs.append(Check("secret-malformed", ref))
        if a.get("from_ref"):
            r = _secret_ref(a["from_ref"])
            cs.append(Check("secret", r) if r else Check("secret-malformed", s("from_ref")))
        return cs
    if name.startswith("browser."):
        return [Check("browser", f"{s('account')}:{name[8:]}")]
    if name.startswith("pc-control."):
        return [Check("pc-control", name[11:])]
    if tool_meta is None:
        return [Check("tool", name)]
    mvs = match_values(a.get(tool_meta.match)) if tool_meta.match else []
    cs = [Check("tool", f"{name}:{v}") for v in mvs] or [Check("tool", name)]
    if not hash_approved:
        cs.append(Check("new-tool", name))
    if tool_meta.category not in ("", "none"):
        cs.append(Check("category", tool_meta.category))
    cs += [Check("secret", r) for r in tool_meta.secrets]
    if tool_meta.server not in ("", "any"):
        cs.append(Check("server", tool_meta.server))
    return cs


def resolved_rules(layers: list[Layer]) -> list[tuple[str, Rule]]:
    """For `lm rules`: (layer name, rule) in effective order, each with rule.origin."""
    out = [(L.name, r) for L in layers for r in L.rules if r.locked]
    out += [(L.name, r) for L in reversed(layers)
            for r in sorted(L.rules, key=lambda r: -_RANK[r.effect]) if not r.locked]
    return out


# ------------------------------------------------------------------- asking
class Asker(Protocol):
    async def __call__(self, question: str, check: Check) -> str:
        """Returns 'yes' | 'always' | 'no'.  Timeout is the implementer's job (-> 'no')."""


DESC_MAX = 140                                   # a plain-language line stays one short line
_CTRL = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u2028-\u202e\u2060-\u2069\ufeff]+")   # controls, bidi, zero-width


def _one_line(text: Any, n: int = DESC_MAX) -> str:
    t = re.sub(r"\s+", " ", _CTRL.sub(" ", str(text or ""))).strip()
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def short_path(path: Any, home: str = "", n: int = 48) -> str:
    """A path for a sentence: relative to the tree root when inside it, else the last two parts; cut at the front."""
    p = _one_line(path, 400).replace("\\", "/")
    if home:
        h = home.replace("\\", "/").rstrip("/") + "/"
        if p.startswith(h):
            p = p[len(h):]
    if len(p) > n and "/" in p:
        p = "…/" + "/".join(p.rstrip("/").split("/")[-2:])
    return p if len(p) <= n else "…" + p[-(n - 1):]


_WRAPPERS = {"sudo", "env", "nohup", "time", "command", "exec", "nice", "timeout", "xargs", "doas"}
_SETUP = {"cd", "export", "set", "unset"}         # not the point of a line: the next command is
_FLAG_VALUE = {"-u", "-g", "-h", "-p", "-C", "-D", "-R", "-T", "-U", "-n", "-s", "-k", "-S", "-I", "-L", "-P"}   # a wrapper flag that takes a value
_STD_DIRS = ("/usr/bin/", "/bin/", "/usr/local/bin/", "/usr/sbin/", "/sbin/")
_SHELLS = {"bash", "sh", "zsh", "dash", "fish", "ksh"}


def _program_word(w: str) -> str:
    """A program as the user must see it: a bare name, but a path that is not a standard directory stays whole
    (`./git` is not `git`)."""
    w = w.strip("'\"")
    return w if "/" in w and not w.startswith(_STD_DIRS) else posixpath.basename(w) or w


def bash_program(cmd: str) -> tuple[str, str]:
    """(program, target) of the first real command of a shell line: `cd x && git push origin` -> ("git", "push").
    Variable assignments are skipped; wrappers (sudo, env, timeout, xargs ...) with their flags are looked through
    and named (`xargs -> rm`); `bash -c` says it runs a script.  The target is the first plain word after the program."""
    subs, _ = parse_bash(cmd)
    for sub in subs or [cmd]:
        words = [w for w in sub.split() if not re.match(r"[A-Za-z_]\w*=", w)]
        via: list[str] = []
        while len(words) > 1 and posixpath.basename(words[0]) in _WRAPPERS:
            via.append(posixpath.basename(words[0]))
            words = words[1:]
            while words and (words[0].startswith("-") or re.fullmatch(r"\d+(\.\d+)?[smhd]?", words[0])
                             or (via[-1] == "env" and re.match(r"[A-Za-z_]\w*=", words[0]))):
                flag = words.pop(0)
                if flag in _FLAG_VALUE and words:
                    words.pop(0)
        if not words or posixpath.basename(words[0]) in _SETUP:
            continue
        prog = _program_word(words[0])
        rest = [w.strip("'\"") for w in words[1:]]
        if posixpath.basename(prog) in _SHELLS and "-c" in rest:
            prog, rest = prog + " -c (a script)", []
        prog = " -> ".join([*via, prog])
        return _one_line(prog, 60), _one_line(next((w for w in rest if not w.startswith("-")), ""), 40)
    return "", ""


def describe_call(name: str, args: dict[str, Any] | None, home: str = "", *, meta: Any = None) -> str:
    """One sentence in plain words for the permission question: what the call does, never the command itself.
    A bash / ssh `description` (written by the model) is used when present, else a summary of the first
    program and its target.  Built-in tools are described by the core, from their arguments."""
    a = args or {}
    s = lambda k: str(a.get(k) or "")
    if name in ("bash", "ssh"):
        where = f" on {_one_line(s('server'), 40)}" if name == "ssh" and s("server") else ""
        if (d := _one_line(a.get("description"))):
            return d + where
        prog, target = bash_program(s("command"))
        if not prog:
            return f"Run a command{where}"
        sub = bool(re.fullmatch(r"[a-z][\w-]*", target))          # `git push`: a word after the program reads as part of it
        return f"Run {prog}" + (f" {target}" if sub else f" on {target}" if target else "") + where
    if name == "read":
        return f"View file {short_path(s('path'), home)}"
    if name == "write":
        return f"Write file {short_path(s('path'), home)}"
    if name == "edit":
        return f"Make changes in {short_path(s('path'), home)}"
    if name == "web":
        try:
            host = urlparse(s("url").strip()).hostname or ""
        except ValueError:                                         # e.g. an unclosed [ in the host
            host = ""
        return f"Fetch web page {_one_line(host, 60)}" if host else "Fetch a web page"
    if name == "spawn":
        title = _one_line(s("task").split("\n", 1)[0], 60)
        who = f" {_one_line(s('agent'), 40)}" if s("agent").strip() else ""
        return f"Start sub-agent{who}: {title}" if title else f"Start sub-agent{who}"
    if name == "workflow":
        what = _one_line(s("path"), 60) or "an inline script"
        return f"Run a workflow of sub-agents: {what}"
    if name == "skill_load":
        return f"Read skill {_one_line(s('name'), 60)}"
    if name == "secrets":
        return f"Change secret {_one_line(s('ref'), 60)}"
    if name.startswith("browser."):
        return f"Use the browser ({_one_line(name[8:], 40)})"
    if name.startswith("pc-control."):
        return f"Control the computer ({_one_line(name[11:], 40)})"
    desc = _one_line(getattr(meta, "description", ""), 80) if meta is not None else ""
    return f"Use tool {_one_line(name, 60)}" + (f": {desc}" if desc else "")


def describe_check(check: Check, home: str = "") -> str:
    """The same sentence from a bare check (no arguments at hand): used when the caller gave no description."""
    t, arg = check.tool, check.arg or ""
    if t in ("bash", "ssh"):
        srv, _, cmd = arg.partition(":") if t == "ssh" else ("", "", arg)
        return describe_call(t, {"command": cmd, "server": srv}, home)
    if t in PATH_TOOLS:
        return describe_call(t, {"path": arg}, home)
    if t == "web":
        return describe_call("web", {"url": arg}, home)
    if t == "shell-unparsed":
        return "Run a command line that cannot be checked safely"
    if t == "server":
        return f"Connect to server {_one_line(arg, 60)}"
    if t in ("secret", "secret-write"):
        return f"{'Change' if t == 'secret-write' else 'Use'} secret {_one_line(arg, 60)}"
    if t == "spawn":
        return f"Start sub-agent {_one_line(arg, 60)}"
    return f"{_one_line(t, 40)}: {_one_line(arg, 60)}" if arg else _one_line(t, 40)


def plain_reason(d: Decision) -> str:
    """Why the question is asked, in a short line without the command: the rule that asks, or the missing rule."""
    if d.rule is not None:
        return f"Asked because of rule {_one_line(d.rule.text(), 60)} ({d.layer})"
    return "Asked because no rule allows this yet"


def ask_text(desc: str, d: Decision, check: Check) -> str:
    """The question: the plain sentence, the program (a description written by the model is not trusted alone),
    the reason.  The raw command is not part of it; the asker gets it as `check.arg`."""
    lines = [desc]
    if check.tool in ("bash", "ssh", "shell-unparsed"):
        cmd = check.arg.partition(":")[2] if check.tool == "ssh" else check.arg or ""
        prog = bash_program(cmd)[0]
        # without a program (`export X=...`, `cd x`) the sentence alone would not say what runs: show the start of the line
        lines.append(f"Program: {prog}" if prog else f"Command: {_one_line(cmd, 80)}")
    lines.append("Asked because the command line cannot be read safely; open the command to see it"
                 if check.tool == "shell-unparsed" else plain_reason(d))
    return "\n".join(lines)


async def enforce(layers: list[Layer], checks: list[Check], ctx: RuleContext,
                  asker: Asker | None, session: SessionState, describe: str = "") -> Decision:
    """decide_all + ask routing.  'always' appends an allow Rule to session.rules (a locked
    ask keeps asking).  No asker (non-interactive) -> ask becomes deny.  The question is a plain sentence
    (`describe`, else made from the check) plus the reason; the raw command is in `check.arg`."""
    ds = [(c, decide(layers, c, ctx)) for c in checks]
    for _, d in ds:
        if d.effect == "deny":
            return d
    result = max((d for _, d in ds), key=lambda d: _RANK[d.effect],
                 default=Decision("allow", reason="no checks"))
    for c, d in ds:
        if d.effect != "ask":
            continue
        if asker is None:
            return replace(d, effect="deny", reason=f"{d.reason}; nobody to ask")
        try:
            # the call's own sentence belongs to its main check (the first); the others (a server, a secret ...) say their own
            ans = await asker(ask_text(describe if describe and c is checks[0] else describe_check(c, ctx.home), d, c), c)
        except asyncio.TimeoutError:
            ans = "no"
        if ans not in ("yes", "always"):
            return replace(d, effect="deny", reason=f"{d.reason}; declined")
        if ans == "always":
            arg = _canon(c.arg or "", ctx.home) if c.tool in PATH_TOOLS else c.arg or ""
            session.rules.append(Rule(c.tool, literal(arg), "allow", origin="session:always"))
        result = replace(d, effect="allow", reason=f"{d.reason}; approved")
    return result


# ---------------------------------------------------------- ledger and audit
def _file(home: Home, key: str, folder: str, default: str) -> Path:
    d = home.path(folder)
    d.mkdir(parents=True, exist_ok=True)
    return d / home.files.get(key, default)      # file name from lightmind.md files: if present


def _finite(v: Any) -> bool:
    """A real, finite number (bool, NaN and +-Infinity are not)."""
    if not isinstance(v, (int, float)) or isinstance(v, bool):
        return False
    try:
        return math.isfinite(v)
    except OverflowError:                       # int too large for a float
        return False


class Ledger:
    """Money ledger (file location from lightmind.md paths.state), limit from the
    permissions file `money_limit` ("20 USD/day"; absent or off = no limit)."""

    def __init__(self, home: Home) -> None:
        self.home = home
        self.file = _file(home, "ledger", "state", "ledger.jsonl")

    def _limit(self) -> tuple[float, str] | None:
        v = self.home.key_doc("permissions").meta.get("money_limit")
        if v in (None, False, "off", "none"):
            return None
        m = re.fullmatch(r"\s*([\d.]+)\s*(?:USD)?\s*/\s*(\w+)\s*", str(v))
        if not m or m.group(2) not in _PERIODS:
            raise ValueError(f"money_limit {v!r}: expected '<amount> USD/<{'|'.join(_PERIODS)}>'")
        amount = float(m.group(1))
        if not math.isfinite(amount):
            raise ValueError(f"money_limit {v!r}: amount is not finite")
        return amount, _PERIODS[m.group(2)]

    def _book(self, delta: float) -> None:
        if not _finite(delta):
            raise ValueError(f"ledger refuses non-finite amount {delta!r}")
        with self.file.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"t": datetime.now().isoformat(timespec="seconds"), "usd": delta}) + "\n")

    def booked(self) -> float:
        lim = self._limit()
        if not lim or not self.file.exists():
            return 0.0
        now = datetime.now().strftime(lim[1])
        def bad(c: str):
            raise ValueError(f"ledger holds non-finite value {c}")
        rows = (json.loads(x, parse_constant=bad) for x in
                self.file.read_text(encoding="utf-8").splitlines() if x.strip())
        total = 0.0
        for r in rows:
            u = r["usd"]
            if not _finite(u):
                raise ValueError(f"ledger holds bad value {u!r}")      # fail closed
            if datetime.fromisoformat(r["t"]).strftime(lim[1]) == now:
                total += u
        return total

    def reserve(self, amount_usd: float) -> bool:
        if not _finite(amount_usd) or amount_usd < 0:
            return False
        lim = self._limit()
        if lim and self.booked() + amount_usd > lim[0]:
            return False
        self._book(amount_usd)
        return True

    def settle(self, reserved_usd: float, spent_usd: float) -> None:
        ok = _finite(spent_usd)
        spent = min(max(spent_usd, 0.0), reserved_usd) if ok else reserved_usd   # unknown -> assume full
        self._book(spent - reserved_usd)


def audit(home: Home, kind: str, record: dict[str, Any]) -> None:
    """Append one JSON line to the audit index if `audit.<kind>` is on in the
    permissions file."""
    on = (home.key_doc("permissions").meta.get("audit") or {}).get(kind)
    if on is False or on is None or str(on).lower() in ("off", "false", "no", "none"):
        return
    line = json.dumps({"t": datetime.now().isoformat(timespec="seconds"), "kind": kind, **record},
                      ensure_ascii=False)
    with _file(home, "audit_index", "audit", "index.jsonl").open("a", encoding="utf-8") as f:
        f.write(line + "\n")
