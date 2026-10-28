"""The user tree: what `init_tree` creates, what the loaders see, how `update_tree` behaves."""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from launcher_support import Launcher, calls, environ_of, same
from lightmind import init as I
from lightmind.files import ENTRY, Home, PathsError, parse_frontmatter, platform_tag
from lightmind.init import INSTALL_SOURCE, TRACK, init_tree, repo_name, shipped_paths, update_tree

FOLDERS = {"skills", "tools", "agents", "workflows", "triggers", "servers", "projects", "vault",
           "secrets", "runs", "providers", "modes"}
ROOT_FILES = {"lightmind.md", "permissions.md", "rechte.md", "config.md", ".gitignore"}
ENGINE = {"README.md", "lm", "lm.cmd", "lightmind", "lightmind.cmd", "lmq", "lmq.cmd", "lmr", "lmr.cmd", "state"}                 # what `system/` holds right after init
PATH_KEYS = {"agents", "skills", "tools", "workflows", "triggers", "servers", "projects", "providers",
             "secrets", "runs", "audit", "permissions", "config", "vault", "system", "state", "runtime", "python", "cache",
             "bin", "claude", "browsers", "modes", "rights"}
HAVE_GIT = shutil.which("git") is not None


SHIPPED_SKILLS = {"text-stats"}
SHIPPED_TOOLS = {"vault", "browser", "pc", "triggers", "text-stats"}
SHIPPED_TRIGGERS = ["example-weekly"]        # templates/triggers/example-weekly.md, switched off


@pytest.fixture
def fresh(tmp_path) -> Path:
    """A tree exactly as `lm init` makes it (no test overlay)."""
    root = tmp_path / "lm"
    init_tree(root)
    return root


def titles(text: str) -> list[str]:
    """H1 lines of a Markdown text, outside fenced code."""
    out, fenced = [], False
    for ln in text.splitlines():
        if ln.startswith("```"):
            fenced = not fenced
        elif not fenced and ln.startswith("# "):
            out.append(ln)
    return out


def write(p: Path, text: str) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def bump(p: Path) -> None:
    """Make an edit visible to the mtime cache even on a coarse file system clock."""
    t = p.stat().st_mtime + 10
    os.utime(p, (t, t))


# ------------------------------------------------------------------ layout
def test_init_creates_exactly_the_tree(fresh):
    assert {p.name for p in fresh.iterdir()} == FOLDERS | ROOT_FILES | {"system"}
    assert {p.name for p in (fresh / "system").iterdir()} == ENGINE
    assert sorted(p.name for p in (fresh / "system" / "state").iterdir()) == sorted([TRACK, INSTALL_SOURCE])
    on_disk = {p.relative_to(fresh).as_posix() for p in fresh.rglob("*") if p.is_file()}
    assert on_disk == set(shipped_paths()) | {f"system/state/{TRACK}", f"system/state/{INSTALL_SOURCE}"}


def test_the_root_holds_only_what_matters_to_user(fresh):
    """lightmind.md, permissions.md, the content folders, and ONE folder for everything technical."""
    visible = {p.name for p in fresh.iterdir() if not p.name.startswith(".")}
    assert visible == {"lightmind.md", "permissions.md", "rechte.md", "config.md", "system"} | FOLDERS
    for launcher in ("lm", "lm.cmd", ".lm"):
        assert not (fresh / launcher).exists(), launcher
    assert (fresh / "system" / "lm").is_file() and (fresh / "system" / "lm.cmd").is_file()


def test_the_system_readme_is_short_and_says_what_can_be_deleted(fresh):
    text = (fresh / "system" / "README.md").read_text(encoding="utf-8")
    assert text.startswith("# system\n") and len(text.splitlines()) < 32
    assert "You never need to touch this folder. To rebuild the engine, delete the `runtimes` folder" in text
    for part in ("state/", "runtimes/<os>-<arch>/rN/", "python/", "cache/", "bin/", "claude/<account>/", "browsers/",
                 "lm.cmd", "lightmind"):
        assert part in text, part


def test_dropped_files_and_folders_are_not_in_the_tree(fresh):
    for gone in ("context.md", "prices.md", "providers.md", "evals", "dash",
                 "dot-gitignore", "connectors"):
        assert not (fresh / gone).exists(), gone


def test_every_folder_has_a_readme(fresh):
    for name in FOLDERS:
        text = (fresh / name / "README.md").read_text(encoding="utf-8")
        assert text.startswith(f"# {name}\n") and len(text.splitlines()) > 8, name
    for extra in ("tools/_example", "tools/vault"):
        assert (fresh / extra / "README.md").is_file(), extra


def test_every_readme_has_one_title_and_no_em_dashes(fresh):
    for p in fresh.rglob("*.md"):
        text = p.read_text(encoding="utf-8")
        assert "\u2014" not in text and "\u2013" not in text, p
        assert text.endswith("\n") and not any(ln != ln.rstrip() for ln in text.splitlines()), p
        if p.name == "README.md":
            assert len(titles(text)) == 1, p


def test_every_template_frontmatter_parses(fresh):
    for p in fresh.rglob("*.md"):
        meta, body = parse_frontmatter(p.read_text(encoding="utf-8"))
        assert isinstance(meta, dict) and not body.startswith("---\n"), p
    assert parse_frontmatter((fresh / "agents/_example/agentMaster.md").read_text(encoding="utf-8"))[0]["effort"] == "medium"


def test_examples_exist_for_every_kind(fresh):
    assert (fresh / "skills/_example/SKILL.md").is_file()
    assert "# /// script" in (fresh / "tools/_example/example.py").read_text(encoding="utf-8")
    assert (fresh / "agents/_example/agentMaster.md").is_file() and (fresh / "agents/_example/tools.md").is_file()
    wf = fresh / "workflows/_example"
    assert {p.name for p in wf.iterdir()} == {"agentMaster.md", "tools.md", "workflow.py"}
    assert (fresh / "triggers/_example.md").is_file()
    assert (fresh / "servers/laptop/info.md").is_file() and (fresh / "servers/_example/info.md").is_file()
    assert {p.name for p in (fresh / "projects/_example").iterdir()} == {
        "projectMemory.md", "projectAssets.md", "tools.md"}
    assert (fresh / "secrets/_example/info.md").is_file()
    assert not list((fresh / "secrets").rglob("*.env"))
    assert {p.name for p in (fresh / "providers").iterdir()} == {
        "README.md", "claude-abo.md", "_example-claude-abo-2.md", "_example-anthropic-api.md",
        "_example-openrouter.md"}


def test_paths_of_lightmind_md_stay_inside_the_tree(fresh):
    home = Home(fresh)
    assert set(home.paths) == PATH_KEYS
    for key in PATH_KEYS:
        p = home.path(key).resolve()
        assert p == fresh.resolve() or fresh.resolve() in p.parents, key
    for name in FOLDERS:
        assert home.path(name).is_dir(), name
    assert home.rel("vault") == "vault"                      # the vault lives inside the tree by default


def test_portable_runtime_layout(fresh):
    home = Home(fresh)
    os_, arch = platform_tag()
    assert os_ in ("windows", "linux", "macos") and arch in ("x86_64", "arm64")
    assert home.rel("system") == "system" and home.rel("runtime") == f"system/runtimes/{os_}-{arch}"
    for key, folder in (("state", "state"), ("python", "python"), ("cache", "cache"), ("bin", "bin"),
                        ("claude", "claude"), ("browsers", "browsers")):
        assert home.path(key) == fresh.resolve() / "system" / folder
    env = home.runtime_env({"PATH": "/usr/bin"})
    assert env["UV_CACHE_DIR"] == str(home.path("cache"))
    assert env["UV_PYTHON_INSTALL_DIR"] == str(home.path("python"))
    assert env["PLAYWRIGHT_BROWSERS_PATH"] == str(home.path("browsers"))
    assert env["PATH"].split(os.pathsep) == ["/usr/bin", str(home.path("bin"))]      # last: it cannot shadow a system program


def test_gitignore_keeps_local_state_out(fresh):
    lines = (fresh / ".gitignore").read_text(encoding="utf-8").split("\n")
    for line in ("system/*", "!system/lm", "!system/lm.cmd", "!system/lightmind", "!system/lightmind.cmd", "!system/lmq", "!system/lmq.cmd", "!system/lmr", "!system/lmr.cmd", "!system/README.md", "runs/*", "!runs/README.md", "secrets/**/*.env"):
        assert line in lines
    assert not any(ln.startswith(".lm") for ln in lines)
    if HAVE_GIT:
        def ignored(rel: str) -> bool:
            return subprocess.run(["git", "check-ignore", "-q", rel], cwd=fresh,
                                  capture_output=True).returncode == 0
        subprocess.run(["git", "init", "-q"], cwd=fresh, check=True)
        for rel in ("system/runtime-windows-x86_64/Scripts/lm.exe", "system/python/x", "system/cache/x",
                    "system/bin/uv", "system/browsers/x", "system/claude/claude-abo/.credentials.json",
                    "system/state/ledger.jsonl", "system/state/templates.json", "system/wheels/lightmind-1-py3-none-any.whl",
                    "secrets/stripe/test.env", "runs/20261020-120000-a-0001/log.md"):
            assert ignored(rel), rel
        for rel in ("system/lm", "system/lm.cmd", "system/lightmind", "system/lightmind.cmd", "system/lmq", "system/lmq.cmd", "system/lmr", "system/lmr.cmd", "system/README.md", "runs/README.md", "secrets/stripe/info.md", "lightmind.md", "permissions.md", "tools/vault/status.py"):
            assert not ignored(rel), rel


def named(root: Path):
    """The places lightmind.md names (`Home`), in the spelling a launcher prints them."""
    home = Home(root)
    return home, {"UV_CACHE_DIR": home.path("cache"), "UV_PYTHON_INSTALL_DIR": home.path("python"),
                  "PLAYWRIGHT_BROWSERS_PATH": home.path("browsers"), "UV_TOOL_BIN_DIR": home.path("bin")}


@pytest.mark.skipif(os.name != "nt", reason="Windows launcher")
def test_windows_launcher_sets_what_lightmind_md_names(fresh, tmp_path):
    """lm.cmd starts the runtime of `paths.runtime` with the places of `Home` in its environment; a copy of
    cmd.exe stands in for lm.exe and prints that environment.  Windows often calls the variable `Path`, so
    names are looked up case-insensitively.  The launcher keeps `system/bin` LAST on PATH."""
    lau = Launcher("windows")
    home, places = named(fresh)
    lau.fake_runtime(fresh)
    uv = lau.fake_uv(tmp_path / "uv")
    done = lau.run(fresh, "/c", "set", env=uv, cwd=fresh.parent)
    assert done.returncode == 0, done.stderr
    env = environ_of(done.stdout)
    assert same(env["LIGHTMIND_HOME"], fresh)
    for var, place in places.items():
        assert same(env[var], place), var
    assert same(env["PATH"].split(os.pathsep)[-1], home.path("bin"))
    assert not any(same(d, home.path("bin")) for d in env["PATH"].split(os.pathsep)[:-1] if d)
    assert calls(uv) == []                                                 # a start with a runtime in place never calls uv


@pytest.mark.skipif(os.name != "posix" or shutil.which("sh") is None, reason="POSIX launcher")
def test_posix_launcher_sets_what_lightmind_md_names(fresh, tmp_path):
    lau = Launcher("posix")
    home, places = named(fresh)
    lau.fake_runtime(fresh)
    uv = lau.fake_uv(tmp_path / "uv")
    done = lau.run(fresh, env=uv)
    assert done.returncode == 0, done.stderr
    env = environ_of(done.stdout)
    assert same(env["LIGHTMIND_HOME"], fresh)
    for var, place in places.items():
        assert same(env[var], place), var
    assert same(env["PATH"].split(os.pathsep)[-1], home.path("bin"))
    assert calls(uv) == []


def test_track_file_lists_every_shipped_path(fresh):
    rec = json.loads((fresh / "system" / "state" / TRACK).read_text(encoding="utf-8"))
    assert rec["shipped"] == shipped_paths() and rec["version"]
    assert all((fresh / p).is_file() for p in rec["shipped"])


def test_init_refuses_an_existing_tree(fresh):
    with pytest.raises(FileExistsError):
        init_tree(fresh)


def test_init_default_target_is_lightmind_home(monkeypatch, tmp_path):
    monkeypatch.setenv("LIGHTMIND_HOME", str(tmp_path / "from-env"))
    res = init_tree()
    assert res.root == tmp_path / "from-env" and (res.root / ENTRY).is_file()


def test_init_never_overwrites(tmp_path):
    root = tmp_path / "lm"
    write(root / ".gitignore", "mine\n")
    res = init_tree(root)
    assert (root / ".gitignore").read_text(encoding="utf-8") == "mine\n" and ".gitignore" not in res.created


# ------------------------------------------------------------------ loaders
def test_loaders_skip_examples_and_readmes(fresh):
    h = Home(fresh)
    assert h.list_agents() == ["example-agent"] and h.list_triggers() == SHIPPED_TRIGGERS
    assert h.list_projects() == [] and {s.name for s in h.list_skills()} == SHIPPED_SKILLS
    assert h.list_servers() == ["laptop"]
    assert {t.name for t in h.list_tools()} == SHIPPED_TOOLS
    assert set(h.load_providers().accounts) == {"claude-abo"}
    with pytest.raises(ValueError):
        h.load_agent("_example")


def test_server_laptop_is_local_and_default(fresh):
    h = Home(fresh)
    s = h.load_server(h.defaults["server"])
    assert s.name == "laptop" and s.local and s.host == "local" and "default" in s.role


def test_vault_tool_is_found_without_a_manifest(fresh):
    t = Home(fresh).load_tool("vault")
    assert t.name == "vault" and t.run == "" and "status line" in t.description and t.path.endswith("vault")


def test_copied_examples_load(fresh):
    h = Home(fresh)
    shutil.copytree(fresh / "skills/_example", fresh / "skills/my-skill")
    shutil.copytree(fresh / "agents/_example", fresh / "agents/my-agent")
    shutil.copytree(fresh / "workflows/_example", fresh / "workflows/my-workflow")
    shutil.copytree(fresh / "projects/_example", fresh / "projects/my-project")
    shutil.copytree(fresh / "servers/_example", fresh / "servers/my-server")
    shutil.copytree(fresh / "tools/_example", fresh / "tools/example")
    shutil.copy(fresh / "triggers/_example.md", fresh / "triggers/morning.md")

    assert {s.name for s in h.list_skills()} == SHIPPED_SKILLS | {"my-skill"}
    assert "Say in one or two sentences" in next(x for x in h.list_skills() if x.name == "my-skill").description

    a = h.load_agent("my-agent")
    assert (a.name, a.effort, a.context_limit, a.lifetime) == ("my-agent", "medium", 200_000, "task")
    assert (a.budget_tokens, a.budget_usd) == (400_000, 2.0)
    assert [(t.kind, t.name) for t in a.tools] == [("skill", "my-skill"), ("tool", "bash"),
                                                   ("tool", "read"), ("tool", "write")]
    assert a.secrets == [] and a.servers == [] and a.provider == "" and a.may_spawn == []
    assert h.list_agents() == ["example-agent", "my-agent"]

    p = h.load_project("my-project")
    assert p.name == "my-project" and p.memory.lstrip().startswith("# my-project")
    assert p.assets == [["notes/brief.md", "the customer brief"]] and p.workdir == ""
    assert h.list_projects() == ["my-project"]

    s = h.load_server("my-server")
    assert (s.host, s.user, s.port, s.local) == ("example.lan", "me", 22, False)

    t = h.load_trigger("morning")
    assert t.when == "cron 0 7 * * 1-5" and t.agent == "my-agent" and t.enabled is False
    assert h.list_triggers() == sorted(["morning", *SHIPPED_TRIGGERS])
    weekly = h.load_trigger("example-weekly")                               # the shipped one: off until switched on
    assert (weekly.agent, weekly.when, weekly.enabled) == ("example-agent", "cron 0 8 * * 1", False)

    assert {t.name for t in h.list_tools()} == SHIPPED_TOOLS | {"example"}


def test_tools_md_grants_secrets_and_servers(fresh):
    d = fresh / "agents" / "mailer"
    write(d / "agentMaster.md", "---\nname: mailer\ndescription: d\npermissions:\n  secrets: [stripe/test]\n"
                                "  allow: [bash(ls*)]\n---\nbody\n")
    write(d / "tools.md", "# Skills\n- mail\n\n# Tools\n- bash\n\n# Secrets\n- mailhost/main\n"
                          "- stripe/test\n\n# Servers\n- buildbox\n")
    a = Home(fresh).load_agent("mailer")
    assert a.secrets == ["mailhost/main", "stripe/test"] and a.servers == ["buildbox"]
    assert a.permissions["secrets"] == ["stripe/test", "mailhost/main"]      # no duplicate, frontmatter first
    assert a.permissions["servers"] == ["buildbox"] and a.permissions["allow"] == ["bash(ls*)"]


def test_tools_md_accepts_the_original_example_format(fresh):
    d = fresh / "agents" / "old"
    write(d / "agentMaster.md", "---\nname: old\ndescription: d\nask_before: [money]\n---\nbody\n")
    write(d / "tools.md", "# Skills (short list; full text via skill_load)\n- crm-build\n# Connectors\n- smtp\n"
                          "# Secrets (folder in secrets/; agent reads only info.md)\n- mailhost: info\n"
                          "- stripe-acme\n# Tools\n- bash, files, browser\n# Servers\n"
                          "- buildbox: read, execute\n")
    a = Home(fresh).load_agent("old")
    assert [(t.kind, t.name) for t in a.tools] == [
        ("skill", "crm-build"), ("connector", "smtp"), ("tool", "bash"), ("tool", "files"), ("tool", "browser")]
    assert a.secrets == ["mailhost/info", "stripe-acme"] and a.servers == ["buildbox"]
    assert a.extra == {"ask_before": ["money"]}


def test_agent_without_tools_md_has_no_loadout(fresh):
    write(fresh / "agents" / "plain" / "agentMaster.md", "---\nname: plain\ndescription: d\nbudget: 100k\n---\nx\n")
    a = Home(fresh).load_agent("plain")
    assert a.tools == [] and a.secrets == [] and a.budget_tokens == 100_000 and a.budget_usd is None


def test_project_tools_md_is_the_only_source_of_project_grants(fresh):
    d = fresh / "projects" / "crm"
    write(d / "projectMemory.md", "---\nprovider: claude-abo\nrepo: git@x:y.git\npath: C:/work/crm\n"
                                  "home_server: buildbox\n---\nFacts.\n")
    write(d / "tools.md", "# Skills\n- crm-build\n# Secrets\n- stripe/test\n# Servers\n- buildbox\n")
    p = Home(fresh).load_project("crm")
    assert (p.provider, p.repo, p.workdir, p.home_server) == ("claude-abo", "git@x:y.git", "C:/work/crm", "buildbox")
    assert [(t.kind, t.name) for t in p.tools] == [("skill", "crm-build")]
    assert p.permissions == {"secrets": ["stripe/test"], "servers": ["buildbox"]} and p.extra == {}
    with pytest.raises(FileNotFoundError):
        Home(fresh).load_project("nothing")


def test_a_permissions_block_in_project_memory_is_ignored_with_a_warning(fresh):
    """projectMemory.md is not a protected file: an agent may write it, so it can never grant anything."""
    d = fresh / "projects" / "crm"
    write(d / "projectMemory.md", "---\npermissions:\n  secrets: [stripe/test]\n  allow: [bash(*)]\n"
                                  "  servers: [buildbox]\n---\nFacts.\n")
    with pytest.warns(UserWarning, match="permissions.*ignored.*tools.md"):
        p = Home(fresh).load_project("crm")
    assert p.permissions == {} and p.secrets == [] and p.servers == [] and p.extra == {}
    write(d / "tools.md", "# Secrets\n- mail/main\n")
    with pytest.warns(UserWarning):
        p = Home(fresh).load_project("crm")
    assert p.permissions == {"secrets": ["mail/main"]}                       # only what tools.md grants


def test_an_empty_permissions_block_in_project_memory_is_dropped_quietly(fresh):
    import warnings
    d = fresh / "projects" / "crm"
    write(d / "projectMemory.md", "---\npermissions:\n  allow: []\n  ask: []\n  deny: []\n---\nFacts.\n")
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        p = Home(fresh).load_project("crm")
    assert p.permissions == {} and p.extra == {}


def test_skills_accept_claude_code_folders_and_single_files(fresh):
    write(fresh / "skills" / "foreign" / "SKILL.md", "---\nname: foreign\ndescription: from the internet\n---\nhi\n")
    write(fresh / "skills" / "foreign" / "scripts" / "x.py", "print(1)\n")
    write(fresh / "skills" / "single.md", "---\ndescription: one file\n---\nbody\n")
    write(fresh / "skills" / "_hidden" / "SKILL.md", "---\nname: hidden\n---\n")
    got = {s.name: s for s in Home(fresh).list_skills()}
    assert set(got) == SHIPPED_SKILLS | {"foreign", "single"} and got["single"].description == "one file"


def test_tool_manifest_is_optional(fresh):
    write(fresh / "tools" / "mail" / "tool.md", "---\nname: mail\ndescription: send\nrun: uv run send.py\n"
                                                "category: send\nsecrets: [mailhost]\n---\nbody\n")
    write(fresh / "tools" / "doc" / "README.md", "---\ndescription: from the readme frontmatter\n---\n# doc\n")
    write(fresh / "tools" / "bare" / "README.md", "# bare\n\nDoes one thing.\n")
    got = {t.name: t for t in Home(fresh).list_tools()}
    assert set(got) == SHIPPED_TOOLS | {"mail", "doc", "bare"}
    assert got["mail"].run == "uv run send.py" and got["mail"].category == "send" and got["mail"].secrets == ["mailhost"]
    assert got["doc"].description == "from the readme frontmatter" and got["bare"].description == "Does one thing."
    with pytest.raises(FileNotFoundError):
        Home(fresh).load_tool("nothing")


def test_providers_one_account_per_file(fresh):
    h = Home(fresh)
    cfg = h.load_providers()
    a = cfg.accounts["claude-abo"]
    assert cfg.default == "claude-abo" == h.defaults["provider"]
    assert (a.type, a.config_dir, a.secret, a.default_model) == ("claude-subscription", "system/claude/claude-abo", "", "sonnet")
    assert a.tiers == {"strong": "opus", "medium": "sonnet", "cheap": "haiku"}
    assert cfg.tiers["medium"] == {"claude-abo": "sonnet"} and cfg.switch_at == 0.9 and cfg.aliases == {}
    assert h.prices() == {"opus": (15.0, 75.0, 18.75, 1.5), "sonnet": (3.0, 15.0, 3.75, 0.3),
                          "haiku": (1.0, 5.0, 1.25, 0.1)}
    assert "CHECK PRICES" in (fresh / "providers/claude-abo.md").read_text(encoding="utf-8")

    write(fresh / "providers" / "api-1.md", "---\ntype: anthropic-api\nsecret: anthropic/api-1\nalias: [side]\n"
                                            "fallback: [claude-abo]\nswitch_at: 0.8\nmodels:\n"
                                            "  sonnet: {input: 4, output: 20}\n  mini: {input: 1, output: 2, cache_read: 0.1}\n---\nnotes\n")
    write(fresh / "providers" / "_off.md", "---\ntype: openai\n---\n")
    cfg = h.load_providers()
    assert set(cfg.accounts) == {"claude-abo", "api-1"} and cfg.aliases == {"side": "api-1"}
    assert cfg.fallback == {"api-1": ["claude-abo"]} and cfg.accounts["api-1"].switch_at == 0.8
    assert h.prices()["sonnet"] == (3.0, 15.0, 3.75, 0.3)              # the default account's price wins
    assert h.prices("api-1")["sonnet"] == (4.0, 20.0, 4.0, 4.0)        # no cache price: the input price
    assert h.prices()["mini"] == (1.0, 2.0, 1.0, 0.1) and h.prices("nope") == {}


def test_default_provider_is_a_setting(fresh):
    write(fresh / "providers" / "other.md", "---\ntype: openrouter\nswitch_at: 0.5\n---\n")
    p = fresh / "config.md"                                   # the default provider is a setting of config.md
    p.write_text(p.read_text(encoding="utf-8").replace("provider: claude-abo", "provider: other"), encoding="utf-8")
    cfg = Home(fresh).load_providers()
    assert cfg.default == "other" and cfg.switch_at == 0.5


# ------------------------------------------------------------ settings + prompt
def test_settings_are_the_frontmatter(fresh):
    h = Home(fresh)
    assert set(h.settings) == {"paths", "files", "defaults", "shell", "tools", "on_start", "on_prompt", "on_end",
                               "context", "run", "ui", "sessions", "spawn", "quick", "watch", "workflow"}            # `sessions` comes from config.md
    assert h.defaults == {"server": "laptop", "provider": "claude-abo", "model": "sonnet", "effort": "medium",
                          "context": "200k", "mode": "auto"}
    assert h.settings["shell"] == "bash" and h.settings["tools"] == {"as_functions": False}
    ctx = h.settings["context"]
    assert ctx["handover_at"] == 0.7 and ctx["tool_output_inline"] == 2000 and ctx["preload_max_tokens"] == 8000
    assert ctx["order"][0] == "lightmind" and "--breakpoint--" in ctx["order"]
    ui = h.settings["ui"]
    assert ui["layout"] == "orbit" and "{ctx_bar}" in ui["statusline"]
    assert h.ui_rows("start_menu")[0] == ["New chat", "n", "new_chat"] and h.ui_rows("slash") == [["/clear", "new_chat"]]
    assert h.ui_rows("nothing") == []
    assert h.settings["run"] == {"max_parallel": 6, "slot_timeout": "10m", "max_depth": 3, "ask_channel": "tui", "ask_timeout": "30m"}


def test_settings_follow_edits_of_lightmind_md_and_config_md(fresh):
    h = Home(fresh)
    assert h.defaults["model"] == "sonnet"
    c = fresh / "config.md"
    c.write_text(c.read_text(encoding="utf-8").replace("model: sonnet", "model: opus"), encoding="utf-8")
    bump(c)
    assert h.defaults["model"] == "opus" and h.settings["defaults"]["model"] == "opus"
    p = fresh / ENTRY
    p.write_text(p.read_text(encoding="utf-8").replace("agents: agents/", "agents: bots/"), encoding="utf-8")
    bump(p)
    assert h.rel("agents") == "bots" and h.path("agents") == fresh.resolve() / "bots"


def test_key_doc_gives_a_settings_section_when_no_file_is_named(fresh):
    h = Home(fresh)
    assert h.key_doc("context").meta["handover_at"] == 0.7
    assert h.key_doc("permissions").meta["fallback"] == "allow"
    with pytest.raises(PathsError):
        h.key_doc("nothing")


def test_hook_lists_and_shell_are_plain_settings(fresh):
    """`loop.py` runs the hooks and `tools.py` the shell; the tree only has to hold them."""
    s = Home(fresh).settings
    (a,) = s["on_start"]
    assert a == {"run": 'uv run {tools}/vault/status.py "{vault}"', "show": True, "context": True, "timeout": 30}
    assert s["on_prompt"] == [] and s["on_end"] == [] and s["shell"] == "bash"
    assert s["tools"] == {"as_functions": False}
    text = (fresh / ENTRY).read_text(encoding="utf-8")
    for word in ("on_prompt", "env:", "timeout:", "show:", "context:", "CLAUDE_PROJECT_DIR", "stdin"):
        assert word in text, word


def test_replacing_the_public_settings_name_does_not_break_the_tree(fresh, monkeypatch):
    """Home's own lookups (paths, files, sections) read the cached document, not `Home.settings`;
    a test double or wrapper on that name only changes what other modules read through it."""
    monkeypatch.setattr(Home, "settings", property(lambda self: {"shell": "zsh"}), raising=False)
    h = Home(fresh)
    assert h.settings == {"shell": "zsh"}
    assert h.path("agents") == fresh / "agents" and h.paths["agents"].rstrip("/") == "agents"
    assert h.key_doc("context").meta["handover_at"] == 0.7 and h.defaults["model"] == "sonnet"
    assert h.fill("{tools}/x") == "tools/x" and h.master_prompt().startswith("# Lightmind")


def test_settings_can_be_replaced_per_instance_and_restored(fresh):
    """The session tests give a Home their own settings; Home's lookups keep reading the file."""
    h = Home(fresh)
    real = h.settings
    h.settings = {"defaults": {"provider": "x"}, "on_start": []}
    assert h.settings == {"defaults": {"provider": "x"}, "on_start": []}
    assert h.defaults["provider"] == "claude-abo" and h.path("tools") == fresh / "tools"
    assert h.load_providers().default == "claude-abo"
    h.settings = None
    assert h.settings is real or h.settings == real


def test_prices_read_the_provider_files_even_if_load_providers_is_replaced(fresh):
    h = Home(fresh)
    h.load_providers = lambda: None                        # a test double for the accounts
    assert h.prices()["haiku"] == (1.0, 5.0, 1.25, 0.1)
    assert h.prices("claude-abo")["opus"][0] == 15.0 and h.prices("nobody") == {}


# ------------------------------------------------------------------ master prompt
def test_master_prompt_fills_every_placeholder(fresh):
    h = Home(fresh)
    mp = h.master_prompt()
    assert mp.startswith("# Lightmind\n\nYou are an agent in Lightmind")
    assert "{" not in mp and "}" not in mp                    # nothing left unfilled
    assert f"`{fresh.resolve().as_posix()}`" in mp            # {home}
    for rel in ("skills", "tools", "agents", "workflows", "triggers", "servers", "projects", "providers",
                "secrets", "runs"):
        assert f"`{rel}/`" in mp, rel
    assert "`vault/`" in mp and "`permissions.md`" in mp and "`runs/README.md`" in mp
    assert h.master_prompt() == mp                            # byte-stable


def test_master_prompt_content(fresh):
    mp = Home(fresh).master_prompt()
    for must in ("Talk to the user in the language they write in", "Everything is a file",
                 "read that folder's `README.md`", "Read the `SKILL.md` before you use a skill",
                 "not function calls", "bash tool", "PEP 723", "uv run", "Never read or print `.env` values",
                 "tell the user at once", "search it before you ask the user", "vault skill",
                 "Ask the user before you spend money or send anything to customers", "handover",
                 "A denied call is final"):
        assert must in mp, must
    assert len(mp) <= 3600 and len(mp.split()) <= 590        # well under ~900 tokens (the timer/watch line made it 583 words)
    assert not re.search(r"\b(fallback|locked|deny:|allow:)", mp)   # no permission rules in the prompt


def test_master_prompt_follows_renamed_folders_and_keeps_unknown_placeholders(fresh):
    p = fresh / ENTRY
    p.write_text(p.read_text(encoding="utf-8").replace("skills: skills/", "skills: wissen/")
                 .replace("# Lightmind\n", "# Lightmind\n\nUnknown {nothing} and {file_skill} stay or fill.\n", 1),
                 encoding="utf-8")
    mp = Home(fresh).master_prompt()
    assert "`wissen/` skills" in mp and "{nothing}" in mp
    assert "Unknown {nothing} and SKILL.md stay or fill." in mp


def test_fill_handles_os_arch_and_extra(fresh):
    h = Home(fresh)
    os_, arch = platform_tag()
    assert h.fill("{os}-{arch} {runs} {x}", {"x": "y"}) == f"{os_}-{arch} runs y"


# ------------------------------------------------------------------ update
def test_update_creates_only_paths_never_shipped(fresh, monkeypatch):
    class Node:
        def __init__(self, data: bytes):
            self.data = data

        def read_bytes(self) -> bytes:
            return self.data

    real = list(I.templates())
    monkeypatch.setattr(I, "templates", lambda: iter(real + [("skills/new-example/SKILL.md", Node(b"new\n")),
                                                             ("brand-new.md", Node(b"#!shebang\n"))]))
    gone = fresh / "agents/README.md"
    gone.unlink()                                             # the user deleted a shipped file
    changed = fresh / "tools/README.md"
    changed.write_text("my own words\n", encoding="utf-8")    # and edited another
    created = update_tree(fresh)
    assert created == ["skills/new-example/SKILL.md", "brand-new.md"]
    assert not gone.exists()                                  # never recreated
    assert changed.read_text(encoding="utf-8") == "my own words\n"
    assert (fresh / "brand-new.md").read_bytes() == b"#!shebang\n"
    shipped = set(json.loads((fresh / "system" / "state" / TRACK).read_text(encoding="utf-8"))["shipped"])
    assert {"skills/new-example/SKILL.md", "brand-new.md", "agents/README.md"} <= shipped
    assert update_tree(fresh) == []                           # and a second run adds nothing


def test_update_is_a_noop_on_an_untouched_tree(fresh):
    before = {p: p.read_bytes() for p in fresh.rglob("*") if p.is_file() and TRACK not in p.name}
    assert update_tree(fresh) == []
    assert before == {p: p.read_bytes() for p in fresh.rglob("*") if p.is_file() and TRACK not in p.name}


def test_update_never_overwrites_a_file_the_user_created(fresh, monkeypatch):
    class Node:
        def read_bytes(self):
            return b"template\n"

    real = list(I.templates())
    monkeypatch.setattr(I, "templates", lambda: iter(real + [("notes.md", Node())]))
    write(fresh / "notes.md", "mine\n")
    assert update_tree(fresh) == [] and (fresh / "notes.md").read_text(encoding="utf-8") == "mine\n"


def test_update_needs_a_tree(tmp_path):
    with pytest.raises(FileNotFoundError):
        update_tree(tmp_path / "nowhere")


def test_update_rebuilds_a_deleted_system_folder_and_touches_nothing_else(fresh):
    """`system/` is the engine: git ignores it, the user may delete it, `lm` builds it again."""
    mine = fresh / "agents" / "README.md"
    mine.write_text("my words\n", encoding="utf-8")
    deleted_on_purpose = fresh / "tools" / "_example" / "example.py"
    deleted_on_purpose.unlink()
    shutil.rmtree(fresh / "system")
    created = update_tree(fresh)
    assert sorted(created) == ["system/README.md", "system/lightmind", "system/lightmind.cmd", "system/lm", "system/lm.cmd", "system/lmq", "system/lmq.cmd", "system/lmr", "system/lmr.cmd"]
    assert mine.read_text(encoding="utf-8") == "my words\n" and not deleted_on_purpose.exists()
    rec = json.loads((fresh / "system" / "state" / TRACK).read_text(encoding="utf-8"))
    assert set(rec["shipped"]) == set(shipped_paths())
    assert (fresh / "system" / "lm.cmd").read_bytes().count(b"\r\n") > 10
    if os.name == "posix":
        assert os.access(fresh / "system" / "lm", os.X_OK)
    assert (fresh / "system" / "state" / INSTALL_SOURCE).read_text(encoding="utf-8") == I.install_source() + "\n"
    assert update_tree(fresh) == []                           # and now it is an ordinary tree again


def test_update_schedules_a_runtime_rebuild_from_the_install_source(fresh):
    """The launcher prefers a kept wheel: `lm update` removes it and asks for a new runtime with a new stamp; no runtime
    folder (not even one of another machine type) is touched."""
    rt = fresh / "system" / "runtime-windows-x86_64"
    other = fresh / "system" / "runtime-linux-arm64"
    wheels = fresh / "system" / "wheels"
    for d in (rt, other, wheels):
        d.mkdir(parents=True)
    (rt / "lm-root.txt").write_text(str(fresh), encoding="utf-8")
    (other / "lm-root.some+old+name").write_bytes(b"")
    (wheels / "lightmind-1.0-py3-none-any.whl").write_bytes(b"w")
    (rt / "keep.txt").write_bytes(b"k")
    src = fresh / "system" / "state" / INSTALL_SOURCE
    src.write_text("folder C:/somewhere/lightmind-core\n", encoding="utf-8")
    stamp = fresh / "system" / "state" / "runtime-stamp.txt"
    update_tree(fresh)
    assert not list(wheels.glob("lightmind-*.whl"))
    assert (rt / "lm-root.txt").is_file() and (other / "lm-root.some+old+name").is_file()     # nobody's marker was removed
    assert (rt / "keep.txt").is_file() and src.is_file()      # the runtime stays: a new one is built next to it
    first = stamp.read_text(encoding="ascii")
    assert first
    update_tree(fresh)
    assert stamp.read_text(encoding="ascii") != first         # every `lm update` asks again


def test_update_keeps_the_wheel_when_it_is_the_only_source(fresh):
    rt = fresh / "system" / "runtime-windows-x86_64"
    wheels = fresh / "system" / "wheels"
    rt.mkdir(parents=True)
    wheels.mkdir(parents=True)
    (rt / "lm-root.txt").write_text(str(fresh), encoding="utf-8")
    whl = wheels / "lightmind-1.0-py3-none-any.whl"
    whl.write_bytes(b"w")
    (fresh / "system" / "state" / INSTALL_SOURCE).write_text(f"wheel {whl}\n", encoding="utf-8")
    update_tree(fresh)
    assert whl.is_file() and (rt / "lm-root.txt").is_file()
    assert not (fresh / "system" / "state" / "runtime-stamp.txt").exists()      # nothing new to build from


def test_the_engine_readme_warns_before_it_says_delete(fresh):
    text = (fresh / "system" / "README.md").read_text(encoding="utf-8")
    assert "delete the `runtimes` folder" in text and "claude/" in text and "Delete it and" not in text


def test_update_with_only_the_record_missing_makes_a_new_record(fresh):
    (fresh / "system" / "state" / TRACK).unlink()
    (fresh / "skills" / "_example" / "SKILL.md").unlink()
    assert update_tree(fresh) == []                           # nothing in system/ was missing
    assert (fresh / "system" / "state" / TRACK).is_file() and not (fresh / "skills" / "_example" / "SKILL.md").exists()


def test_update_without_record_or_system_location_cannot_tell_what_the_tree_holds(fresh):
    (fresh / "system" / "state" / TRACK).unlink()
    entry = fresh / ENTRY
    entry.write_text(re.sub(r"(?m)^  system:.*\n", "", entry.read_text(encoding="utf-8")), encoding="utf-8")
    with pytest.raises(FileNotFoundError, match="init"):
        update_tree(fresh)


# ------------------------------------------------------------------ vault repo
def run_git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout


def make_repo(path: Path, notes: int = 2) -> Path:
    path.mkdir(parents=True)
    run_git(path, "init", "-q", "-b", "main")
    for i in range(notes):
        write(path / f"note{i}.md", f"# note {i}\n")
    run_git(path, "add", "-A")
    run_git(path, "commit", "-q", "-m", "notes")
    return path


def test_repo_name():
    assert repo_name("https://github.com/someone/notes-repo") == "notes-repo"
    assert repo_name("https://github.com/someone/notes-repo.git/") == "notes-repo"
    assert repo_name("git@github.com:someone/notes-repo.git") == "notes-repo"
    assert repo_name(r"C:\repos\my vault") == "my-vault" and repo_name("/srv/x/notes") == "notes"
    assert repo_name("..") == "vault-repo" and repo_name("") == "vault-repo"


@pytest.mark.skipif(not HAVE_GIT, reason="git is not installed")
def test_init_with_vault_repo_clones_into_the_tree(tmp_path):
    origin = make_repo(tmp_path / "src" / "notes-repo")
    res = init_tree(tmp_path / "lm", vault_repo=str(origin))
    root = tmp_path / "lm"
    assert res.warnings == [] and res.vault == "vault/notes-repo"
    h = Home(root)
    assert h.rel("vault") == "vault/notes-repo" and h.path("vault") == root.resolve() / "vault" / "notes-repo"
    assert (root / "vault/notes-repo/.git").is_dir() and (root / "vault/notes-repo/note0.md").is_file()
    assert (root / "vault/README.md").is_file()                        # beside the clone, not inside it
    assert not (root / "vault/notes-repo/README.md").exists()
    assert "vault/notes-repo/" in (root / ".gitignore").read_text(encoding="utf-8").split("\n")
    text = (root / ENTRY).read_text(encoding="utf-8")
    assert "  vault: vault/notes-repo/" in text and "knowledge base" in text     # comment and layout kept
    assert len(re.findall(r"(?m)^\s+vault:", text)) == 1
    for name in (ENTRY, ".gitignore"):                                 # rewritten files keep their LF (Windows would make CRLF)
        assert b"\r" not in (root / name).read_bytes(), name
    assert 'status.py "vault/notes-repo"' in h.fill(h.settings["on_start"][0]["run"])
    rec = json.loads((root / "system" / "state" / TRACK).read_text(encoding="utf-8"))
    assert not any(p.startswith("vault/notes-repo") for p in rec["shipped"])
    assert run_git(root / "vault/notes-repo", "status", "--porcelain") == ""   # nothing was written into the clone


@pytest.mark.skipif(not HAVE_GIT, reason="git is not installed")
def test_failed_vault_clone_keeps_the_default_vault(tmp_path):
    res = init_tree(tmp_path / "lm", vault_repo=str(tmp_path / "does-not-exist"))
    assert res.vault == "vault" and len(res.warnings) == 1 and "clone failed" in res.warnings[0]
    assert Home(tmp_path / "lm").rel("vault") == "vault" and not (tmp_path / "lm/vault/does-not-exist").exists()


def test_vault_clone_without_git_is_a_warning(tmp_path):
    res = init_tree(tmp_path / "lm", vault_repo="https://example.invalid/x/y", git="git-that-does-not-exist")
    assert res.vault == "vault" and "clone failed" in res.warnings[0]


@pytest.mark.parametrize("repo", ["--upload-pack=touch x", "-c core.sshCommand=x", "  --help", "-", "   ",
                                  "a" + chr(10) + "b", "a" + chr(0) + "b", "a" + chr(13) + "b"])
def test_a_repo_value_git_would_take_for_an_option_is_refused_before_anything_is_created(tmp_path, repo):
    with pytest.raises(ValueError, match="not a git URL or path"):
        init_tree(tmp_path / "lm", vault_repo=repo)
    assert not (tmp_path / "lm").exists()


def test_no_vault_repo_means_no_clone(tmp_path):
    for i, none in enumerate((None, "")):
        res = init_tree(tmp_path / f"lm{i}", vault_repo=none)
        assert res.vault == "vault" and res.warnings == []


def test_the_clone_command_line_ends_the_options_and_limits_the_transports(tmp_path, monkeypatch):
    seen = {}

    def fake_run(argv, **kw):
        seen["argv"], seen["env"] = list(argv), dict(kw.get("env") or {})
        return subprocess.CompletedProcess(argv, 1, "", "fatal: stop")

    monkeypatch.setattr(I.subprocess, "run", fake_run)
    res = init_tree(tmp_path / "lm", vault_repo="  https://example.invalid/x/y.git  ")
    a = seen["argv"]
    assert a[:5] == ["git", "-c", "protocol.ext.allow=never", "clone", "--quiet"]
    assert a[5] == "--" and a[6] == "https://example.invalid/x/y.git" and a[7].endswith("y") and len(a) == 8
    assert seen["env"]["GIT_ALLOW_PROTOCOL"] == "https:ssh:git:file"
    assert res.vault == "vault" and "clone failed: fatal: stop" in res.warnings[0]


@pytest.mark.skipif(not HAVE_GIT, reason="git is not installed")
def test_git_itself_refuses_an_ext_remote_and_plain_http(tmp_path):
    marker = tmp_path / "pwned"
    res = init_tree(tmp_path / "lm", vault_repo=f"ext::sh -c 'touch {marker.as_posix()}'")
    assert not marker.exists() and res.vault == "vault"
    assert len(res.warnings) == 1 and "not allowed" in res.warnings[0]
    res = init_tree(tmp_path / "lm2", vault_repo="http://example.invalid/x/y")
    assert res.vault == "vault" and "not allowed" in res.warnings[0]
