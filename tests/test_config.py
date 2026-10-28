"""config.md: the layer between lightmind.md and local.md, its fixed list of keys, the move of the values out of
lightmind.md into an empty one (older trees), the guard in permissions.md, and what `lm check` says about it."""
import json
import os
import re
from datetime import date

import pytest

from lightmind import checks, cli, rules as R, settings as S
from lightmind.files import CONFIG_KEYS, ENTRY, LOCAL, Home
from lightmind.init import TRACK, init_tree, update_tree
from lightmind.model import RuleContext
from lightmind.providers.base import settings_defaults

from conftest import legacy_modes

# what an older lightmind.md held (values that differ from the template, so a copy of the template would show)
OLD = {("defaults", "provider"): "claude-abo", ("defaults", "model"): "opus", ("defaults", "effort"): "high",
       ("defaults", "context"): "500k", ("defaults", "mode"): "plan", ("ui", "boot"): "brief",
       ("run", "max_parallel"): 2, ("run", "max_depth"): 1}


def put(p, text: str) -> None:
    """Write a file the way a person's editor would: bytes as given, and a time the mtime cache must notice."""
    old = p.stat().st_mtime if p.exists() else 0
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))
    os.utime(p, (old + 10, old + 10))


def text_of(p) -> str:
    return p.read_bytes().decode("utf-8")


GUARD_LINE = re.compile(r"(?m)^  - write\(\{config\}\).*\n")


def unguard(root) -> str:
    """Take the guard line out of permissions.md, the way the user (or a careless edit) could; returns the old text."""
    perm = root / "permissions.md"
    old = text_of(perm)
    put(perm, GUARD_LINE.sub("", old))
    assert not GUARD_LINE.search(text_of(perm))     # the bypass block keeps its own locked deny of config.md
    return old


@pytest.fixture
def fresh(tmp_path):
    """A new tree in the layout of the version before mode files: the guard of config.md is the `write({config})` line in
    the grant_files of permissions.md, the modes sit in its `modes:` block (see test_modes.py for the mode files)."""
    init_tree(tmp_path / "lm")
    legacy_modes(tmp_path / "lm")
    return tmp_path / "lm"


@pytest.fixture
def old(tmp_path):
    """A tree as it was before config.md: its keys sit in lightmind.md, config.md is the empty file the user made,
    permissions.md has no guard line, the record does not know config.md."""
    root = tmp_path / "lm"
    init_tree(root)
    legacy_modes(root)
    entry = root / ENTRY
    text = S.drop_keys(text_of(entry), [("paths", "config")])
    put(entry, S.edit_text(text, OLD))
    (root / "config.md").write_bytes(b"")
    perm = root / "permissions.md"
    unguard(root)
    track = root / "system" / "state" / TRACK
    rec = json.loads(track.read_text(encoding="utf-8"))
    rec["shipped"].remove("config.md")
    track.write_text(json.dumps(rec), encoding="utf-8")
    return root


def effective(h: Home) -> dict:
    return {k: S.dig(h.settings, k) for k in [*OLD, ("defaults", "server")]}


def lines(h: Home, status=None) -> list[str]:
    return [f"{x.status}: {x.detail}" for x in checks.check_config(h) if x.label == "config" and status in (None, x.status)]


# ------------------------------------------------------------------ the layers
def test_a_fresh_tree_has_its_settings_in_config_md_and_none_of_them_in_lightmind_md(fresh):
    h = Home(fresh)
    assert h.config.state == "ok" and h.config.both == [] and h.config.ignored == []
    assert set(h.config.values) == set(CONFIG_KEYS) - {"vscode", "tools", "remote", "skills"} and h.config.values["sessions"] == {"open": "tab", "auto_title": True, "title_model": "haiku", "title_every": 1}   # vscode.*: shipped empty, tools.memory_mb and skills.source too, remote.* commented out = not set
    entry = h.entry.meta
    for sec, keys in CONFIG_KEYS.items():
        assert not any(k in (entry.get(sec) or {}) for k in keys), sec
    assert h.defaults == {"server": "laptop", "provider": "claude-abo", "model": "sonnet", "effort": "medium",
                          "context": "200k", "mode": "auto"}
    assert h.settings["ui"]["boot"] is True and h.settings["ui"]["boot_wait"] is True and h.settings["run"]["max_depth"] == 3


def test_later_wins_lightmind_then_config_then_local(fresh):
    h = Home(fresh)
    put(fresh / ENTRY, S.edit_text(text_of(fresh / ENTRY), {("defaults", "model"): "haiku", ("defaults", "server"): "gpubox"}))
    assert h.defaults["model"] == "sonnet" and h.defaults["server"] == "gpubox"      # config.md beats lightmind.md
    assert h.config.both == ["defaults.model"]
    local = fresh / "system" / "state" / LOCAL
    put(local, "---\ndefaults:\n  model: opus\n  server: buildbox\n---\n")
    assert h.defaults["model"] == "opus" and h.defaults["server"] == "buildbox"    # local.md beats both
    local.unlink()
    put(fresh / "config.md", S.edit_text(text_of(fresh / "config.md"), {("defaults", "model"): None}))
    assert h.defaults["model"] == "haiku" and h.config.values["defaults"].get("model") is None   # not set there: lightmind.md shows


def test_only_the_fixed_keys_are_read_everything_else_is_ignored_and_warned(fresh):
    h = Home(fresh)
    put(fresh / "config.md", S.edit_text(text_of(fresh / "config.md"), {
        ("defaults", "server"): "elsewhere", ("run", "slot_timeout"): "1s", ("shell",): "zsh", ("sessions", "colour"): "red"}))
    assert h.defaults["server"] == "laptop" and h.settings["run"]["slot_timeout"] == "10m" and h.settings["shell"] == "bash"
    assert "colour" not in h.settings["sessions"]
    assert sorted(h.config.ignored) == ["defaults.server", "run.slot_timeout", "sessions.colour", "shell"]
    (warn,) = lines(h, "warn")
    assert "defaults.server" in warn and "shell" in warn


def test_an_empty_value_is_not_set_and_falls_through(fresh):
    put(fresh / "config.md", text_of(fresh / "config.md").replace("  model: sonnet ", "  model:        ", 1))
    h = Home(fresh)
    assert "model" not in h.config.values["defaults"]
    assert "model" not in h.defaults and None not in h.defaults.values()            # not the text "None", not a value at all


def test_a_missing_or_empty_config_md_leaves_everything_as_it_was(old):
    h = Home(old)
    assert h.config.state == "empty" and h.config.values == {}
    assert effective(h) == {**OLD, ("defaults", "server"): "laptop"}
    (old / "config.md").unlink()
    h = Home(old)
    assert h.config.state == "missing" and effective(h) == {**OLD, ("defaults", "server"): "laptop"}
    assert lines(h, "info") == ["info: config.md is missing"]
    assert any("config.md is not guarded" in x for x in lines(h, "fail"))              # an older tree: `lm update` first


def test_a_broken_config_md_is_a_failure_and_lightmind_md_keeps_applying(fresh):
    put(fresh / ENTRY, S.edit_text(text_of(fresh / ENTRY), {("defaults", "model"): "haiku"}))
    put(fresh / "config.md", text_of(fresh / "config.md").replace("  model: sonnet ", "  model: [sonnet ", 1))
    h = Home(fresh)
    assert h.config.state == "error" and "line" in h.config.error and h.config.values == {}
    assert h.defaults["model"] == "haiku"                                            # the settings of lightmind.md apply
    (fail,) = lines(h, "fail")
    assert "config.md: line" in fail and "lightmind.md apply" in fail
    assert [x.status for x in dict(checks.groups(h))["config"]() if x.status == "fail"] == ["fail"]    # the boot checklist runs this group
    put(fresh / "config.md", "no frontmatter at all\n")
    assert Home(fresh).config.state == "error" and "frontmatter" in Home(fresh).config.error


def test_a_key_in_both_files_is_an_info_line(fresh):
    put(fresh / ENTRY, S.edit_text(text_of(fresh / ENTRY), {("run", "max_depth"): 5}))
    h = Home(fresh)
    assert h.settings["run"]["max_depth"] == 3 and "info: set in lightmind.md and config.md, the latter wins: run.max_depth" in lines(h)


# ------------------------------------------------------------------ older tree: guard and move
def test_update_moves_the_values_out_of_lightmind_md_and_loses_nothing(old):
    before = effective(Home(old))
    entry_before = text_of(old / ENTRY)
    created = update_tree(old)
    h = Home(old)
    assert created == []                                                             # it existed (empty), so it was not created
    assert effective(h) == before                                                    # the same settings, from other files
    assert h.config.state == "ok" and h.config.values["defaults"]["model"] == "opus" and h.config.values["run"] == {"max_parallel": 2, "max_depth": 1}
    assert h.config.values["sessions"] == {"open": "tab", "auto_title": True, "title_model": "haiku", "title_every": 1} and h.config.values["ui"] == {"boot": "brief", "boot_wait": True, "notes_layout": "split"}
    assert h.config.both == []                                                       # moved, not copied
    entry = h.entry.meta
    for sec, keys in CONFIG_KEYS.items():
        assert not any(k in (entry.get(sec) or {}) for k in keys)
    assert "defaults" not in entry and entry["run"].keys() >= {"slot_timeout", "ask_channel"} and "boot" not in entry["ui"]
    back = old / f"lightmind.md.bak-{date.today().isoformat()}"
    assert text_of(back) == entry_before                                             # the old file is kept as it was
    assert S.front_meta(text_of(old / "config.md"))["defaults"]["effort"] == "high"
    assert "moved to config.md" in text_of(old / ENTRY)                              # a pointer where the settings were
    assert any("config.md: filled" in n for n in created.notes)
    again = update_tree(old)
    assert again == [] and again.notes == []


def test_the_default_server_goes_to_local_md_not_to_config_md(old):
    update_tree(old)
    local = old / "system" / "state" / LOCAL
    assert S.front_meta(text_of(local)) == {"defaults": {"server": "laptop"}}
    assert "server" not in Home(old).config.values["defaults"]
    assert Home(old).defaults["server"] == "laptop"


def test_values_come_from_lightmind_md_not_from_the_local_overlay(old):
    local = old / "system" / "state" / LOCAL
    put(local, "---\ndefaults:\n  model: haiku\n  server: buildbox\n---\n")
    update_tree(old)
    assert S.front_meta(text_of(old / "config.md"))["defaults"]["model"] == "opus"   # not the machine's own choice
    meta = S.front_meta(text_of(local))
    assert meta["defaults"] == {"model": "haiku", "server": "buildbox"}            # an own server of this machine stays
    assert Home(old).defaults["model"] == "haiku" and Home(old).defaults["server"] == "buildbox"
    assert (old / f"system/state/local.md.bak-{date.today().isoformat()}").exists() is False   # nothing in it changed


def test_a_nonempty_config_md_is_never_filled_or_touched(old):
    put(old / "config.md", "---\nsessions:\n  open: window\n---\nmine\n")
    update_tree(old)
    assert text_of(old / "config.md") == "---\nsessions:\n  open: window\n---\nmine\n"
    assert Home(old).defaults["model"] == "opus"                                      # lightmind.md still holds the rest
    assert "defaults" in Home(old).entry.meta


def test_nothing_is_written_when_the_move_cannot_be_proved(old, monkeypatch):
    entry_before = (old / ENTRY).read_bytes()
    def refuse(*a, **k):
        raise S.SettingsError("counter-check failed")
    monkeypatch.setattr(S, "edit_text", refuse)
    created = update_tree(old)
    monkeypatch.undo()
    assert any("config.md: not filled, nothing was changed" in n for n in created.notes)
    assert (old / ENTRY).read_bytes() == entry_before and (old / "config.md").read_bytes() == b""
    assert not list(old.glob("lightmind.md.bak-*")) and not (old / "system" / "state" / LOCAL).exists()
    assert Home(old).config.state == "empty" and Home(old).defaults["model"] == "opus"


def test_local_md_that_cannot_take_the_server_keeps_it_in_lightmind_md(old):
    put(old / "system" / "state" / LOCAL, "just a note, no frontmatter\n")
    created = update_tree(old)
    assert Home(old).entry.meta["defaults"] == {"server": "laptop"} and Home(old).config.state == "ok"
    assert any("defaults.server stays in lightmind.md" in n for n in created.notes)
    assert Home(old).defaults["model"] == "opus"


def test_update_guards_config_md_and_keeps_a_backup(old):
    perm = old / "permissions.md"
    before = text_of(perm)
    update_tree(old)
    after = text_of(perm)
    assert GUARD_LINE.sub("", after) == before                 # (the bypass block carries its own locked deny, not matched)
    assert len(GUARD_LINE.findall(after)) == 1
    assert text_of(old / f"permissions.md.bak-{date.today().isoformat()}") == before
    meta = S.front_meta(after)
    assert meta["grant_files"][2] == "write({config})" and meta["modes"]["auto"]["locked"]["ask"] == meta["grant_files"]
    rec = json.loads((old / "system" / "state" / TRACK).read_text(encoding="utf-8"))
    assert "config.md" in rec["shipped"] and "done" not in rec
    assert update_tree(old).notes == [] and text_of(perm) == after                   # guarded: nothing more to add


def test_update_puts_a_missing_guard_back_and_says_what_starts_to_apply(fresh):
    perm = fresh / "permissions.md"
    guarded = unguard(fresh)                                                         # the line is gone again
    put(fresh / "config.md", S.edit_text(text_of(fresh / "config.md"), {("defaults", "mode"): "bypass"}))
    assert Home(fresh).config.state == "unguarded"
    created = update_tree(fresh)
    assert text_of(perm) == re.sub(r"(?m)^(  - write\(\{config\}\)) +#.*$", r"\1", guarded)   # the line, bare, in its place
    assert Home(fresh).config.state == "ok" and Home(fresh).defaults["mode"] == "bypass"
    assert any("guarded now" in n for n in created.notes)
    assert any("apply from now on" in n and "defaults.mode=bypass" in n for n in created.notes)
    assert len(list(fresh.glob("permissions.md.bak-*"))) == 1


def test_a_tree_with_its_own_permissions_layout_is_told_not_changed(old):
    perm = old / "permissions.md"
    put(perm, text_of(perm).replace("  - write({entry})\n", "").replace("  - write({permissions})\n", ""))
    before = text_of(perm)
    created = update_tree(old)
    assert text_of(perm) == before and any("add `write({config})` to grant_files by hand" in n for n in created.notes)
    assert Home(old).config.state == "empty"                                         # no page that an agent could write freely
    assert any("config.md is not guarded" in w and "lm update" in w for w in lines(Home(old), "fail"))


# ------------------------------------------------------------------ fail closed: no guard, no config.md
def test_without_the_guard_config_md_does_not_count_and_the_checks_say_fail(fresh):
    put(fresh / ENTRY, S.edit_text(text_of(fresh / ENTRY), {("defaults", "mode"): "plan"}))
    put(fresh / "config.md", S.edit_text(text_of(fresh / "config.md"), {("defaults", "mode"): "bypass", ("run", "max_depth"): 7}))
    h = Home(fresh)
    assert h.config.state == "ok" and h.defaults["mode"] == "bypass" and h.settings["run"]["max_depth"] == 7
    assert h.unguarded_reason() == "" and lines(h, "fail") == []
    unguard(fresh)
    assert h.config.state == "unguarded" and h.config.values == {} and "config.md" in h.config.error
    assert h.defaults["mode"] == "plan"                                              # the mode of lightmind.md, never bypass
    assert h.settings["defaults"]["mode"] == "plan" and "max_depth" not in h.settings["run"]     # nothing of config.md shows
    assert settings_defaults(h)["mode"] == "plan"                                    # what a new chat takes
    (fail,) = lines(h, "fail")
    assert "config.md is not guarded" in fail and "not used" in fail and "lm update" in fail and "write({config})" in fail
    group = [x for x in dict(checks.groups(h))["config"]() if x.status == "fail"]    # the boot checklist runs this group
    assert len(group) == 1 and "lm update" in group[0].detail
    update_tree(fresh)                                                               # `lm update` is the way back
    assert h.config.state == "ok" and h.defaults["mode"] == "bypass" and lines(h, "fail") == []


def test_a_guard_line_that_does_not_ask_in_every_mode_is_no_guard(fresh):
    perm = fresh / "permissions.md"
    put(perm, text_of(perm).replace("audit:", "  yolo:\n    label: Yolo\n    allow: [write(**)]\naudit:", 1))
    put(fresh / "config.md", S.edit_text(text_of(fresh / "config.md"), {("defaults", "mode"): "bypass"}))
    h = Home(fresh)
    assert "write({config})" in text_of(perm)                                        # the line is there, the mode lets it pass
    assert "yolo" in h.unguarded_reason() and "without asking" in h.unguarded_reason()
    assert h.config.state == "unguarded" and h.defaults.get("mode") != "bypass"
    assert any("not guarded" in x and "yolo" in x for x in lines(h, "fail"))
    notes = update_tree(fresh).notes                                                 # nothing `lm update` could add to the list
    assert any("yolo" in n for n in notes) and Home(fresh).config.state == "unguarded"


def test_what_cannot_be_judged_is_not_guarded(fresh):
    put(fresh / "permissions.md", "---\nmodes: oops\n---\n")
    put(fresh / "config.md", S.edit_text(text_of(fresh / "config.md"), {("defaults", "mode"): "bypass"}))
    h = Home(fresh)
    assert h.unguarded_reason() and h.config.state == "unguarded" and h.defaults.get("mode") != "bypass"
    (fresh / "permissions.md").unlink()
    h = Home(fresh)
    assert h.unguarded_reason() and h.config.state == "unguarded"                    # no permissions at all: nothing asks


def test_the_guard_verdict_follows_an_edit_of_permissions_md_within_a_session(fresh):
    h = Home(fresh)
    assert h.config.state == "ok"
    unguard(fresh)
    assert h.config.state == "unguarded"                                             # the same Home object sees it at once
    perm = fresh / "permissions.md"
    put(perm, text_of(perm).replace("  - write({permissions})\n", "  - write({permissions})\n  - write({config})\n", 1))
    assert h.config.state == "ok"


def ctx(h):
    return RuleContext(home=str(h.root), run_dir=str(h.root / "runs" / "r1"), project_dir=str(h.root / "projects" / "p"))


def write_decision(h, mode, path):
    return R.decide_all(R.build_layers(h, None, mode, None), R.checks_for_call("write", {"path": path}), ctx(h)).effect


@pytest.mark.parametrize("mode", ("auto", "bypass"))
def test_an_agent_cannot_write_config_md_without_asking(fresh, mode):
    h = Home(fresh)
    want = "ask" if mode == "auto" else "deny"                  # bypass asks nothing: config.md is a locked deny there
    assert write_decision(h, mode, "config.md") == want
    assert write_decision(h, mode, "lightmind.md") == want                  # a file that holds grants, like config.md
    assert R.decide_all(R.build_layers(h, None, mode, None), R.checks_for_call("edit", {"path": "config.md", "old": "a", "new": "b"}), ctx(h)).effect == want
    assert write_decision(h, mode, "notes.md") == "allow"
    assert write_decision(h, "plan", "config.md") == "deny"


def test_an_older_tree_is_open_until_update_and_asks_afterwards_without_paths_config(old):
    h = Home(old)
    assert "config" not in h.paths                                                   # no `paths.config` in this lightmind.md
    assert write_decision(h, "auto", "config.md") == "allow"                         # the hole update closes
    update_tree(old)
    assert write_decision(Home(old), "auto", "config.md") == "ask"
    assert write_decision(Home(old), "bypass", "config.md") == "deny"                # bypass asks nothing: a locked deny


# ------------------------------------------------------------------ the start of lm
def test_starting_the_tui_prepares_an_older_tree_and_says_so(old, capsys, monkeypatch):
    monkeypatch.delenv("LM_DEPTH", raising=False)
    monkeypatch.delenv("LM_RUN", raising=False)
    cli._migrate(Home(old))
    err = capsys.readouterr().err
    assert "permissions.md: config.md is guarded" in err and "config.md: filled" in err
    assert Home(old).config.state == "ok" and "write({config})" in text_of(old / "permissions.md")
    cli._migrate(Home(old))
    assert capsys.readouterr().err == ""                                             # once


def test_a_run_started_by_an_agent_never_touches_the_tree(old, monkeypatch):
    before = {p: p.read_bytes() for p in old.rglob("*") if p.is_file()}
    monkeypatch.setenv("LM_RUN", "20261021-000000-x-0001")
    cli._migrate(Home(old))
    monkeypatch.delenv("LM_RUN")
    monkeypatch.setenv("LM_DEPTH", "1")
    cli._migrate(Home(old))
    assert before == {p: p.read_bytes() for p in old.rglob("*") if p.is_file()}


def test_lm_update_prints_what_it_did_to_config_md(old, capsys):
    assert cli.main(["--home", str(old), "update"]) == 0
    out = capsys.readouterr().out
    assert "config.md: filled" in out and "guarded" in out
    assert cli.main(["--home", str(old), "--json", "update"]) == 0
    got = json.loads(capsys.readouterr().out)
    assert got["created"] == [] and "notes" not in got                                # `notes` only when there are some


def test_a_new_tree_needs_no_migration(fresh):
    before = {p: p.read_bytes() for p in fresh.rglob("*") if p.is_file() and TRACK not in p.name}
    assert update_tree(fresh) == [] and update_tree(fresh).notes == []
    assert before == {p: p.read_bytes() for p in fresh.rglob("*") if p.is_file() and TRACK not in p.name}
    assert not list(fresh.glob("*.bak-*"))
