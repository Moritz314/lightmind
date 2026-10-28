"""The settings page as a module (settings.py): which fields exist, which values each may take, reading a file into a page,
saving only the changed fields line by line.  Both screens use this module, so these tests are the contract."""
import re

import pytest

from lightmind import settings as S
from lightmind.files import CONFIG_KEYS, CONFIG_ALIASES, CONFIG_FREE_TEXT, Home


def text_of(p) -> str:
    return p.read_bytes().decode("utf-8")


def put(p, text: str) -> None:
    import os
    old = p.stat().st_mtime if p.exists() else 0
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode("utf-8"))
    os.utime(p, (old + 10, old + 10))


def tree_bytes(root):
    return {str(p): p.read_bytes() for p in root.rglob("*") if p.is_file() and "runs" not in p.parts and "state" not in p.parts}


def make_agent(home, name="webdev", front="description: builds sites\nmode: auto\n", crlf=False, bom=False):
    text = f"---\n{front}---\nYou build sites.\n"
    if crlf:
        text = text.replace("\n", "\r\n")
    p = S.agent_file(home, name)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(((S.BOM if bom else "") + text).encode("utf-8"))
    return p


# ------------------------------------------------------------------ the fields
def test_the_general_fields_are_exactly_the_fixed_key_list_of_config_md():
    assert {f.path for f in S.GENERAL} == {(sec, k) for sec, keys in CONFIG_KEYS.items() for k in keys} - CONFIG_ALIASES - CONFIG_FREE_TEXT
    assert len({f.path for f in S.GENERAL}) == len(S.GENERAL) and all(f.help and f.label for f in S.GENERAL + S.AGENT)
    assert [f.path for f in S.AGENT] == [("provider",), ("model",), ("effort",), ("mode",), ("project",)]
    assert all(f.unset for f in S.AGENT) and {f.path[0] for f in S.GENERAL if f.unset} == {"vscode", "tools", "spawn", "quick", "sessions", "remote", "workflow"}
    assert {f.path for f in S.GENERAL + S.AGENT if f.danger} == {("defaults", "mode"), ("mode",), ("vscode", "mode"), ("quick", "mode"), ("remote", "mode")}


def test_options_come_from_the_tree(home):
    by = {f.path: f for f in S.GENERAL}
    def vals(path, values=None):
        return [o[0] for o in S.options(home, by[path], values)]
    assert vals(("defaults", "mode")) == ["auto", "plan", "bypass"]
    assert vals(("defaults", "effort")) == list(S.EFFORTS) and vals(("sessions", "open")) == ["tab", "window", "replace"]
    assert vals(("ui", "boot")) == [True, "brief", False] and vals(("ui", "boot_wait")) == [True, False]
    assert vals(("run", "max_depth")) == list(range(0, 11)) and vals(("run", "max_parallel"))[:4] == [1, 2, 3, 4]      # 0: only the top level
    assert "200k" in vals(("defaults", "context")) and set(vals(("defaults", "provider"))) == set(home.load_providers().accounts)
    assert [o[1] for o in S.options(home, by[("defaults", "mode")])] == ["Auto", "Plan", "Bypass"]            # labels of permissions.md


def test_the_models_follow_the_account(home):
    f = next(x for x in S.GENERAL if x.path == ("defaults", "model"))
    accounts = home.load_providers().accounts
    for name, acct in accounts.items():
        got = [o[0] for o in S.options(home, f, {("defaults", "provider"): name})]
        assert set(acct.models) <= set(got) and set(acct.tiers) <= set(got), name
    assert S.options(home, f, {("defaults", "provider"): "no-such-account"}) == []


def test_context_sizes_follow_ui_context_sizes(home):
    f = next(x for x in S.GENERAL if x.path == ("defaults", "context"))
    home.settings = {"ui": {"context_sizes": "50k, 2M"}}
    assert [o[0] for o in S.options(home, f)] == ["50k", "2m"]
    home.settings = {"ui": "oops"}
    assert [o[0] for o in S.options(home, f)] == list(S.CONTEXTS)


def test_projects_are_the_folders_of_projects_without_examples(home):
    f = next(x for x in S.AGENT if x.path == ("project",))
    (home.path("projects") / "site").mkdir(parents=True, exist_ok=True)
    got = S.options(home, f)
    assert got[0] == (None, "none", "") and [o[0] for o in got[1:]] == ["site"]          # `_example` is not a project


def test_what_the_file_holds_stays_choosable(home):
    f = next(x for x in S.GENERAL if x.path == ("run", "max_parallel"))
    assert 5 in [o[0] for o in S.options(home, f, {f.path: 5})] and 7 not in [o[0] for o in S.options(home, f)]
    m = next(x for x in S.GENERAL if x.path == ("defaults", "model"))
    assert "future-model-9" in [o[0] for o in S.options(home, m, {m.path: "future-model-9"})]


# ------------------------------------------------------------------ the allowed values
@pytest.mark.parametrize("path, value, ok", [
    (("ui", "boot_wait"), True, True), (("ui", "boot_wait"), False, True), (("ui", "boot_wait"), 1, False), (("ui", "boot_wait"), "true", False),
    (("ui", "boot"), "brief", True), (("ui", "boot"), True, True), (("ui", "boot"), "never", False), (("ui", "boot"), 0, False),
    (("sessions", "open"), "tab", True), (("sessions", "open"), "tabb", False), (("sessions", "open"), "", False), (("sessions", "open"), None, False),
    (("run", "max_parallel"), 3, True), (("run", "max_parallel"), 0, False), (("run", "max_parallel"), 33, False), (("run", "max_parallel"), True, False),
    (("run", "max_parallel"), "3", False), (("run", "max_parallel"), 2.0, False), (("run", "max_depth"), 10, True), (("run", "max_depth"), 0, True), (("run", "max_depth"), -1, False), (("run", "max_depth"), 11, False),
    (("defaults", "mode"), "bypass", True), (("defaults", "mode"), "yolo", False), (("defaults", "effort"), "xhigh", True),
    (("defaults", "effort"), "extreme", False), (("defaults", "context"), "1m", True), (("defaults", "context"), "9z", False),
    (("defaults", "model"), "sonnet", True), (("defaults", "model"), "some-new-model-1", True), (("defaults", "model"), "bad;model", False),
    (("defaults", "model"), "a b", False), (("defaults", "model"), "../x", False), (("defaults", "provider"), "no-such-account", False),
])
def test_check_value(home, path, value, ok):
    f = next(x for x in S.GENERAL if x.path == path)
    assert (S.check_value(home, f, value) == "") is ok, (path, value)


def test_an_agent_field_may_be_unset_a_general_one_may_not(home):
    assert S.check_value(home, S.AGENT[0], None) == "" and S.check_value(home, S.AGENT[4], "") == ""
    assert "a value is needed" in S.check_value(home, S.GENERAL[0], None)
    assert "not one of the values" in S.check_value(home, S.AGENT[4], "no-such-project")


# ------------------------------------------------------------------ reading
def test_read_general_shows_the_file_and_what_applies(home):
    page = S.read_general(home)
    assert not page.error and page.state == "ok" and page.path == home.config_file and page.loaded == home.config_file.read_bytes()
    by = {r.field.path: r for r in page.rows}
    assert by[("sessions", "open")].value == "replace" and by[("sessions", "open")].effective == "replace"       # the shared test tree pins it
    assert by[("ui", "boot_wait")].value is True and by[("defaults", "mode")].value == "auto"
    put(home.root / "lightmind.md", text_of(home.root / "lightmind.md").replace("on_start:", "defaults:\n  model: haiku\non_start:", 1)) if False else None


def test_an_empty_broken_or_unreadable_config_md_is_an_error_not_a_crash(home):
    cfg = home.config_file
    good = text_of(cfg)
    put(cfg, "")
    page = S.read_general(home)
    assert page.rows == [] and "is empty" in page.error and "lm update" in page.error
    put(cfg, good.replace("  model: sonnet", "  model: [sonnet", 1))
    page = S.read_general(home)
    assert page.rows == [] and page.error.startswith("config.md: line") and page.state == "error"
    with pytest.raises(S.SettingsError, match="config.md"):
        S.save_page(home, page, {("defaults", "mode"): "plan"})
    cfg.unlink()
    assert "config.md" in S.read_general(home).error and S.read_general(home).state == "missing"


def test_read_agent_shows_the_file_and_the_default_that_applies(home):
    make_agent(home, "webdev", "description: x\nmode: plan\n")
    page = S.read_agent(home, "webdev")
    by = {r.field.path: r for r in page.rows}
    assert page.scope == "agent" and page.name == "webdev" and not page.error
    assert by[("mode",)].value == "plan" and by[("mode",)].effective == "plan"
    assert by[("effort",)].value is None and by[("effort",)].effective == home.defaults["effort"]      # the tree's default applies
    assert by[("project",)].value is None and by[("project",)].effective is None


def test_blank_values_count_as_not_set(home):
    make_agent(home, "blank", 'description: x\nprovider: ""\nmodel:\n')
    by = {r.field.path: r.value for r in S.read_agent(home, "blank").rows}
    assert by[("provider",)] is None and by[("model",)] is None


# ------------------------------------------------------------------ saving: only what changed, line by line
def test_saving_changes_only_the_lines_of_the_changed_fields(home):
    before = text_of(home.config_file)
    page = S.read_general(home)
    new = S.save_page(home, page, {("defaults", "model"): "opus", ("run", "max_parallel"): 5})
    after = text_of(home.config_file)
    old_lines, new_lines = before.splitlines(), after.splitlines()
    changed = [(a, b) for a, b in zip(old_lines, new_lines) if a != b]
    assert len(old_lines) == len(new_lines) and len(changed) == 2                       # nothing else moved
    assert any(a.strip().startswith("model:") and "opus" in b for a, b in changed)
    assert any(a.strip().startswith("max_parallel:") and "5" in b for a, b in changed)
    assert all("#" in b for a, b in changed if "#" in a)                                # the comments next to them stayed
    assert {r.field.path: r.value for r in new.rows}[("defaults", "model")] == "opus"
    assert home.settings["defaults"]["model"] == "opus" and home.settings["run"]["max_parallel"] == 5   # the core sees it at once
    assert new.loaded == home.config_file.read_bytes()


def test_a_value_that_is_already_so_changes_nothing_and_writes_nothing(home):
    page = S.read_general(home)
    before, mtime = home.config_file.read_bytes(), home.config_file.stat().st_mtime_ns
    S.save_page(home, page, {("defaults", "mode"): "auto", ("ui", "boot_wait"): True})
    assert home.config_file.read_bytes() == before and home.config_file.stat().st_mtime_ns == mtime
    S.save_page(home, page, {})
    assert home.config_file.stat().st_mtime_ns == mtime


def test_crlf_and_bom_stay(home):
    cfg = home.config_file
    put(cfg, S.BOM + text_of(cfg).replace("\r\n", "\n").replace("\n", "\r\n"))
    page = S.read_general(home)
    assert not page.error
    S.save_page(home, page, {("sessions", "open"): "window"})
    raw = cfg.read_bytes()
    assert raw.startswith(S.BOM.encode("utf-8")) and raw.count(b"\r\n") == raw.count(b"\n") and b"open: window" in raw


def test_nothing_is_written_when_a_value_is_not_allowed(home):
    page = S.read_general(home)
    before = home.config_file.read_bytes()
    with pytest.raises(S.SettingsError, match="Mode: 'yolo' is not one of the values offered"):
        S.save_page(home, page, {("defaults", "model"): "opus", ("defaults", "mode"): "yolo"})     # the good one is not written either
    with pytest.raises(S.SettingsError, match="no such setting"):
        S.save_page(home, page, {("defaults", "server"): "elsewhere"})                              # defaults.server is local.md's
    with pytest.raises(S.SettingsError, match="a value is needed"):
        S.save_page(home, page, {("defaults", "model"): None})
    assert home.config_file.read_bytes() == before


def test_nothing_is_written_when_the_counter_check_fails(home, monkeypatch):
    page = S.read_general(home)
    before = home.config_file.read_bytes()
    def refuse(*a, **k):
        raise S.SettingsError("counter-check failed")
    monkeypatch.setattr(S, "prove_text", refuse)
    with pytest.raises(S.SettingsError, match="counter-check"):
        S.save_page(home, page, {("defaults", "model"): "opus"})
    assert home.config_file.read_bytes() == before


def test_a_file_that_changed_meanwhile_is_a_conflict(home):
    page = S.read_general(home)
    put(home.config_file, text_of(home.config_file).replace("  effort: medium", "  effort: high", 1))     # someone edited it
    edited = home.config_file.read_bytes()
    with pytest.raises(S.Conflict):
        S.save_page(home, page, {("defaults", "model"): "opus"})
    assert home.config_file.read_bytes() == edited
    fresh = S.read_general(home)                                                           # reading again is the way on
    S.save_page(home, fresh, {("defaults", "model"): "opus"})
    assert b"effort: high" in home.config_file.read_bytes() and b"model: opus" in home.config_file.read_bytes()


def test_only_the_file_of_the_page_is_touched(home):
    before = tree_bytes(home.root)
    S.save_page(home, S.read_general(home), {("defaults", "effort"): "high"})
    after = tree_bytes(home.root)
    assert [k for k in after if after[k] != before.get(k)] == [str(home.config_file)]


# ------------------------------------------------------------------ an agent file
def test_a_missing_project_key_is_inserted_at_a_fixed_place(home):
    p = make_agent(home, "webdev", "description: builds sites\nmode: auto\nmay_spawn: []\n")
    (home.path("projects") / "site").mkdir(parents=True, exist_ok=True)
    page = S.read_agent(home, "webdev")
    new = S.save_page(home, page, {("project",): "site"})
    lines = text_of(p).splitlines()
    assert lines[:5] == ["---", "description: builds sites", "mode: auto", "project: site", "may_spawn: []"]   # behind `mode`
    assert text_of(p).endswith("---\nYou build sites.\n") and {r.field.path: r.value for r in new.rows}[("project",)] == "site"


def test_an_agent_field_set_to_not_set_keeps_its_key_empty_and_its_comment(home):
    p = make_agent(home, "webdev", 'description: x\nprovider: abo   # which account\nmode: auto\nproject: site\n')
    (home.path("projects") / "site").mkdir(parents=True, exist_ok=True)
    S.save_page(home, S.read_agent(home, "webdev"), {("provider",): None, ("project",): None})
    out = text_of(p)
    assert re.search(r"provider: (''|\"\")\s+# which account", out)                    # empty, and the comment stays
    assert re.search(r"project: (''|\"\")\n", out) and "mode: auto" in out
    assert {r.field.path: r.value for r in S.read_agent(home, "webdev").rows}[("provider",)] is None


def test_not_setting_a_key_the_file_does_not_have_writes_nothing(home):
    p = make_agent(home, "webdev")
    before, mtime = p.read_bytes(), p.stat().st_mtime_ns
    S.save_page(home, S.read_agent(home, "webdev"), {("project",): None, ("effort",): None})
    assert p.read_bytes() == before and p.stat().st_mtime_ns == mtime


def test_an_agent_file_keeps_crlf_and_bom(home):
    p = make_agent(home, "webdev", crlf=True, bom=True)
    (home.path("projects") / "site").mkdir(parents=True, exist_ok=True)
    S.save_page(home, S.read_agent(home, "webdev"), {("project",): "site", ("effort",): "high"})
    raw = p.read_bytes()
    assert raw.startswith(S.BOM.encode("utf-8")) and raw.count(b"\r\n") == raw.count(b"\n")
    assert b"project: site" in raw and b"effort: high" in raw and raw.endswith(b"You build sites.\r\n")


def test_an_agent_project_must_exist(home):
    make_agent(home, "webdev")
    with pytest.raises(S.SettingsError, match="Project: 'ghost' is not one of the values offered"):
        S.save_page(home, S.read_agent(home, "webdev"), {("project",): "ghost"})
    with pytest.raises(S.SettingsError, match="Project"):
        S.save_page(home, S.read_agent(home, "webdev"), {("project",): "../agents"})


def test_the_model_of_an_agent_follows_its_account(home):
    make_agent(home, "webdev", "description: x\nprovider: claude-abo\n")
    page = S.read_agent(home, "webdev")
    f = next(r.field for r in page.rows if r.field.path == ("model",))
    accounts = home.load_providers().accounts
    names = {o[0] for o in S.options(home, f, page.values())}
    assert set(accounts["claude-abo"].models) <= names


def test_the_agent_list_is_sorted_and_without_examples(home):
    make_agent(home, "zeta")
    make_agent(home, "alpha")
    assert S.agent_names(home) == sorted(S.agent_names(home)) and "alpha" in S.agent_names(home) and "_example" not in S.agent_names(home)


# ------------------------------------------------------------------ the remote group (Remote Control, the phone app)
REMOTE_KEYS = ("enabled", "auto", "gateway", "default_node", "agent", "model", "effort", "mode", "push", "push_questions", "upload_mb")


def test_the_remote_group_is_in_the_key_list_and_has_a_field_for_every_key(home):
    assert CONFIG_KEYS["remote"] == REMOTE_KEYS
    by = {f.path: f for f in S.GENERAL}
    assert all(("remote", k) in by and by[("remote", k)].unset for k in REMOTE_KEYS)       # every key may stay empty: the default applies
    vals = lambda k: [o[0] for o in S.options(home, by[("remote", k)])]
    assert vals("enabled") == [None, True, False] == vals("auto") and vals("push_questions") == [None, True, False]
    assert vals("push") == [None, "webpush", "ntfy", "both", "off"]
    assert vals("effort")[1:] == list(S.EFFORTS) and vals("mode")[1:] == ["auto", "plan", "bypass"]
    assert set(vals("gateway")[1:]) == set(home.list_servers()) == set(vals("default_node")[1:]) and "laptop" in vals("gateway")
    assert 200 in vals("upload_mb") and by[("remote", "mode")].danger


@pytest.mark.parametrize("key,value,ok", [("enabled", True, True), ("enabled", "yes", False), ("push", "ntfy", True), ("push", "sms", False),
                                          ("push_questions", False, True), ("upload_mb", 200, True), ("upload_mb", 0, False),
                                          ("upload_mb", 5000, False), ("upload_mb", "200", False), ("gateway", "laptop", True),
                                          ("gateway", "no-such-server", False), ("default_node", "", True), ("mode", "bypass", True),
                                          ("mode", "no-such-mode", False)])
def test_check_value_for_remote(home, key, value, ok):
    f = next(f for f in S.GENERAL if f.path == ("remote", key))
    assert (S.check_value(home, f, value) == "") is ok


def test_the_remote_group_ships_commented_out_and_a_save_writes_it(home):
    text = text_of(home.config_file)
    assert "# remote:" in text and "\nremote:" not in text                   # off until the person removes the #
    assert "remote" not in home.config.values and home.config.state == "ok" and home.config.ignored == []
    S.save_page(home, S.read_general(home), {("remote", "enabled"): True, ("remote", "push"): "both", ("remote", "upload_mb"): 50})
    assert home.settings["remote"] == {"enabled": True, "push": "both", "upload_mb": 50}      # the core reads it at once
    assert home.config.state == "ok" and home.config.ignored == [] and home.config.values["remote"]["enabled"] is True
    page = S.read_general(home)
    assert {r.field.path: r.value for r in page.rows}[("remote", "push")] == "both"
    S.save_page(home, page, {("remote", "push"): None})                                       # back to the default: the key stays, empty
    assert not home.settings["remote"]["push"]                                                # empty counts as not set, like every key of config.md
