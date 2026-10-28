"""Modes from numbered rights: reads the catalog (`rechte.md`) and the mode files (`modes/<mode>.md`) and turns them
into the rules of the mode layer.  Nothing is decided here; `rules.decide` judges the rules this module builds.

May import: model, rules, files (through the Home it is given).  A mode file says `regel: darf-nur` or
`regel: darf-alles-ausser`, lists catalog numbers under `rechte:` and, as the third state, under `fragt:`.
Per number the result is one of ALLOWED, ASKS, BLOCKED:
- BLOCKED: a locked deny on every rule of the number (no agent, session or later layer lifts it).
- ASKS: an ask on every rule; locked when the catalog says `Fragen: fest`.
- ALLOWED: nothing, the base layer decides (an open default stays open, a closed one such as secrets stays closed and
  opens only through `geheimnisse:`, an agent or the session); a number whose default is `frage` gets an allow.
A number that lies inside another one (`Teil von`) keeps its own state: the rules of the outer number skip it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .files import MODES, RIGHTS, _front, parse_frontmatter
from .model import Rule
from .rules import parse_rule

ALLOWED, ASKS, BLOCKED = "erlaubt", "fragt", "gesperrt"
ONLY, ALL_BUT = "darf-nur", "darf-alles-ausser"
BASES = ("offen", "zu", "frage")              # offen: base allows; zu: base denies (secrets); frage: the core asks by itself
ASKING = ("ja", "fest", "nein")               # may the number ask, and is that ask locked
GRANT_FILES = "@grant_files"                  # the only rule reference: the `grant_files` list of permissions.md
ALWAYS_GRANT = ("write({modes}/**)", "write({rights})")   # the mode files and the catalog hold grants in every tree
_KEYS = {"label", "color", "order", "regel", "rechte", "fragt", "geheimnisse", "fragt-nie", "extra"}
_SECRET = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(?:/(?:[A-Za-z0-9][A-Za-z0-9._-]*|\*))?")
_LEADING = re.compile(r"\s*(\d+)(?![.\d])(?:\s*-\s*(\d+)(?![.\d]))?")


@dataclass(frozen=True)
class Right:
    """One numbered row of the catalog."""
    number: int
    label: str
    rules: tuple[str, ...]                    # rule texts as in permissions.md, `@grant_files` already expanded
    base: str                                 # one of BASES
    asking: str                               # one of ASKING
    parent: int | None = None                 # `Teil von`: the number this one lies inside
    group: str = ""


@dataclass
class Catalog:
    """The parsed `rechte.md`: numbered rights, and the fixed limits (`F1` ...) that no mode can switch."""
    path: str
    rights: dict[int, Right] = field(default_factory=dict)
    fixed: list[tuple[str, str, tuple[str, ...]]] = field(default_factory=list)     # (id, label, rules)

    def children(self, number: int) -> list[int]:
        """Every number that lies inside `number`, at any depth."""
        out, todo = [], [number]
        while todo:
            n = todo.pop()
            kids = [r.number for r in self.rights.values() if r.parent == n]
            out += kids
            todo += kids
        return out


# --------------------------------------------------------------------- catalog
def _cells(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def _rule_texts(cell: str) -> list[str]:
    """`read(**)`, `write(**)` -> the rule texts; backticks name them, else a comma list."""
    ticks = re.findall(r"`([^`]+)`", cell)
    return [t.strip() for t in ticks] if ticks else [t.strip() for t in cell.split(",") if t.strip()]


def parse_catalog(text: str, origin: str, grant_files: list[str] | None = None) -> Catalog:
    """Read the Markdown tables of the catalog: `## <group>` headings, rows `| Nr | Recht | Regeln | Grundsatz |
    Fragen | Teil von |`, and a table whose ids start with `F` (the fixed limits).  `grant_files` expands
    `@grant_files`.  Raises ValueError naming the line on any row it cannot use."""
    _, body = parse_frontmatter(text)
    cat, group, fenced = Catalog(origin), "", False
    for no, line in enumerate(body.replace("\r\n", "\n").split("\n"), 1):
        s = line.strip()
        if s.startswith("```"):
            fenced = not fenced
        if fenced or not s:
            continue
        if s.startswith("#"):
            group = s.lstrip("#").strip()
            continue
        if not s.startswith("|"):
            continue
        cells = _cells(s)
        head = cells[0].lower()
        if head in ("nr", "kennung", "") or set(cells[0]) <= set("-: "):
            continue                                         # header or separator row
        where = f"{origin}: line {no}"
        if head.startswith("f"):
            cat.fixed.append((cells[0], cells[1] if len(cells) > 1 else "",
                              tuple(_rule_texts(cells[2])) if len(cells) > 2 else ()))
            continue
        if len(cells) < 5 or not cells[0].isdigit():
            raise ValueError(f"{where}: a right needs Nr, Recht, Regeln, Grundsatz, Fragen (got {s!r})")
        number = int(cells[0])
        if number in cat.rights:
            raise ValueError(f"{where}: number {number} is there twice")
        texts: list[str] = []
        for t in _rule_texts(cells[2]):
            if t == GRANT_FILES:
                texts += list(grant_files or [])
            elif t.startswith("@"):
                raise ValueError(f"{where}: unknown reference {t!r} (only {GRANT_FILES})")
            else:
                parse_rule(t, origin=where)                  # raises on bad syntax
                texts.append(t)
        base, asking = cells[3].lower(), cells[4].lower()
        if base not in BASES:
            raise ValueError(f"{where}: Grundsatz must be {', '.join(BASES)}, not {cells[3]!r}")
        if asking not in ASKING:
            raise ValueError(f"{where}: Fragen must be {', '.join(ASKING)}, not {cells[4]!r}")
        parent_cell = cells[5] if len(cells) > 5 else ""
        m = _LEADING.match(parent_cell)
        if parent_cell and not m:
            raise ValueError(f"{where}: Teil von must be a number, not {parent_cell!r}")
        cat.rights[number] = Right(number, cells[1], tuple(texts), base, asking,
                                   int(m.group(1)) if m else None, group)
    for r in cat.rights.values():
        if r.parent is not None and r.parent not in cat.rights:
            raise ValueError(f"{origin}: number {r.number} is part of {r.parent}, which the catalog does not have")
        seen, n = {r.number}, r.parent
        while n is not None:
            if n in seen:
                raise ValueError(f"{origin}: number {r.number} lies inside itself (Teil von goes round)")
            seen.add(n)
            n = cat.rights[n].parent
    if not cat.rights:
        raise ValueError(f"{origin}: no numbered right found (rows like `| 11 | Dateien lesen | `read(**)` | offen | ja |`)")
    return cat


# ------------------------------------------------------------------ mode files
@dataclass
class ModeSpec:
    """One mode file, checked against the catalog."""
    key: str
    path: str
    label: str
    color: str
    order: int | None
    text: str
    toggle: str                               # ONLY or ALL_BUT
    listed: set[int]
    asks: set[int]
    secrets: list[str]
    never_asks: bool
    extra: dict[str, Any]
    catalog: Catalog
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)          # worth knowing, not wrong

    def state(self, number: int) -> str:
        """ALLOWED, ASKS or BLOCKED for one catalog number.  A number named in no list is blocked under darf-nur;
        under darf-alles-ausser it takes the state of the number it lies inside (`Teil von`), so blocking 12 blocks
        13 and 14 as well, and asking for 13 asks for 14 (a number that cannot ask is allowed then)."""
        if number in self.asks:
            return ASKS
        if number in self.listed:
            return ALLOWED if self.toggle == ONLY else BLOCKED
        if self.toggle == ONLY:
            return BLOCKED
        right = self.catalog.rights.get(number)
        if right is None or right.parent is None:
            return ALLOWED
        st = self.state(right.parent)
        return ALLOWED if st == ASKS and right.asking == "nein" else st


def _norm_toggle(v: Any) -> str:
    s = str(v or "").strip().lower().rstrip(":").replace("ß", "ss").replace("ä", "ae")
    return re.sub(r"[\s_]+", "-", s)


def _numbers(v: Any, field_name: str, cat: Catalog, where: str) -> set[int]:
    """`[12, "13 Rechte-Dateien", "51-55"]` -> {12, 13, 51, ..., 55}; an unknown number is an error."""
    if v is None or v == "":
        return set()
    items = v if isinstance(v, list) else str(v).split(",")
    out: set[int] = set()
    for x in items:
        if isinstance(x, (bool, float)):
            raise ValueError(f"{where}: {field_name}: {x!r} is no whole number")
        m = _LEADING.match(str(x))
        if not m:
            raise ValueError(f"{where}: {field_name}: {x!r} is no number of the catalog")
        lo = int(m.group(1))
        hi = int(m.group(2)) if m.group(2) else lo
        span = [n for n in range(lo, hi + 1) if n in cat.rights] if m.group(2) else [lo]
        if not span:
            raise ValueError(f"{where}: {field_name}: {x!r} holds no number of the catalog")
        for n in span:
            if n not in cat.rights:
                raise ValueError(f"{where}: {field_name}: number {n} is not in {Path(cat.path).name}")
            out.add(n)
    return out


def read_front(text: str, name: str) -> tuple[dict[str, Any], str]:
    """Strict frontmatter of a mode file: YAML or an error naming the line (no lenient flat reading, which would
    turn `fragt: [13, 51` into a text and lose numbers)."""
    text = text.lstrip("\ufeff").replace("\r\n", "\n")
    split = _front(text)
    if split is None:
        raise ValueError(f"{name}: no frontmatter, the file must start with a --- block")
    try:
        meta = yaml.safe_load(split[0]) or {}
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None)
        raise ValueError(f"{name}: {'line %d: ' % (mark.line + 2) if mark else ''}"
                         f"{getattr(e, 'problem', '') or 'not valid YAML'}") from None
    if not isinstance(meta, dict):
        raise ValueError(f"{name}: the frontmatter is not a mapping")
    return meta, split[1]


def _yes(v: Any) -> bool:
    return v is True or str(v).strip().lower() in ("ja", "yes", "true", "on")


def parse_mode(key: str, path: str, meta: dict[str, Any], body: str, cat: Catalog) -> ModeSpec:
    """Check a mode file's frontmatter against the catalog.  Raises ValueError with the reason (fail closed: a mode
    that cannot be read is not usable, the other modes are)."""
    where = Path(path).name
    toggle = _norm_toggle(meta.get("regel"))
    if toggle not in (ONLY, ALL_BUT):
        raise ValueError(f"{where}: `regel:` must be darf-nur or darf-alles-ausser, not {meta.get('regel')!r}")
    listed = _numbers(meta.get("rechte"), "rechte", cat, where)
    asks = _numbers(meta.get("fragt"), "fragt", cat, where)
    if both := sorted(listed & asks):
        raise ValueError(f"{where}: {', '.join(map(str, both))} under rechte and fragt at once; put each number in one list")
    if never := sorted(n for n in asks if cat.rights[n].asking == "nein"):
        raise ValueError(f"{where}: {', '.join(map(str, never))} cannot ask (catalog: Fragen nein)")
    secrets = [str(s).strip() for s in (meta.get("geheimnisse") or [])] if not isinstance(meta.get("geheimnisse"), str) \
        else [s.strip() for s in str(meta["geheimnisse"]).split(",") if s.strip()]
    if bad := [s for s in secrets if not _SECRET.fullmatch(s) or ".." in s]:
        raise ValueError(f"{where}: geheimnisse: {', '.join(bad)} is no secret name (service or service/account)")
    if secrets and 31 in asks:
        raise ValueError(f"{where}: geheimnisse and fragt 31 together: the ask would cover the listed secrets too; "
                         "drop one of them")
    if unknown := sorted(set(map(str, meta)) - _KEYS):
        raise ValueError(f"{where}: unknown key(s) {', '.join(unknown)} (a typo would switch nothing); "
                         f"known: {', '.join(sorted(_KEYS))}")
    extra = meta.get("extra") or {}
    if not isinstance(extra, dict):
        raise ValueError(f"{where}: `extra:` must be a mapping with allow / ask / deny / locked")
    for block, locked in ((extra, False), (extra.get("locked") or {}, True)):
        if not isinstance(block, dict):
            raise ValueError(f"{where}: extra: `locked:` must be a mapping with allow / ask / deny")
        if odd := sorted(set(map(str, block)) - {"allow", "ask", "deny"} - ({"locked"} if not locked else set())):
            raise ValueError(f"{where}: extra: unknown key(s) {', '.join(odd)}")
        for eff in ("allow", "ask", "deny"):
            items = block.get(eff)
            for t in [items] if isinstance(items, str) else items or []:
                if not isinstance(t, str):
                    raise ValueError(f"{where}: extra: rule must be text, got {t!r}")
                parse_rule(t, eff, locked=locked, origin=f"{where}: extra")
    order = meta.get("order")
    if order is not None and (isinstance(order, bool) or not isinstance(order, int)):
        raise ValueError(f"{where}: `order:` must be a whole number")
    spec = ModeSpec(key, path, str(meta.get("label") or key), str(meta.get("color") or ""), order, body.strip(),
                    toggle, listed, asks, [s if "/" in s else s + "/*" for s in secrets],
                    key == "bypass" or _yes(meta.get("fragt-nie")), extra, cat)
    if spec.secrets and 31 in cat.rights and spec.state(31) == BLOCKED:
        raise ValueError(f"{where}: geheimnisse needs number 31 ({cat.rights[31].label}), which this mode blocks")
    if spec.never_asks and asks:
        spec.warnings.append(f"{where}: this mode never asks; fragt {sorted(asks)} is treated as allowed "
                             "(files that hold grants as blocked)")
    if 13 in cat.rights and spec.state(13) == ALLOWED:
        spec.notes.append(f"{where}: agents may write the files that hold grants unasked (number 13 allowed)")
    return spec


def compile_rules(spec: ModeSpec, cat: Catalog) -> list[Rule]:
    """The rules of the mode layer for `spec` (see the module docstring)."""
    rel = f"{MODES}/{Path(spec.path).name}"
    out: list[Rule] = []
    for n, right in sorted(cat.rights.items()):
        state = spec.state(n)
        if state == ALLOWED and (right.base != "frage" or n not in spec.listed):
            continue                                        # `frage` is lifted only by naming it under darf-nur
        if state == ASKS and spec.never_asks and right.base == "zu":
            continue                                        # never asking must not open what is closed (secrets)
        effect = {ALLOWED: "allow", ASKS: "ask", BLOCKED: "deny"}[state]
        locked = state == BLOCKED or (state == ASKS and right.asking == "fest")
        skip = tuple(parse_rule(t) for c in cat.children(n) if spec.state(c) != state for t in cat.rights[c].rules)
        origin = f"{rel}: Nr {n} {right.label}"
        for t in right.rules:
            r = parse_rule(t, effect, locked=locked, origin=origin)
            out.append(Rule(r.tool, r.pattern, r.effect, r.locked, r.until, r.origin, skip))
    for s in spec.secrets:
        out.append(Rule("secret", s, "allow", origin=f"{rel}: geheimnisse"))
    return out


def summary(spec: ModeSpec, cat: Catalog) -> dict[str, list[str]]:
    """{'fragt': [...], 'gesperrt': [...]} as `<nr> <label>` lines, for the mode menu."""
    out: dict[str, list[str]] = {ASKS: [], BLOCKED: []}
    for n, right in sorted(cat.rights.items()):
        st = spec.state(n)
        if st in out:
            out[st].append(f"{n} {right.label}")
    return out


# --------------------------------------------------------------- tree access
def modes_dir(home: Any) -> Path:
    return home.path("modes") if "modes" in home.paths else home.root / MODES


def catalog_path(home: Any) -> Path:
    return home.path("rights") if "rights" in home.paths else home.root / RIGHTS


def mode_files(home: Any) -> list[Path]:
    """The mode files of the tree: `<modes>/*.md` without README.md and files starting with `_` or `.`."""
    d = modes_dir(home)
    try:
        return sorted(p for p in d.iterdir() if p.suffix == ".md" and p.is_file()
                      and p.name[0] not in "_." and p.name.lower() != "readme.md")
    except OSError:
        return []


def _stat(p: Path) -> Any:
    try:
        st = p.stat()
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def load_catalog(home: Any) -> Catalog:
    """The catalog of the tree, with `@grant_files` taken from permissions.md.  Raises ValueError with the reason."""
    p = catalog_path(home)
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as e:
        raise ValueError(f"the catalog {p.name} cannot be read ({e.__class__.__name__}); modes/ needs it") from e
    try:
        grants = home.key_doc("permissions").meta.get("grant_files") or []
    except Exception:                                   # noqa: BLE001 - no permissions file: no grant files to name
        grants = []
    if not isinstance(grants, list) or not all(isinstance(g, str) for g in grants):
        raise ValueError("permissions.md: grant_files must be a list of rules")
    return parse_catalog(text, str(p), list(grants) + [g for g in ALWAYS_GRANT if g not in grants])


def state_key(home: Any) -> tuple[Any, ...]:
    """Size and time of every file the modes are built from (mode files, catalog): a cache that depends on the
    modes keys on this, so an edit of a mode file is seen at once."""
    return tuple((str(p), _stat(p)) for p in mode_files(home)) + (_stat(catalog_path(home)),)


def presets(home: Any) -> dict[str, dict] | None:
    """key -> preset of every mode file, ordered by `order` (modes without one last), or None when the tree has no
    mode file (then the `modes:` block of permissions.md counts).  A preset holds label, color, order, text, no_ask,
    path, warnings, and either `compiled` (the rules of its layer) and `summary`, or `error` (why the mode cannot be
    used).  Cached on the Home by the size and time of every file it read."""
    files = mode_files(home)
    if not files:
        if catalog_path(home).exists():                 # a tree that switched: no silent way back to the old block
            raise ValueError(f"{modes_dir(home).name}/ holds no mode file, but {catalog_path(home).name} is there; "
                             f"restore the mode files (`lm update` brings the shipped ones back only into a new tree)")
        return None
    try:
        perm = home.path("permissions")
    except Exception:                                   # noqa: BLE001 - a tree without permissions file
        perm = None
    key = (state_key(home), _stat(perm) if perm else None)
    hit = getattr(home, "_modes_cache", None)
    if hit and hit[0] == key:
        return hit[1]
    try:
        cat, cat_error = load_catalog(home), ""
    except ValueError as e:
        cat, cat_error = None, str(e)
    specs: list[tuple[Any, str, dict]] = []
    for p in files:
        k = p.stem
        base = {"label": k, "color": "", "order": None, "text": "", "no_ask": k == "bypass", "path": str(p),
                "warnings": [], "notes": []}
        try:
            meta, body = read_front(p.read_text(encoding="utf-8"), p.name)
            base.update(label=str(meta.get("label") or k), color=str(meta.get("color") or ""), text=body.strip())
            if cat is None:
                raise ValueError(cat_error)
            spec = parse_mode(k, str(p), meta, body, cat)
            base.update(order=spec.order, no_ask=spec.never_asks, warnings=spec.warnings, notes=spec.notes,
                        compiled=compile_rules(spec, cat), summary=summary(spec, cat), extra=spec.extra)
        except Exception as e:                          # noqa: BLE001 - YAML, a bad number, a bad rule: the mode says why
            base["error"] = str(e) or e.__class__.__name__
        specs.append((base["order"] is None, base["order"] or 0, k, base))
    out = {k: b for _, _, k, b in sorted(specs, key=lambda s: s[:3])}
    home._modes_cache = (key, out)
    return out


def findings(home: Any) -> list[tuple[str, str]]:
    """What `lm check` says about the mode files: ('fail' | 'warn' | 'info', text).  Empty for a tree without mode
    files.  A mode that cannot be used fails; a fixed limit of the catalog whose rules permissions.md does not deny,
    and a `modes:` block left in permissions.md, are reported too."""
    found = presets(home)
    if found is None:
        return []
    out: list[tuple[str, str]] = []
    for k, m in found.items():
        if m.get("error"):
            out.append(("fail", f"mode {k}: {m['error']}"))
        out += [("warn", w) for w in m.get("warnings") or []]
        out += [("info", w) for w in m.get("notes") or []]
    try:
        cat = load_catalog(home)
        meta = home.key_doc("permissions").meta
    except Exception:                                   # noqa: BLE001 - reported with the modes above
        return out
    held = list((meta.get("locked") or {}).get("deny") or []) + list(meta.get("deny") or [])
    for fid, label, texts in cat.fixed:
        if missing := [t for t in texts if t not in held]:
            out.append(("warn", f"{Path(cat.path).name} {fid} ({label}): permissions.md does not deny {', '.join(missing)}"))
    if meta.get("modes"):
        out.append(("info", "permissions.md still has a `modes:` block: only cores older than the mode files read it; "
                            "delete it once every machine runs `lm update`"))
    return out
