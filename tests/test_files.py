import os
import shutil
import time
from pathlib import Path

import pytest

from lightmind.files import (Home, PathsError, parse_frontmatter, parse_rows, parse_size,
                             split_arrow)

STRICT = Path(__file__).parent / "fixtures" / "permissions_strict.md"


def test_frontmatter_basic_and_crlf():
    m, b = parse_frontmatter("---\r\na: 1\r\nb: [x, y]\r\n---\r\nbody\r\n")
    assert m == {"a": 1, "b": ["x", "y"]} and b == "body\n"


def test_frontmatter_absent_or_unclosed():
    assert parse_frontmatter("just text") == ({}, "just text")
    assert parse_frontmatter("---\na: 1\nno end")[0] == {}


def test_rows_sections_comments_fences():
    body = """intro
# Start menu   # label | key | action
- New chat | n | new_chat   # comment
- Quit|q|quit
```
- ignored | row
```
# Slash commands
- /landing → lm workflow x
"""
    r = parse_rows(body)
    assert r["Start menu"] == [["New chat", "n", "new_chat"], ["Quit", "q", "quit"]]
    assert split_arrow(r["Slash commands"][0][0]) == ("/landing", "lm workflow x")


def test_rows_hash_inside_cell_is_kept():
    assert parse_rows("# S\n- a#b | c\n")["S"] == [["a#b", "c"]]


def test_parse_size():
    assert parse_size("200k") == 200_000 and parse_size("1m") == 1_000_000 and parse_size(5) == 5


def test_home_paths_come_from_entry_file(home):
    assert home.path("permissions").name == "permissions.md"
    assert home.rel("agents") == "agents"
    with pytest.raises(PathsError):
        home.path("nope")


def test_home_requires_entry_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        Home(tmp_path)


def test_renaming_a_folder_only_needs_the_entry_file(tree):
    (tree / "agents").rename(tree / "bots")
    p = tree / "lightmind.md"
    p.write_text(p.read_text(encoding="utf-8").replace("agents: agents/", "agents: bots/"), encoding="utf-8")
    assert Home(tree).list_agents() == ["example-agent"]


def test_resolve_root(monkeypatch, tmp_path):
    monkeypatch.setenv("LIGHTMIND_HOME", str(tmp_path))
    assert Home.resolve_root() == tmp_path
    assert Home.resolve_root(str(tmp_path / "x")) == tmp_path / "x"


def test_mtime_cache(home, tree):
    p = tree / "notes.md"
    p.write_text("---\nlayout: dashboard\n---\nx\n", encoding="utf-8")
    d1 = home.doc("notes.md")
    assert home.doc("notes.md") is d1
    p.write_text("---\nlayout: orbit\n---\nx\n", encoding="utf-8")
    os.utime(p, (time.time() + 5, time.time() + 5))
    assert home.doc("notes.md").meta["layout"] == "orbit"


def test_load_agent(home):
    a = home.load_agent("example-agent")
    assert a.context_limit == 200_000 and a.mode == "auto"
    assert [(t.kind, t.name) for t in a.tools][0] == ("skill", "run-tracking")
    assert a.body.startswith("You are")


def test_skills_claude_code_format_and_dropin(home, tree):
    names = {s.name for s in home.list_skills()}
    assert {"run-tracking", "example-skill"} <= names
    d = tree / "skills" / "foreign"            # unchanged Claude Code skill folder
    (d / "scripts").mkdir(parents=True)
    (d / "SKILL.md").write_text("---\nname: foreign\ndescription: from the internet\n---\nhi\n", encoding="utf-8")
    assert next(s for s in home.list_skills() if s.name == "foreign").description == "from the internet"


def test_tools_dropin_and_pdf_template(home, tree):
    t = home.load_tool("pdf")
    assert t.run == "uv run pdf.py" and t.params["command"]["required"]
    assert "# /// script" in (tree / "tools" / "pdf" / "pdf.py").read_text(encoding="utf-8")
    d = tree / "tools" / "mine"
    d.mkdir()
    (d / "tool.md").write_text("---\nname: mine\ndescription: d\nrun: uv run x.py\n---\n", encoding="utf-8")
    assert {t.name for t in home.list_tools()} == {"pdf", "mine", "vault", "browser", "pc", "triggers", "text-stats"}


def test_providers_and_permissions_templates(home):
    """The shipped permissions.md only has to load and be well-formed; which rules it ships is
    the template's business (and changes), so no rule line is asserted here."""
    p = home.load_providers()
    assert p.default == "claude-abo" and p.accounts["claude-abo"].type == "claude-subscription"
    perm = home.key_doc("permissions").meta
    assert "modes" not in perm
    assert home.modes() and all(isinstance(v, dict) for v in home.modes().values())
    assert perm["fallback"] in ("allow", "ask", "deny")
    assert home.prices()["opus"][0] > 0


def test_strict_permissions_fixture_uses_placeholders(tree):
    """Rule lines are asserted against a fixture permissions file, not the shipped template."""
    shutil.copy(STRICT, tree / "permissions.md")
    perm = Home(tree).key_doc("permissions").meta
    assert set(perm["modes"]) == {"auto", "plan", "bypass"}
    assert "read({secrets}/**/*.env)" in perm["locked"]["deny"]
    assert "read(secrets/**/*.env)" not in perm["locked"]["deny"]
    assert perm["fallback"] == "deny"


def test_flat_frontmatter_with_an_unquoted_colon_is_read_like_claude_code():
    from lightmind.files import parse_frontmatter
    meta, body = parse_frontmatter("---\nname: arsenal\ndescription: Use when a design needs color and spacing: WebGL, 3D\n---\nbody\n")
    assert meta == {"name": "arsenal", "description": "Use when a design needs color and spacing: WebGL, 3D"}
    assert body == "body\n"


def test_nested_invalid_frontmatter_still_raises():
    import pytest
    from lightmind.files import parse_frontmatter
    with pytest.raises(ValueError):
        parse_frontmatter("---\npaths:\n  a: [b\n---\n")


def test_one_broken_skill_does_not_hide_the_others(home):
    import pytest
    bad = home.path("skills") / "broken"
    bad.mkdir()
    (bad / "SKILL.md").write_text("---\nname: broken\nlist:\n  - [x\n---\n", encoding="utf-8")
    with pytest.warns(UserWarning, match="broken"):
        names = [s.name for s in home.list_skills()]
    assert "broken" not in names and names
