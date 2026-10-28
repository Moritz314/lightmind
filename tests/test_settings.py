"""settings.py: line-by-line writing with a counter-check, CRLF and BOM kept, conflicts, the cache after a write."""
import os
from datetime import date

import pytest

from lightmind import settings as S
from lightmind.files import Home
from lightmind.init import init_tree

AGENT = """---
name: my-agent
description: One line   # about it
provider: ""          # empty = defaults.provider
model: ""             # empty = defaults.model
effort: medium        # low | medium | high | max
mode: ""              # empty = defaults.mode
may_spawn: []
budget:
  tokens: 400k        # per run
  usd: 2              # per run

  # a note between the keys
permissions:          # extra rules
  allow: []
---

# my-agent

Body text, with `provider: x` in it.
"""


def changed_lines(old: str, new: str) -> list[str]:
    """The lines of `new` that `old` does not have (a plain set difference is enough for these small files)."""
    return [ln for ln in new.splitlines() if ln not in old.splitlines()]


# ------------------------------------------------------------------ one line, nothing else
def at(text: str, prefix: str) -> int:
    return next(i for i, ln in enumerate(text.splitlines()) if ln.startswith(prefix))


def test_a_value_replaces_its_own_line_and_nothing_else():
    new = S.edit_text(AGENT, {("model",): "opus"})
    (line,) = changed_lines(AGENT, new)
    old = AGENT.splitlines()[at(AGENT, "model:")]
    assert line.startswith("model: opus ") and line.endswith("# empty = defaults.model")
    assert line.index("#") == old.index("#")                                                # the comment stays in its column
    assert new.splitlines()[:3] == AGENT.splitlines()[:3] and new.endswith("Body text, with `provider: x` in it.\n")
    assert S.front_meta(new)["model"] == "opus" and S.front_meta(new)["provider"] == ""


def test_types_and_quoting_come_from_yaml_dump():
    new = S.edit_text(AGENT, {("provider",): "claude-abo", ("effort",): "true", ("mode",): "plan", ("budget", "usd"): 2.5,
                              ("may_spawn",): ["a", "b"], ("name",): "no: way"})
    meta = S.front_meta(new)
    assert meta["effort"] == "true" and isinstance(meta["effort"], str)                    # a string that looks like a bool stays a string
    assert meta["budget"]["usd"] == 2.5 and meta["may_spawn"] == ["a", "b"] and meta["name"] == "no: way"
    assert "effort: 'true'" in new and "may_spawn: [a, b]" in new


def test_a_comment_with_a_hash_and_a_quoted_value_with_one():
    text = '---\nname: "a # b"      # c # d\nmodel: x\n---\n'
    new = S.edit_text(text, {("name",): "z # y"})
    assert S.front_meta(new)["name"] == "z # y" and new.splitlines()[1].endswith("# c # d")


def test_a_longer_value_pushes_the_comment_only_as_far_as_it_must():
    new = S.edit_text(AGENT, {("effort",): "a-rather-long-value-here"})
    assert "effort: a-rather-long-value-here # low | medium | high | max" in new


# ------------------------------------------------------------------ missing keys and removal
def test_a_missing_key_goes_behind_an_anchor_else_to_the_end():
    new = S.edit_text(AGENT, {("project",): "crm"}, after=("mode", "effort"))
    assert new.splitlines()[at(new, "mode:") + 1] == "project: crm"
    no_anchor = S.edit_text(AGENT, {("project",): "crm"})
    assert no_anchor.splitlines()[no_anchor.splitlines().index("---", 1) - 1] == "project: crm"      # last line of the frontmatter
    assert S.front_meta(new)["project"] == "crm" and S.front_meta(no_anchor)["project"] == "crm"


def test_a_nested_key_goes_to_the_end_of_its_section_before_the_next_comment_and_key():
    new = S.edit_text(AGENT, {("budget", "turns"): 9})
    lines = new.splitlines()
    assert lines[at(new, "  usd:") + 1] == "  turns: 9"
    assert S.front_meta(new)["budget"] == {"tokens": "400k", "usd": 2, "turns": 9}
    assert lines[at(new, "  turns:") + 1] == "" and lines[at(new, "  turns:") + 2].strip().startswith("# a note")


def test_a_missing_section_is_added_at_the_end_with_its_key_and_an_empty_section_gets_children():
    new = S.edit_text(AGENT, {("sessions", "open"): "tab", ("permissions", "ask"): ["bash(*)"]})
    meta = S.front_meta(new)
    assert meta["sessions"] == {"open": "tab"} and meta["permissions"]["ask"] == ["bash(*)"] and meta["permissions"]["allow"] == []
    empty = S.edit_text("---\nsessions:   # none yet\n---\n", {("sessions", "open"): "tab"})
    assert S.front_meta(empty) == {"sessions": {"open": "tab"}} and "sessions:   # none yet" in empty


def test_removing_a_key_and_a_missing_key_removed():
    new = S.edit_text(AGENT, {("model",): None, ("nothing",): None, ("budget", "ghost"): None})
    assert "model" not in S.front_meta(new) and changed_lines(AGENT, new) == []
    assert len(new.splitlines()) == len(AGENT.splitlines()) - 1


def test_the_text_below_the_frontmatter_is_never_touched():
    new = S.edit_text(AGENT, {("provider",): "x", ("model",): "y", ("project",): "z"})
    assert new.split("\n---\n", 1)[1] == AGENT.split("\n---\n", 1)[1]


# ------------------------------------------------------------------ line endings and BOM
def test_crlf_and_a_bom_stay_and_inserted_lines_follow_the_file():
    bom = S.BOM
    raw = (bom + AGENT).replace("\n", "\r\n")
    new = S.edit_text(raw, {("model",): "opus", ("project",): "crm", ("budget", "turns"): 9, ("sessions", "open"): "tab"}, after=("mode",))
    data = new.encode("utf-8")
    assert data.startswith(b"\xef\xbb\xbf---\r\n")                                        # the BOM is still the first thing
    assert data.count(b"\n") == data.count(b"\r\n")                                       # not one bare LF, inserted lines included
    meta = S.front_meta(new)
    assert (meta["model"], meta["project"], meta["budget"]["turns"], meta["sessions"]) == ("opus", "crm", 9, {"open": "tab"})
    assert new.replace("\r\n", "\n").endswith("Body text, with `provider: x` in it.\n")


def test_an_lf_file_stays_lf():
    new = S.edit_text(AGENT, {("model",): "opus", ("project",): "k"})
    assert "\r" not in new


# ------------------------------------------------------------------ the counter-check
def test_a_value_that_spans_several_lines_is_refused():
    text = "---\nmay_spawn:\n  - a\n  - b\nmodel: x\n---\n"
    with pytest.raises(S.SettingsError, match="spans several lines"):
        S.edit_text(text, {("may_spawn",): ["c"]})
    with pytest.raises(S.SettingsError, match="not a section"):
        S.edit_text("---\nbudget: {tokens: 4}\n---\n", {("budget", "usd"): 2})


def test_a_change_that_does_not_come_out_as_asked_writes_nothing(tmp_path, monkeypatch):
    p = tmp_path / "agentMaster.md"
    p.write_bytes(AGENT.encode("utf-8"))
    h = Home(init_tree(tmp_path / "lm").root)
    monkeypatch.setattr(S, "dump_value", lambda v: "[unclosed")                              # a bad dump: the file would break
    with pytest.raises(S.SettingsError, match="counter-check"):
        S.save(h, p, {("model",): "opus"})
    monkeypatch.setattr(S, "dump_value", lambda v: "something-else")                         # parses, but not to what was asked
    with pytest.raises(S.SettingsError, match="counter-check"):
        S.save(h, p, {("model",): "opus"})
    assert p.read_bytes() == AGENT.encode("utf-8") and not list(tmp_path.glob("*.tmp-*"))


def test_a_file_that_is_not_valid_yaml_or_has_no_frontmatter_is_not_touched():
    for bad in ("---\nmodel: [x\n---\nbody\n", "just text\n", "---\nmodel: x\n", "---\n- a\n- b\n---\n"):
        with pytest.raises(S.SettingsError):
            S.edit_text(bad, {("model",): "y"})


def test_a_file_that_is_not_utf8_is_refused(tmp_path):
    p = tmp_path / "x.md"
    p.write_bytes(b"---\nmodel: caf\xe9\n---\n")
    h = Home(init_tree(tmp_path / "lm").root)
    with pytest.raises(S.SettingsError, match="UTF-8"):
        S.save(h, p, {("model",): "y"})


# ------------------------------------------------------------------ saving
def test_a_save_that_changes_nothing_does_not_touch_the_file(tmp_path):
    p = tmp_path / "agentMaster.md"
    p.write_bytes(AGENT.encode("utf-8"))
    os.utime(p, (1_000_000, 1_000_000))
    h = Home(init_tree(tmp_path / "lm").root)
    assert S.save(h, p, {("effort",): "medium", ("model",): ""}) == AGENT.encode("utf-8")
    assert p.stat().st_mtime == 1_000_000


def test_a_file_changed_since_it_was_loaded_is_a_conflict_and_stays_as_the_other_one_wrote_it(tmp_path):
    p = tmp_path / "agentMaster.md"
    p.write_bytes(AGENT.encode("utf-8"))
    h = Home(init_tree(tmp_path / "lm").root)
    loaded = p.read_bytes()
    theirs = AGENT.replace("effort: medium", "effort: high  ").encode("utf-8")
    p.write_bytes(theirs)                                                                  # an editor saved meanwhile
    with pytest.raises(S.Conflict, match="changed on disk"):
        S.save(h, p, {("model",): "opus"}, loaded=loaded)
    assert p.read_bytes() == theirs
    new = S.save(h, p, {("model",): "opus"}, loaded=theirs)                                # loaded again: now it goes through
    assert S.front_meta(new.decode("utf-8"))["model"] == "opus" and b"effort: high" in new


def test_a_save_is_atomic_and_leaves_no_temporary_file(tmp_path):
    p = tmp_path / "agentMaster.md"
    p.write_bytes(AGENT.encode("utf-8"))
    h = Home(init_tree(tmp_path / "lm").root)
    S.save(h, p, {("model",): "opus"})
    assert sorted(x.name for x in tmp_path.iterdir() if x.is_file()) == ["agentMaster.md"]


def test_the_cache_forgets_a_file_the_core_wrote_even_when_size_and_time_are_the_same(tmp_path):
    h = Home(init_tree(tmp_path / "lm").root)
    p = h.config_file
    assert h.config.values["defaults"]["model"] == "sonnet" and h.defaults["model"] == "sonnet"      # cached now
    stat = p.stat()
    S.save(h, p, {("defaults", "model"): "strong"})                                        # same size: sonnet -> strong
    os.utime(p, ns=(stat.st_atime_ns, stat.st_mtime_ns))                                   # and the same clock tick
    assert p.stat().st_size == stat.st_size and p.stat().st_mtime_ns == stat.st_mtime_ns
    assert h.config.values["defaults"]["model"] == "strong" and h.defaults["model"] == "strong"


# ------------------------------------------------------------------ taking keys out, backups
def test_drop_keys_removes_a_section_that_is_left_empty_with_its_comment_and_leaves_a_note():
    text = ("---\n# top comment\n\n# What the defaults are.\ndefaults:\n  model: x   # a\n  mode: y\n\n"
            "run:\n  a: 1\n  b: 2   # keep\n---\nbody\n")
    new = S.drop_keys(text, [("defaults", "model"), ("defaults", "mode"), ("run", "a")], "moved away")
    assert new == "---\n# top comment\n\n# moved away\n\nrun:\n  b: 2   # keep\n---\nbody\n"


def test_drop_keys_keeps_crlf_and_ignores_keys_that_are_not_there():
    text = "---\r\ndefaults:\r\n  model: x\r\n  server: s\r\nrun:\r\n  a: 1\r\n---\r\nbody\r\n"
    new = S.drop_keys(text, [("defaults", "model"), ("run", "zzz"), ("nope", "a")], "n")
    assert new == "---\r\ndefaults:\r\n  # n\r\n  server: s\r\nrun:\r\n  a: 1\r\n---\r\nbody\r\n"


def test_a_backup_never_overwrites_an_older_one(tmp_path):
    p = tmp_path / "lightmind.md"
    p.write_text("one", encoding="utf-8")
    a = S.backup(p)
    p.write_text("two", encoding="utf-8")
    b = S.backup(p)
    assert a.name == f"lightmind.md.bak-{date.today().isoformat()}" and b.name == a.name + "-2"
    assert a.read_text(encoding="utf-8") == "one" and b.read_text(encoding="utf-8") == "two"
    assert S.backup(tmp_path / "missing.md") is None
