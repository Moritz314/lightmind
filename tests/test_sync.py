"""lm sync: units (secrets as age files, hash rules, config) and integration with two temporary trees and a bare repo.

Never a real secret, never a real remote: every tree lives in tmp_path, every value is made up."""
import importlib.util
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from lightmind import checks, secrets as S, sync
from lightmind.files import Home
from lightmind.init import init_tree

from conftest import templates_dir

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
KEY = "sk-test-AAAAAAAAAAAAAAAA"
KEY2 = "sk-test-BBBBBBBBBBBBBBBB"


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    """No background threads: the tests call `sync.run` themselves."""
    monkeypatch.setattr(sync, "kick", lambda home: None)
    monkeypatch.delenv("LM_MACHINE", raising=False)
    monkeypatch.delenv("LM_DEPTH", raising=False)


def git(root: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                       capture_output=True, text=True, encoding="utf-8")
    assert r.returncode == 0, (args, r.stderr)
    return r.stdout


def remote_blob(bare: str, path: str) -> bytes | None:
    """`path` on the remote's main, or None when it is not there."""
    r = subprocess.run(["git", "-C", bare, "show", f"main:{path}"], capture_output=True)
    return r.stdout if r.returncode == 0 else None


def tracked(root: Path) -> list[str]:
    return git(root, "ls-files").splitlines()


def history(root: Path) -> list[str]:
    return git(root, "log", "--all", "--name-only", "--format=").splitlines()


@pytest.fixture
def bare(tmp_path) -> str:
    p = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(p)], check=True)
    return str(p)


def tree(tmp_path, name: str) -> Home:
    init_tree(tmp_path / name)
    return Home(tmp_path / name)


def _build_pair(base: Path) -> str:
    """A: initialised and pushed, with one secret.  B: joined, public key known, not yet a recipient.  Everything under `base`."""
    bare = base / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    a, b = tree(base, "a"), tree(base, "b")
    S.store_secret(a, "stripe/test", "STRIPE_KEY", KEY)
    sync.init(a, str(bare), "laptop")
    (b.root / "notes").mkdir()
    (b.root / "notes" / "b-only.md").write_text("only on b\n", encoding="utf-8")
    sync.join(b, str(bare), "server", confirm=lambda rec: True)     # the user said yes to the list shown at join
    return str(bare)


@pytest.fixture(scope="session")
def pair_template(tmp_path_factory) -> Path:
    """The expensive part (two trees, git, key pairs) once per worker; every test gets a copy (`pair`)."""
    mp = pytest.MonkeyPatch()
    mp.setattr(sync, "kick", lambda home: None)
    mp.delenv("LM_MACHINE", raising=False)
    mp.delenv("LM_DEPTH", raising=False)
    try:
        base = tmp_path_factory.mktemp("pair_template")
        _build_pair(base)
    finally:
        mp.undo()
    return base


@pytest.fixture
def pair(tmp_path, bare, pair_template):
    """A copy of the template pair in this test's own tmp_path; the remote is `bare` (the copy of the template's), A and B point at it."""
    shutil.rmtree(bare)
    shutil.copytree(pair_template / "remote.git", bare, symlinks=True)
    for name in ("a", "b"):
        shutil.copytree(pair_template / name, tmp_path / name, symlinks=True)
        git(tmp_path / name, "remote", "set-url", "origin", bare)
    return Home(tmp_path / "a"), Home(tmp_path / "b")


def add_b(a, b):
    sync.add_device(a, "server", sync.public_key(b))
    sync.run(b)


# ------------------------------------------------------------------------------------------------ units
def test_recipients_roundtrip_and_ignores_comments(home):
    ka, kb, kx = (_new_identity()[1] for _ in range(3))
    sync.write_recipients(home, {"b": kb, "a": ka})
    assert (home.path("secrets") / "recipients").read_text(encoding="utf-8") == f"a {ka}\nb {kb}\n"
    (home.path("secrets") / "recipients").write_text(f"# note\n\nx {kx}\n", encoding="utf-8")
    assert sync.read_recipients(home) == {"x": kx}


def test_two_recipients_each_can_open_a_third_cannot(tmp_path):
    h1, h2, h3 = (tree(tmp_path, n) for n in "123")
    rec = {"one": sync.make_identity(h1), "two": sync.make_identity(h2)}
    blob = sync.encrypt(b"hello", rec)
    assert sync.decrypt(h1, blob) == b"hello" and sync.decrypt(h2, blob) == b"hello"
    sync.make_identity(h3)
    assert sync.decrypt(h3, blob) is None


def test_instance_without_key_gets_a_warning_not_an_error(tmp_path, home):
    a = home
    sync.make_identity(a)
    sync.write_recipients(a, {"a": sync.public_key(a)})
    S.store_secret(a, "x/y", "K", KEY)
    sync.encrypt_ref(a, "x/y")
    other = tree(tmp_path, "o")
    shutil.copytree(a.path("secrets") / "x", other.path("secrets") / "x")
    (other.path("secrets") / "x" / "y.env").unlink()
    warns, changed = sync.reconcile(other, "main", after_pull=True)
    assert any("x/y" in w and "cannot be decrypted" in w for w in warns) and not changed
    assert not (other.path("secrets") / "x" / "y.env").exists()


def test_machine_name_and_local_overlay(home, monkeypatch):
    assert home.machine == ""
    (home.path("state")).mkdir(parents=True, exist_ok=True)
    (home.path("state") / "machine").write_text("laptop\n", encoding="utf-8")
    assert home.machine == "laptop"
    monkeypatch.setenv("LM_MACHINE", "server")
    assert home.machine == "server"
    (home.path("state") / "local.md").write_text("---\ndefaults:\n  server: homeserver\nrun:\n  always_on: true\n---\n", encoding="utf-8")
    assert home.settings["defaults"]["server"] == "homeserver" and home.settings["defaults"]["provider"]
    assert home.settings["run"]["always_on"] is True and "paths" in home.settings


def test_server_card_is_local_by_machine_with_host_local_as_transition(home):
    card = home.path("servers") / "homeserver"
    card.mkdir()
    (card / "info.md").write_text("---\nhost: homeserver.example\nmachine: homeserver\nssh: homeserver\n---\n", encoding="utf-8")
    (home.path("state")).mkdir(parents=True, exist_ok=True)
    (home.path("state") / "machine").write_text("laptop\n", encoding="utf-8")
    assert not home.load_server("homeserver").local
    (home.path("state") / "machine").write_text("homeserver\n", encoding="utf-8")
    s = home.load_server("homeserver")
    assert s.local and not s.legacy_local
    old = home.load_server("laptop")                                   # shipped card: `host: local`, no machine
    assert old.local and old.legacy_local


def _apply():
    spec = importlib.util.spec_from_file_location("apply_triggers", templates_dir() / "tools" / "triggers" / "apply.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_trigger_filter_by_machine(tmp_path, monkeypatch):
    ap = _apply()
    t = tmp_path / "triggers"
    t.mkdir()
    (t / "a.md").write_text('---\nschedule: "0 7 * * *"\nagent: x\ntask: y\nmachine: Server\n---\n', encoding="utf-8")
    (t / "b.md").write_text('---\nschedule: "0 8 * * *"\nagent: x\ntask: y\n---\n', encoding="utf-8")
    trig = {x["name"]: x for x in ap.load_triggers(t)}
    assert trig["a"]["machine"] == "Server" and trig["b"]["machine"] == ""
    assert ap.here(trig["a"], "server", False) and not ap.here(trig["a"], "laptop", True)
    assert not ap.here(trig["b"], "server", False) and ap.here(trig["b"], "server", True)
    assert ap.here(trig["b"], "", False)                               # no instance name: nothing is filtered
    state = tmp_path / "system" / "state"
    state.mkdir(parents=True)
    (state / "machine").write_text("server\n", encoding="utf-8")
    (state / "local.md").write_text("---\nalways_on: true\n---\n", encoding="utf-8")
    assert ap.instance(tmp_path) == ("server", True)


def test_home_loads_trigger_machine(home):
    (home.path("triggers") / "t.md").write_text("---\nwhen: cron 0 7 * * *\nagent: x\ntask: y\nmachine: server\n---\n", encoding="utf-8")
    assert home.load_trigger("t").machine == "server"


def test_not_a_sync_repo_is_silent(home):
    assert not sync.enabled(home)
    assert sync.run(home).status == "skip"
    assert sync.start_pull(home) is None
    sync.after_turn(home)
    sync.session_end(home)
    assert checks.check_sync(home)[0].status == checks.INFO


def test_nested_depth_does_no_automatic_sync(pair, monkeypatch):
    a, _ = pair
    monkeypatch.setenv("LM_DEPTH", "1")
    assert sync.start_pull(a) is None


# ------------------------------------------------------------------------------------------------ integration
def test_init_pushes_content_and_only_encrypted_secrets(pair, tmp_path, bare):
    a, _ = pair
    files = tracked(a.root)
    assert "secrets/stripe/test.env.age" in files and "secrets/recipients" in files and "lightmind.md" in files
    assert ".gitattributes" in files and ".gitignore" in files
    assert not [f for f in files + history(a.root) if f.endswith((".env", ".env.tmp")) or f.startswith("system/")]
    assert not any(f.startswith("system") for f in files)
    for blob in git(a.root, "log", "--all", "-p", "--format=").splitlines():
        assert KEY not in blob
    clone = tmp_path / "c"
    subprocess.run(["git", "clone", "-q", bare, str(clone)], check=True)
    assert KEY not in "".join(p.read_text("utf-8", "ignore") for p in clone.rglob("*") if p.is_file() and ".git" not in p.parts)
    assert (a.path("state") / "age.key").is_file() and sync.public_key(a) in (a.path("secrets") / "recipients").read_text()


def test_gitignore_is_a_positive_list_for_secrets(pair):
    a, _ = pair
    s = a.path("secrets")
    (s / "stripe" / "test.env.tmp").write_text("K=zzzzzzzzzzzz\n", encoding="utf-8")
    (s / "stripe" / "extra.key").write_text("x", encoding="utf-8")
    (s / "stripe" / "x.plain").write_text("x", encoding="utf-8")
    (s / "stripe" / ".credentials.json").write_text("{}", encoding="utf-8")
    (s / "stripe" / "note.txt").write_text("x", encoding="utf-8")
    (a.path("state") / "local.md").write_text("---\nrun:\n  always_on: true\n---\n", encoding="utf-8")
    (a.root / "x.md.lokal").write_text("x", encoding="utf-8")
    sync.run(a)
    files = tracked(a.root)
    assert "secrets/stripe/test.env.tmp" not in files
    assert not any(f.endswith((".key", ".plain", ".lokal", "credentials.json", "note.txt")) for f in files)
    assert not any(f.startswith("system") or f.endswith(".env") for f in files + history(a.root))


def test_secret_set_on_a_arrives_on_b(pair):
    a, b = pair
    add_b(a, b)
    assert (b.path("secrets") / "stripe" / "test.env").read_text("utf-8").count(KEY) == 1
    S.store_secret(a, "stripe/test", "STRIPE_KEY", KEY2)
    assert sync.run(a).status == "ok"
    r = sync.run(b)
    assert r.status == "ok", r.text()
    assert KEY2 in (b.path("secrets") / "stripe" / "test.env").read_text("utf-8")
    sync.run(a)
    assert "only on b" in (a.root / "notes" / "b-only.md").read_text("utf-8")      # b's own file went up


def test_join_keeps_differing_local_files_as_lokal(tmp_path, bare):
    a, b = tree(tmp_path, "a"), tree(tmp_path, "b")
    (b.path("servers") / "laptop" / "info.md").write_text("---\nhost: local\n---\nmine\n", encoding="utf-8")
    sync.init(a, bare, "laptop")
    out = sync.join(b, bare, "server")
    assert (b.path("servers") / "laptop" / "info.md.lokal").read_text("utf-8").endswith("mine\n")
    assert "mine" not in (b.path("servers") / "laptop" / "info.md").read_text("utf-8")
    assert any("info.md.lokal" in x for x in out) and any("age1" in x for x in out)
    assert not [f for f in tracked(b.root) if f.endswith(".lokal")]


def test_pull_does_not_overwrite_a_locally_changed_env_and_resolve_decides(pair):
    a, b = pair
    add_b(a, b)
    envb = b.path("secrets") / "stripe" / "test.env"
    S.store_secret(b, "stripe/test", "STRIPE_KEY", "sk-test-LOCAL-ON-B")      # changed on b (and not pushed yet) ...
    S.store_secret(a, "stripe/test", "STRIPE_KEY", KEY2)                       # ... and on a, which pushes first
    assert sync.run(a).status == "ok"
    r = sync.run(b)
    assert "LOCAL-ON-B" in envb.read_text("utf-8")
    assert any("stripe/test" in w and "nothing overwritten" in w for w in r.warnings)
    assert checks.check_sync(b, pull=False)[0].status == checks.WARN
    assert any("CONFLICT" in x for x in sync.status(b))
    assert sync.run(b).status == "warn" and "LOCAL-ON-B" in envb.read_text("utf-8")      # stays so until resolved
    sync.resolve(b, "stripe/test", "remote")
    assert KEY2 in envb.read_text("utf-8")
    assert sync.run(b).status == "ok" and not sync._store(b)["conflicts"]


def test_resolve_keep_local_pushes_the_local_value(pair):
    a, b = pair
    add_b(a, b)
    S.store_secret(b, "stripe/test", "STRIPE_KEY", "sk-test-LOCAL-ON-B")
    S.store_secret(a, "stripe/test", "STRIPE_KEY", KEY2)
    sync.run(a)
    sync.run(b)
    sync.resolve(b, "stripe/test", "local")
    sync.run(a)
    assert "LOCAL-ON-B" in (a.path("secrets") / "stripe" / "test.env").read_text("utf-8")


def test_text_conflict_aborts_cleanly_and_overwrites_nothing(pair):
    a, b = pair
    add_b(a, b)
    (a.root / "notes").mkdir(exist_ok=True)
    for h, text in ((a, "from a\n"), (b, "from b\n")):
        (h.root / "notes" / "shared.md").write_text(text, encoding="utf-8")
    assert sync.run(a).status == "ok"
    envb = (b.path("secrets") / "stripe" / "test.env").read_text("utf-8")
    r = sync.run(b)
    assert r.status == "conflict" and any("notes/shared.md" in w for w in r.warnings)
    assert (b.root / "notes" / "shared.md").read_text("utf-8") == "from b\n"
    assert not (b.root / ".git" / "rebase-merge").exists() and not (b.root / ".git" / "rebase-apply").exists()
    assert git(b.root, "status", "--porcelain", "--untracked-files=no").strip() == ""
    assert "<<<<" not in (b.root / "notes" / "shared.md").read_text("utf-8")
    assert (b.path("secrets") / "stripe" / "test.env").read_text("utf-8") == envb
    line = checks.check_sync(b, pull=False)[0]                          # the checklist must not say "ok" after this
    assert line.status == checks.WARN and "notes/shared.md" in line.detail


def test_checklist_warns_when_the_start_pull_was_skipped_by_a_lock(pair):
    a, _ = pair
    (a.path("state") / "sync.lock").write_text("x")                     # another sync holds the lock
    line = checks.check_sync(a)[0]                                      # pull=True: awaits the start pull
    assert line.status == checks.WARN and "another sync" in line.detail


def test_binary_conflict_aborts_too(pair):
    a, b = pair
    add_b(a, b)
    (a.root / "img.bin").write_bytes(b"\x00\x01 a")
    (b.root / "img.bin").write_bytes(b"\x00\x02 b")
    sync.run(a)
    assert sync.run(b).status == "conflict"
    assert (b.root / "img.bin").read_bytes() == b"\x00\x02 b"


def test_remove_device_reencrypts_without_it_and_lists_secrets(pair):
    a, b = pair
    add_b(a, b)
    old = (a.path("secrets") / "stripe" / "test.env.age").read_bytes()
    out = sync.remove_device(a, "server")
    assert "server" not in sync.read_recipients(a) and any("stripe/test" in x for x in out)
    new = (a.path("secrets") / "stripe" / "test.env.age").read_bytes()
    assert sync.decrypt(b, old) is not None and sync.decrypt(b, new) is None and sync.decrypt(a, new) is not None
    with pytest.raises(sync.SyncError):
        sync.remove_device(a, "laptop")                                  # not itself


def test_add_device_needs_a_valid_key_and_name(pair):
    a, _ = pair
    with pytest.raises(sync.SyncError):
        sync.add_device(a, "x", "not-a-key")
    with pytest.raises(sync.SyncError):
        sync.add_device(a, "Bad Name", sync.public_key(a))


def test_not_a_recipient_keeps_local_env_and_gets_a_warning(pair):
    a, b = pair
    envb = b.path("secrets") / "stripe" / "test.env"
    assert not envb.exists()
    S.store_secret(b, "stripe/test", "STRIPE_KEY", "sk-test-LOCAL-ON-B")
    r = sync.run(b)
    assert "LOCAL-ON-B" in envb.read_text("utf-8")                       # untouched
    assert not any(f.endswith("test.env") for f in tracked(b.root))


def test_secrets_tool_write_creates_the_age_file_at_once(pair):
    a, _ = pair
    S.secrets_tool(a, "new", "gh/bot", var="GH_TOKEN", generate=True)
    age = a.path("secrets") / "gh" / "bot.env.age"
    assert age.is_file()
    assert sync.decrypt(a, age.read_bytes()) == (a.path("secrets") / "gh" / "bot.env").read_bytes()


def test_masker_check_refuses_a_file_with_a_secret_value(pair):
    a, _ = pair
    (a.root / "notes").mkdir(exist_ok=True)
    (a.root / "notes" / "leak.md").write_text(f"token is {KEY}\n", encoding="utf-8")
    (a.root / "notes" / "fine.md").write_text("fine\n", encoding="utf-8")
    r = sync.run(a)
    assert "notes/fine.md" in tracked(a.root) and "notes/leak.md" not in tracked(a.root)
    assert any("notes/leak.md" in w and "secret value" in w for w in r.warnings)
    assert not any(KEY in w for w in r.warnings)


def test_big_file_and_nested_repo_are_refused_the_rest_goes_through(pair):
    a, _ = pair
    (a.root / "big.bin").write_bytes(b"x" * (sync.MAX_BYTES + 1))
    nested = a.root / "projects" / "inner"
    nested.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(nested)], check=True)
    (nested / "f.txt").write_text("x", encoding="utf-8")
    (a.root / "ok.md").write_text("ok\n", encoding="utf-8")
    r = sync.run(a)
    files = tracked(a.root)
    assert "ok.md" in files and "big.bin" not in files and not any(f.startswith("projects/inner") for f in files)
    assert any("big.bin" in w for w in r.warnings) and any("nested git repo" in w for w in r.warnings)


def test_lock_blocks_a_second_run(pair):
    a, _ = pair
    lock = a.path("state") / "sync.lock"
    lock.write_text("x")
    assert sync.run(a).status == "skip"
    lock.unlink()
    assert sync.run(a).status == "ok" and not lock.exists()


def test_offline_commits_locally_and_the_next_run_pushes(pair, tmp_path):
    a, _ = pair
    git(a.root, "remote", "set-url", "origin", str(tmp_path / "nowhere.git"))
    (a.root / "late.md").write_text("late\n", encoding="utf-8")
    r = sync.run(a)
    assert r.status == "offline" and "late.md" in tracked(a.root)
    assert checks.check_sync(a, pull=False)[0].status == checks.WARN
    git(a.root, "remote", "set-url", "origin", str(tmp_path / "remote.git"))
    assert sync.run(a).status == "ok" and sync._ahead(a, "main") == 0


def test_commit_message_names_machine_and_count(pair):
    a, _ = pair
    (a.root / "m.md").write_text("m\n", encoding="utf-8")
    sync.run(a)
    assert git(a.root, "log", "-1", "--format=%s").strip() == "sync laptop: 1 files"


def test_config_warnings_name_legacy_host_local_and_triggers_without_machine(pair):
    a, _ = pair
    (a.path("triggers") / "t.md").write_text("---\nwhen: cron 0 7 * * *\nagent: x\ntask: y\n---\n", encoding="utf-8")
    w = sync.config_warnings(a)
    assert any("laptop" in x and "host: local" in x for x in w) and any("trigger" in x and "t" in x for x in w)
    lines = checks.check_sync(a, pull=False)
    assert any(x.status == checks.WARN for x in lines)


def test_after_turn_commits_in_the_background_debounced(pair, monkeypatch):
    a, _ = pair
    ran = []
    monkeypatch.setattr(sync, "_bg", lambda fn, *args: ran.append((fn, args)) or type("T", (), {"join": lambda s, t=None: None})())
    sync._last_auto.clear()
    sync._timers.clear()
    sync.after_turn(a)
    assert len(ran) == 1                                                 # first call: at once
    sync.after_turn(a)
    assert len(ran) == 1 and str(a.root) in sync._timers                 # second within 30 s: one timer, no second run
    sync._timers.pop(str(a.root)).cancel()


def test_status_lists_recipients_and_decrypt_state(pair):
    a, b = pair
    txt = "\n".join(sync.status(a))
    assert "laptop (this)" in txt and "can decrypt  yes" in txt
    assert "can decrypt  no" in "\n".join(sync.status(b))


# ------------------------------------------------------------------------------------------------ review findings 2026-10-21
def test_a_lock_of_a_dead_process_is_taken_over_at_once(pair):
    a, _ = pair
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    lock = a.path("state") / "sync.lock"
    lock.write_text(f"{gone.pid}\n")
    assert sync.run(a).status == "ok" and not lock.exists()


def test_a_lock_held_by_a_live_process_still_blocks(pair):
    a, _ = pair
    lock = a.path("state") / "sync.lock"
    sleeper = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])      # a helper of this test
    try:
        lock.write_text(f"{sleeper.pid}\n")
        assert sync.run(a).status == "skip" and lock.exists()
    finally:
        sleeper.terminate()
        sleeper.wait()
    lock.write_text(f"{os.getpid()}\n")                                   # another thread of this very process
    assert sync.run(a).status == "skip" and lock.exists()


def test_an_interrupted_rebase_is_aborted_before_anything_is_pushed(pair, bare):
    a, b = pair
    add_b(a, b)
    (a.root / "shared.md").write_text("from a\n", encoding="utf-8")
    assert sync.run(a).status == "ok"
    (b.root / "shared.md").write_text("from b\n", encoding="utf-8")
    git(b.root, "add", "shared.md")
    git(b.root, "commit", "-qm", "b local")
    git(b.root, "fetch", "-q", "origin")
    stuck = subprocess.run(["git", "-C", str(b.root), "-c", "user.name=t", "-c", "user.email=t@t", "rebase", "origin/main"],
                           capture_output=True)                           # what a killed sync thread leaves behind
    assert stuck.returncode != 0 and (b.root / ".git" / "rebase-merge").exists()
    (b.root / "other.md").write_text("later work on b\n", encoding="utf-8")
    res = sync.run(b)
    assert not (b.root / ".git" / "rebase-merge").exists() and not (b.root / ".git" / "rebase-apply").exists()
    assert git(b.root, "symbolic-ref", "-q", "HEAD").strip() == "refs/heads/main"
    assert remote_blob(bare, "shared.md") == b"from a\n"                  # no conflict markers went up
    assert remote_blob(bare, "other.md") is None                          # nothing goes up while the pull conflicts
    assert res.status == "conflict" and "<<<<" not in (b.root / "shared.md").read_text("utf-8")


def test_commit_refuses_conflict_markers_and_unmerged_entries(pair):
    a, _ = pair
    notes = a.root / "notes"
    notes.mkdir(exist_ok=True)
    (notes / "marked.md").write_text("intro\n<<<<<<< HEAD\nours\n=======\ntheirs\n>>>>>>> origin/main\n", encoding="utf-8")
    (notes / "heading.md").write_text("Title\n=======\ntext\n", encoding="utf-8")        # a setext heading is no marker
    (notes / "fine.md").write_text("fine\n", encoding="utf-8")
    r = sync.run(a)
    files = tracked(a.root)
    assert "notes/fine.md" in files and "notes/heading.md" in files and "notes/marked.md" not in files
    assert any("notes/marked.md" in w and "conflict marker" in w for w in r.warnings)
    git(a.root, "checkout", "-q", "-b", "side")                           # an unmerged entry in the index
    (a.root / "x.md").write_text("side\n", encoding="utf-8")
    git(a.root, "add", "x.md")
    git(a.root, "commit", "-qm", "side")
    git(a.root, "checkout", "-q", "main")
    (a.root / "x.md").write_text("main\n", encoding="utf-8")
    git(a.root, "add", "x.md")
    git(a.root, "commit", "-qm", "main")
    subprocess.run(["git", "-C", str(a.root), "-c", "user.name=t", "-c", "user.email=t@t", "merge", "side"], capture_output=True)
    head = git(a.root, "rev-parse", "HEAD")
    n, warns = sync._commit(a)
    assert n == 0 and any("x.md" in w and "unmerged" in w for w in warns)
    assert git(a.root, "rev-parse", "HEAD") == head
    assert any(ln[:2] in ("AA", "UU") and ln.endswith("x.md") for ln in git(a.root, "status", "--porcelain").splitlines())


def test_masker_reads_the_staged_content_not_the_working_copy(pair, monkeypatch):
    a, _ = pair
    leak = a.root / "notes" / "leak.md"
    leak.parent.mkdir(exist_ok=True)
    leak.write_text(f"token is {KEY}\n", encoding="utf-8")
    real = sync._g

    def spy(home, *args, **kw):
        out = real(home, *args, **kw)
        if args[:1] == ("add",):                                          # the file changes after it was staged
            leak.write_text("clean now\n", encoding="utf-8")
        return out
    monkeypatch.setattr(sync, "_g", spy)
    r = sync.run(a)
    assert "notes/leak.md" not in tracked(a.root)
    assert any("notes/leak.md" in w and "secret value" in w for w in r.warnings)


def test_key_and_env_files_are_created_with_mode_600_from_the_start(home, monkeypatch):
    seen = []
    real = os.open

    def spy(path, flags, mode=0o777, **kw):
        seen.append((str(path), mode))
        return real(path, flags, mode, **kw)
    monkeypatch.setattr(os, "open", spy)
    sync.make_identity(home)
    sync._write_env(home.path("secrets") / "x" / "y.env", b"K=zzzzzzzz\n")
    assert any(p.endswith("age.key.tmp") and m == 0o600 for p, m in seen)
    assert any(p.endswith("y.env.tmp") and m == 0o600 for p, m in seen)


# ------------------------------------------------------------------------------------------------ the staged-content check fails closed
@pytest.fixture
def repo(tmp_path):
    """A tree that is a plain git repo with one local secret (no sync set up): enough for the functions that vet what is staged."""
    h = tree(tmp_path, "r")
    subprocess.run(["git", "-C", str(h.root), "init", "-q", "-b", "main"], check=True)
    (h.root / ".gitignore").write_text("system/\n*.env\n", encoding="utf-8")
    S.store_secret(h, "stripe/test", "STRIPE_KEY", KEY)
    return h


def _blob(root: Path, data: bytes) -> str:
    r = subprocess.run(["git", "-C", str(root), "hash-object", "-w", "--stdin"], input=data, capture_output=True, check=True)
    return r.stdout.decode().strip()


def _stage_raw(root: Path, entries: list[tuple[bytes, bytes]]) -> None:
    """Put entries straight into the index, no working copy needed: any name goes (protectNTFS off for the odd ones)."""
    lines = b"".join(b"100644 " + _blob(root, data).encode() + b"\t" + path + b"\0" for path, data in entries)
    subprocess.run(["git", "-C", str(root), "-c", "core.protectNTFS=false", "update-index", "--add", "-z", "--index-info"],
                   input=lines, capture_output=True, check=True)


def test_a_file_that_grew_after_the_size_check_is_refused_not_passed(repo, monkeypatch):
    grows = repo.root / "notes" / "grows.md"
    grows.parent.mkdir(exist_ok=True)
    grows.write_text("small\n", encoding="utf-8")
    real = sync._g

    def spy(home, *args, **kw):
        if args[:1] == ("add",):                                          # between the size check and the staging
            grows.write_bytes(b"x" * (sync.MAX_BYTES + 1))
        return real(home, *args, **kw)
    monkeypatch.setattr(sync, "_g", spy)
    n, warns = sync._commit(repo)
    assert "notes/grows.md" not in tracked(repo.root)
    assert any("notes/grows.md" in w and "MB" in w for w in warns)


def test_what_cannot_be_read_for_the_check_is_refused_by_name(repo, monkeypatch):
    notes = repo.root / "notes"
    notes.mkdir(exist_ok=True)
    (notes / "one.md").write_text("one\n", encoding="utf-8")
    (notes / "two.md").write_text("two\n", encoding="utf-8")
    real = sync._gb

    def broken(home, *args, **kw):                                        # git cat-file gives nothing usable
        return (1, b"") if args[:1] == ("cat-file",) else real(home, *args, **kw)
    monkeypatch.setattr(sync, "_gb", broken)
    n, warns = sync._commit(repo)
    files = tracked(repo.root)
    assert "notes/one.md" not in files and "notes/two.md" not in files
    assert any("notes/one.md" in w for w in warns) and any("notes/two.md" in w for w in warns)


def test_an_object_git_does_not_have_is_refused(repo, monkeypatch):
    (repo.root / "notes").mkdir(exist_ok=True)
    (repo.root / "notes" / "one.md").write_text("one\n", encoding="utf-8")
    real = sync._gb

    def lost(home, *args, **kw):                                          # every blob "missing" (pruned, a broken object db)
        if args[:2] == ("cat-file", "--batch-check"):
            return 0, b"".join(s.encode() + b" missing\n" for s in kw["input"].decode().split())
        return real(home, *args, **kw)
    monkeypatch.setattr(sync, "_gb", lost)
    n, warns = sync._commit(repo)
    assert "notes/one.md" not in tracked(repo.root) and any("notes/one.md" in w for w in warns)


def test_staged_blobs_are_found_by_object_id_not_by_path_spelling(repo):
    odd = {b"foo": b"AAA\n", b"a\nb": b"CCC\n", b"with\rcr": b"EEE\n", b"caf\xe9.md": b"DDD\n"}
    if os.name != "nt":
        odd[b"0:foo"] = b"BBB\n"                                          # `:0:foo` would name foo's blob, not this one
    _stage_raw(repo.root, list(odd.items()))
    by_path = {p: sha for st, p, mode, sha in sync._staged_changes(repo) if st == "A"}
    assert set(by_path) == set(odd)
    got, bad = sync._blobs(repo, list(by_path.values()))
    assert not bad and all(got[by_path[p]] == data for p, data in odd.items())


def test_vet_refuses_what_it_cannot_trust_and_passes_the_rest(repo):
    fine = _blob(repo.root, b"fine\n")
    leak = _blob(repo.root, f"token {KEY}\n".encode())
    marked = _blob(repo.root, b"<<<<<<< x\na\n=======\nb\n>>>>>>> y\n")
    changes = [("A", b"ok.md", "100644", fine), ("A", b"leak.md", "100644", leak), ("M", b"marked.md", "100644", marked),
               ("A", b"x\ny.md", "100644", fine), ("A", b"with\rcr.md", "100644", fine), ("A", b"caf\xe9.md", "100644", fine),
               ("A", b"inner", "160000", "1" * 40), ("A", b"ghost.md", "100644", "2" * 40), ("A", b"keys.env", "100644", fine),
               ("D", b"gone\nname.md", "000000", "0" * 40), ("A", b"secrets/x/y.env.age", "100644", fine)]
    refused = sync._vet(repo, changes)
    assert set(refused) == {b"leak.md", b"marked.md", b"x\ny.md", b"with\rcr.md", b"caf\xe9.md", b"inner", b"ghost.md", b"keys.env"}
    assert not any(KEY in w for w in refused.values())
    assert all(isinstance(w, str) and w.startswith("not committed") for w in refused.values())


def test_whatever_is_staged_by_hand_is_vetted_too(repo, monkeypatch):
    _stage_raw(repo.root, [(b"notes/ok.md", b"fine\n"), (b"x\ny.md", b"fine\n"), (b"keys.env", b"K=zzzzzzzz\n"),
                           (b"notes/leak.md", f"token {KEY}\n".encode())])
    monkeypatch.setattr(sync, "_entries", lambda home: [])                # the first phase sees nothing: it is all staged already
    n, warns = sync._commit(repo)
    assert git(repo.root, "ls-tree", "-r", "--name-only", "HEAD").splitlines() == ["notes/ok.md"] and n == 1
    assert any("keys.env" in w for w in warns) and any("notes/leak.md" in w for w in warns)
    assert any("line break" in w for w in warns) and not any(KEY in w for w in warns)


_REAL_KICK = sync.kick                                                    # the autouse fixture swaps it out for every test
WRITE_CHILD = r"""
import sys
from lightmind import secrets as S
from lightmind.files import Home
S.store_secret(Home(sys.argv[1]), "stripe/test", "STRIPE_KEY", sys.argv[2])
"""
END_CHILD = r"""
import sys, time
from lightmind import sync
from lightmind.files import Home
h = Home(sys.argv[1])
(h.root / "notes.md").write_text("changed in the last turn %s\n" % time.time(), encoding="utf-8")
sync.after_turn(h)                        # the debounced commit and push start in a thread
time.sleep(float(sys.argv[2]))            # write the result, close the session
sync.session_end(h)                       # commit and push, waits for its own run
"""


def test_session_end_waits_for_a_running_sync_instead_of_skipping(pair):
    a, _ = pair
    lock = a.path("state") / "sync.lock"
    lock.write_text(f"{os.getpid()}\n")                                   # after_turn's thread holds the lock ...
    threading.Timer(1.0, lock.unlink).start()                             # ... and is done in a second
    (a.root / "late.md").write_text("late\n", encoding="utf-8")
    sync.session_end(a, wait=15)
    assert "late.md" in tracked(a.root) and sync._ahead(a, "main") == 0


def test_the_exit_hook_waits_for_running_sync_threads_but_not_forever(monkeypatch):
    monkeypatch.setattr(sync, "_waited", 0.0)
    monkeypatch.setattr(sync, "EXIT_WAIT", 5.0)
    done = []
    sync._bg(lambda: (time.sleep(0.5), done.append(1)))
    sync._finish()
    assert done == [1]
    monkeypatch.setattr(sync, "EXIT_WAIT", 0.3)
    stop = threading.Event()
    sync._bg(lambda: stop.wait(10))
    t0 = time.monotonic()
    sync._finish()
    assert time.monotonic() - t0 < 3
    stop.set()
    pending = threading.Timer(60, lambda: None)                           # a debounce that never came due is dropped
    pending.daemon = True
    pending.start()
    sync._timers["some-tree"] = pending
    sync._finish()
    pending.join(5)
    assert not pending.is_alive() and "some-tree" not in sync._timers


def test_secret_write_commits_before_it_returns_and_hands_the_push_to_a_thread(pair, monkeypatch):
    a, _ = pair
    monkeypatch.setattr(sync, "kick", _REAL_KICK)
    started = []
    monkeypatch.setattr(sync, "_bg", lambda fn, *args: started.append((fn, args)) or type("T", (), {"join": lambda s, t=None: None})())
    S.store_secret(a, "stripe/test", "STRIPE_KEY", KEY2)
    assert "secrets/stripe/test.env.age" in git(a.root, "log", "-1", "--name-only", "--format=%s")     # committed already
    assert git(a.root, "status", "--porcelain", "--untracked-files=no").strip() == ""
    assert len(started) == 1                                              # pull and push: a thread the process end waits for


def test_secret_write_does_not_block_on_a_sync_thread_of_its_own_process(pair, monkeypatch):
    a, _ = pair
    monkeypatch.setattr(sync, "kick", _REAL_KICK)
    started = []
    monkeypatch.setattr(sync, "_bg", lambda fn, *args: started.append(fn) or type("T", (), {"join": lambda s, t=None: None})())
    (a.path("state") / "sync.lock").write_text(f"{os.getpid()}\n")           # the TUI's own pull holds the lock (the caller may be the UI)
    t0 = time.monotonic()
    S.store_secret(a, "stripe/test", "STRIPE_KEY", KEY2)
    assert time.monotonic() - t0 < 5 and len(started) == 1                  # the successor thread commits and pushes when it is free


def test_a_process_that_writes_a_secret_leaves_no_lock_and_the_change_is_pushed(pair, bare):
    a, _ = pair
    r = subprocess.run([sys.executable, "-c", WRITE_CHILD, str(a.root), KEY2], capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stderr
    assert not (a.path("state") / "sync.lock").exists()                   # released, not left behind for ten minutes
    assert git(a.root, "status", "--porcelain", "--untracked-files=no").strip() == ""
    assert sync._ahead(a, "main") == 0                                    # pushed before the process ended
    plain = sync.decrypt(a, remote_blob(bare, "secrets/stripe/test.env.age") or b"")
    assert plain is not None and KEY2.encode() in plain


@pytest.mark.parametrize("delay", ["0", "0.6"])
def test_the_last_turn_and_the_session_end_in_a_short_lived_process_lose_nothing(pair, bare, delay):
    a, _ = pair
    r = subprocess.run([sys.executable, "-c", END_CHILD, str(a.root), delay], capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, r.stderr
    assert not (a.path("state") / "sync.lock").exists()
    assert git(a.root, "status", "--porcelain", "--untracked-files=no").strip() == "" and not sync._entries(a)
    assert sync._ahead(a, "main") == 0
    assert remote_blob(bare, "notes.md") == (a.root / "notes.md").read_bytes().replace(b"\r\n", b"\n")      # the repo keeps LF


# ------------------------------------------------------------------------------------------------ remove-device locks the device out
def test_remove_device_locks_the_removed_instance_out_after_a_stale_write_and_after_a_conflict(tmp_path, bare, monkeypatch):
    a, b, c = (tree(tmp_path, n) for n in "abc")
    S.store_secret(a, "stripe/test", "STRIPE_KEY", KEY)
    sync.init(a, bare, "a")
    for name, h in (("b", b), ("c", c)):
        sync.join(h, bare, name, confirm=lambda rec: True)
        sync.add_device(a, name, sync.public_key(h))
    for h in (b, c, a):
        sync.run(h)
    sync.approve(b, "c", sync.fingerprint(sync.public_key(c)))                                                  # b joined before c existed: it confirms c by hand
    assert set(sync.read_recipients(b)) == {"a", "b", "c"}
    assert sync._store(a)["recipients"]["stripe/test"]["fp"] == sync._fp(sync.read_recipients(a))      # remembered per secret

    # a removes c; b has not pulled yet and writes a NEW secret for the list it still knows, then syncs
    sync.remove_device(a, "c")
    S.store_secret(b, "gh/new", "GH_TOKEN", "sk-test-NEW-AFTER-REMOVAL")
    sync.run(b)
    new = remote_blob(bare, "secrets/gh/new.env.age")
    assert new is not None and sync.decrypt(c, new) is None and sync.decrypt(a, new) is not None
    assert set(sync.read_recipients(b)) == {"a", "b"}

    # c is taken in again.  A plain run of a that has to pull while it pushes: its .age conflicts with b's change, the repo's
    # side wins, and that file is made for the list WITH c (what the checker's script showed)
    sync.run(a)
    sync.add_device(a, "c", sync.public_key(c))
    sync.run(b)
    sync.approve(b, "c", sync.fingerprint(sync.public_key(c)))
    sync.run(c)
    S.store_secret(b, "stripe/test", "STRIPE_KEY", "sk-test-CHANGED-ONCE")
    sync.run(b)                                                           # b has not seen what a does next
    rec = sync.read_recipients(a)
    del rec["c"]
    sync.write_recipients(a, rec)
    sync.reencrypt_all(a)
    sync.run(a, pull=False)
    assert sync.decrypt(c, remote_blob(bare, "secrets/stripe/test.env.age") or b"") is None

    # c is taken in once more, then removed through the command while b pushes a change inside a's window
    sync.run(a)
    sync.add_device(a, "c", sync.public_key(c))
    sync.run(b)
    sync.approve(b, "c", sync.fingerprint(sync.public_key(c)))
    sync.run(c)
    real = sync.reencrypt_all

    def racing(home):
        out = real(home)
        S.store_secret(b, "stripe/test", "STRIPE_KEY", "sk-test-CHANGED-ON-B")
        sync.run(b)                                                       # b still lists c as a recipient
        return out
    monkeypatch.setattr(sync, "reencrypt_all", racing)
    lines = sync.remove_device(a, "c")
    assert lines[0].startswith("c removed")                               # said only now: nothing is made for c any more
    blob = remote_blob(bare, "secrets/stripe/test.env.age")
    assert sync.decrypt(c, blob) is None
    for h in (a, b):
        assert b"CHANGED-ON-B" in (sync.decrypt(h, blob) or b"")          # b's change is kept, for the new list

    # nothing keeps rewriting: the instances that stay do not touch each other's files
    head = subprocess.run(["git", "-C", bare, "rev-parse", "main"], capture_output=True, text=True).stdout
    for h in (b, a, b, a):
        sync.run(h)
    assert subprocess.run(["git", "-C", bare, "rev-parse", "main"], capture_output=True, text=True).stdout == head


def test_remove_device_is_not_reported_while_a_secret_cannot_be_checked(pair):
    a, b = pair
    add_b(a, b)
    (a.path("secrets") / "x").mkdir()
    (a.path("secrets") / "x" / "y.env.age").write_bytes(sync.encrypt(b"K=zzzzzzzzzzzz\n", {"server": sync.public_key(b)}))   # not for a
    sync.run(a)
    with pytest.raises(sync.SyncError, match="x/y"):
        sync.remove_device(a, "server")
    assert "server" not in sync.read_recipients(a)                         # the list is changed ...
    assert any("REMOVING" in x and "server" in x for x in sync.status(a))  # ... but it is said that this is not finished
    (a.path("secrets") / "x" / "y.env.age").unlink()                       # nothing unreadable is left
    out = sync.remove_device(a, "server")                                  # the same command finishes the job
    assert out[0].startswith("server removed") and not any("REMOVING" in x for x in sync.status(a))


# ------------------------------------------------------------------------------------------------ the repo's recipient list is not trusted
def _new_identity():
    from pyrage import x25519
    ident = x25519.Identity.generate()
    return ident, str(ident.to_public())


def _push_from_clone(tmp_path: Path, bare: str, name: str, edit) -> None:
    """What anyone with write access to the repo can do: clone it, change the recipient list, push."""
    clone = tmp_path / name
    subprocess.run(["git", "clone", "-q", bare, str(clone)], check=True)
    edit(clone / "secrets" / "recipients")
    git(clone, "add", "-A")
    git(clone, "commit", "-qm", "list change")
    git(clone, "push", "-q", "origin", "HEAD:main")


def test_a_key_slipped_into_the_list_by_a_pull_is_used_nowhere_and_warns(pair, bare, tmp_path):
    import pyrage
    a, b = pair
    add_b(a, b)
    evil, pub = _new_identity()
    _push_from_clone(tmp_path, bare, "evil", lambda p: p.write_text(p.read_text("utf-8") + f"evil {pub}\n", encoding="utf-8"))
    for h in (a, b):
        sync.run(h)                                                       # the pull brings the foreign line
        assert "evil" in sync.read_recipients(h) and "evil" not in sync.effective_recipients(h)
    S.store_secret(a, "stripe/test", "STRIPE_KEY", KEY2)                  # what exists and what is written now: nobody makes it for evil
    sync.run(a)
    sync.run(b)
    S.store_secret(b, "gh/b", "GH_TOKEN", "sk-test-FROM-B")
    sync.run(b)
    for path in ("secrets/stripe/test.env.age", "secrets/gh/b.env.age"):
        blob = remote_blob(bare, path)
        assert blob is not None and sync.decrypt(a, blob) is not None and sync.decrypt(b, blob) is not None
        with pytest.raises(Exception):
            pyrage.decrypt(blob, [evil])
    for h in (a, b):                                                      # the warning: checklist and status
        line = checks.check_sync(h, pull=False)[0]
        assert line.status == checks.WARN and "evil" in line.detail and "approve" in line.detail
        assert any("evil" in x and "approve" in x for x in sync.status(h))
    out = sync.approve(a, "evil")                                         # without the fingerprint it only shows and explains
    assert sync.fingerprint(pub) in "\n".join(out) and "evil" not in sync.effective_recipients(a)
    with pytest.raises(Exception):
        pyrage.decrypt(remote_blob(bare, "secrets/stripe/test.env.age"), [evil])
    sync.approve(a, "evil", sync.fingerprint(pub))                        # only the user's word, with the fingerprint, takes it in
    assert KEY2.encode() in pyrage.decrypt(remote_blob(bare, "secrets/stripe/test.env.age"), [evil])


def test_a_removal_that_comes_with_a_pull_is_adopted(pair, bare, tmp_path):
    import pyrage
    a, b = pair
    add_b(a, b)
    x, pub = _new_identity()                                              # a device that a added and b confirmed by hand
    sync.add_device(a, "x", pub)
    sync.run(b)
    sync.approve(b, "x", sync.fingerprint(pub))
    assert pyrage.decrypt(remote_blob(bare, "secrets/stripe/test.env.age"), [x])
    _push_from_clone(tmp_path, bare, "drop", lambda p: p.write_text(
        "".join(ln for ln in p.read_text("utf-8").splitlines(True) if not ln.startswith("x ")), encoding="utf-8"))
    sync.run(b)                                                           # the pull takes x off the list: b makes its files again
    assert set(sync.effective_recipients(b)) == {"laptop", "server"} and pub not in sync.confirmed_keys(b)
    with pytest.raises(Exception):
        pyrage.decrypt(remote_blob(bare, "secrets/stripe/test.env.age"), [x])


def test_remove_device_needs_this_instance_in_the_list_and_never_reports_what_it_cannot_judge(pair):
    a, b = pair                                                           # b is not a recipient yet
    with pytest.raises(sync.SyncError, match="not a recipient"):
        sync.remove_device(b, "laptop")
    assert "laptop" in sync.read_recipients(b)                            # nothing was changed
    assert sync.rewrap(b, foreign=True) == ([], ["stripe/test"])          # cannot open it: "not checked", never "fine"
    assert sync.rewrap(b) == ([], [])


def test_join_confirms_only_itself_without_a_terminal_and_the_list_when_the_user_agrees(tmp_path, bare):
    a, b, c = (tree(tmp_path, n) for n in "abc")
    sync.init(a, bare, "laptop")
    assert sync.public_key(a) in sync.confirmed_keys(a)                   # init confirms its own line
    sync.join(b, bare, "server")                                          # no terminal here: only itself
    assert sync.confirmed_keys(b) == {sync.public_key(b)} and not sync.effective_recipients(b)
    assert sync.confirmed_pairs(b) == {"server": sync.public_key(b)}      # not a line of the repo's list, under any name
    assert any("laptop" in x and "approve" in x for x in sync.status(b))
    sync.approve(b, "laptop", sync.fingerprint(sync.public_key(a)))
    assert set(sync.effective_recipients(b)) == {"laptop"}
    sync.join(c, bare, "third", confirm=lambda rec: set(rec) == {"laptop"})      # the user is shown the list and says yes
    assert set(sync.effective_recipients(c)) == {"laptop"}


# ------------------------------------------------------------------------------------------------ deleting a secret
def test_a_secret_deleted_on_a_stays_deleted_on_both(pair, bare):
    a, b = pair
    add_b(a, b)
    d = a.path("secrets") / "stripe"
    (d / "test.env").unlink()
    (d / "test.env.age").unlink()
    assert sync.run(a).status == "ok"
    sync.run(b)
    sync.run(a)
    assert not (d / "test.env").exists() and not (d / "test.env.age").exists()
    assert remote_blob(bare, "secrets/stripe/test.env.age") is None
    db = b.path("secrets") / "stripe"
    assert not (db / "test.env").exists() and not (db / "test.env.age").exists()


def test_deleting_only_the_env_deletes_the_secret_everywhere(pair, bare):
    a, b = pair
    add_b(a, b)
    (a.path("secrets") / "stripe" / "test.env").unlink()
    sync.run(a)
    assert not (a.path("secrets") / "stripe" / "test.env").exists()       # not written back from the .age
    assert remote_blob(bare, "secrets/stripe/test.env.age") is None
    sync.run(b)
    assert not (b.path("secrets") / "stripe" / "test.env").exists() and not (b.path("secrets") / "stripe" / "test.env.age").exists()
    sync.run(a)
    assert not (a.path("secrets") / "stripe" / "test.env").exists() and not (a.path("secrets") / "stripe" / "test.env.age").exists()
    assert not sync._store(a)["hashes"] and not sync._store(b)["hashes"]


def test_a_lost_age_file_is_made_again_from_the_env_not_taken_for_a_deletion(pair, bare):
    a, _ = pair
    (a.path("secrets") / "stripe" / "test.env.age").unlink()
    sync.run(a)
    assert (a.path("secrets") / "stripe" / "test.env").is_file() and (a.path("secrets") / "stripe" / "test.env.age").is_file()
    assert remote_blob(bare, "secrets/stripe/test.env.age") is not None


def test_a_deletion_here_against_a_change_in_the_repo_is_a_conflict(pair, bare):
    a, b = pair
    add_b(a, b)
    S.store_secret(b, "stripe/test", "STRIPE_KEY", KEY2)
    assert sync.run(b).status == "ok"                                     # the repo has a new value
    (a.path("secrets") / "stripe" / "test.env").unlink()                  # a deletes it without having seen that
    r = sync.run(a)
    assert not (a.path("secrets") / "stripe" / "test.env").exists()       # not written back
    assert (a.path("secrets") / "stripe" / "test.env.age").is_file()      # not deleted: the repo's change is not ours to drop
    assert any("stripe/test" in w and "nothing overwritten" in w for w in r.warnings)
    assert "stripe/test" in sync._store(a)["conflicts"] and checks.check_sync(a, pull=False)[0].status == checks.WARN
    sync.resolve(a, "stripe/test", "local")                               # the deletion is meant
    assert remote_blob(bare, "secrets/stripe/test.env.age") is None
    assert not (a.path("secrets") / "stripe" / "test.env.age").exists()


def test_a_change_here_against_a_deletion_in_the_repo_is_a_conflict(pair, bare):
    a, b = pair
    add_b(a, b)
    S.store_secret(b, "stripe/test", "STRIPE_KEY", KEY2)                  # b changes it (not pushed yet) ...
    (a.path("secrets") / "stripe" / "test.env").unlink()                  # ... while a deletes it and pushes first
    assert sync.run(a).status == "ok"
    assert remote_blob(bare, "secrets/stripe/test.env.age") is None
    r = sync.run(b)
    envb = b.path("secrets") / "stripe" / "test.env"
    assert KEY2 in envb.read_text("utf-8")                                # b's value is not deleted behind its back
    assert any("stripe/test" in w and "nothing overwritten" in w for w in r.warnings)
    assert "stripe/test" in sync._store(b)["conflicts"]
    sync.resolve(b, "stripe/test", "local")                               # b keeps it: the secret is back for everyone
    sync.run(a)
    assert KEY2 in (a.path("secrets") / "stripe" / "test.env").read_text("utf-8")


def test_resolving_to_the_remote_side_when_the_repo_deleted_it_deletes_it_here_too(pair, bare):
    a, b = pair
    add_b(a, b)
    S.store_secret(b, "stripe/test", "STRIPE_KEY", KEY2)                  # b changes it (not pushed) ...
    (a.path("secrets") / "stripe" / "test.env").unlink()                  # ... while a deletes it and pushes first
    assert sync.run(a).status == "ok"
    sync.run(b)
    assert "stripe/test" in sync._store(b)["conflicts"]
    assert any("changed here, deleted in the repo" in x for x in sync.status(b))      # the kind of conflict is named
    sync.resolve(b, "stripe/test", "remote")
    db = b.path("secrets") / "stripe"
    assert not (db / "test.env").exists() and not (db / "test.env.age").exists()
    assert not sync._store(b)["conflicts"] and not sync._store(b)["hashes"] and sync.run(b).status == "ok"


def test_resolving_to_the_remote_side_when_the_repo_changed_it_brings_its_value_back(pair, bare):
    a, b = pair
    add_b(a, b)
    S.store_secret(b, "stripe/test", "STRIPE_KEY", KEY2)
    assert sync.run(b).status == "ok"
    (a.path("secrets") / "stripe" / "test.env").unlink()
    sync.run(a)
    assert any("deleted here, changed in the repo" in x for x in sync.status(a))
    sync.resolve(a, "stripe/test", "remote")
    assert KEY2 in (a.path("secrets") / "stripe" / "test.env").read_text("utf-8")
    assert not sync._store(a)["conflicts"] and sync.run(a).status == "ok"


# ------------------------------------------------------------------------------------------------ approve is bound to the key
def _evil_line(pub: str, name: str = "evil"):
    return lambda p: p.write_text(p.read_text("utf-8") + f"{name} {pub}\n", encoding="utf-8")


def _set_key(name: str, pub: str):
    """A list change as anyone with write access can make it: the line of `name` gets another key."""
    def edit(p):
        lines = [f"{name} {pub}\n" if ln.split()[:1] == [name] else ln for ln in p.read_text("utf-8").splitlines(True)]
        p.write_text("".join(lines), encoding="utf-8")
    return edit


def test_a_fingerprint_is_32_hex_digits_in_groups_of_four():
    import re
    assert re.fullmatch(r"([0-9a-f]{4}-){7}[0-9a-f]{4}", sync.fingerprint(_new_identity()[1]))


def test_approve_without_a_fingerprint_only_shows_it_and_says_how_to_confirm(pair, bare, tmp_path):
    a, _ = pair
    _, pub = _new_identity()
    _push_from_clone(tmp_path, bare, "evil", _evil_line(pub))
    sync.run(a)
    fp = sync.fingerprint(pub)
    out = "\n".join(sync.approve(a, "evil"))
    assert fp in out and f"lm sync approve evil {fp}" in out and "lm sync status" in out        # what to compare, what to type
    assert pub not in sync.confirmed_keys(a) and "evil" not in sync.effective_recipients(a)    # nothing was confirmed
    with pytest.raises(sync.SyncError, match="no device"):
        sync.approve(a, "nobody")


def test_approve_needs_the_fingerprint_of_the_key_in_the_list(pair, bare, tmp_path):
    a, _ = pair
    _, pub = _new_identity()
    _, other = _new_identity()
    _push_from_clone(tmp_path, bare, "evil", _evil_line(pub))
    sync.run(a)
    fp = sync.fingerprint(pub)
    digits = fp.replace("-", "")
    for bad in (sync.fingerprint(other), digits[:15], fp[:14], "zzzz-zzzz-zzzz-zzzz-zzzz", digits + "00"):
        with pytest.raises(sync.SyncError):                                   # another key, too short, no hex, too long
            sync.approve(a, "evil", bad)
        assert pub not in sync.confirmed_keys(a) and "evil" not in sync.effective_recipients(a)
    sync.approve(a, "evil", digits[:16].upper())                              # 16 digits are enough, case and dashes do not matter
    assert sync.effective_recipients(a)["evil"] == pub
    assert "confirmed already" in sync.approve(a, "evil", fp)[0]


def test_approve_refuses_when_a_pull_put_another_key_under_the_name(pair, bare, tmp_path):
    a, _ = pair
    _, k1 = _new_identity()
    _, k2 = _new_identity()
    _push_from_clone(tmp_path, bare, "evil1", _evil_line(k1))
    sync.run(a)
    shown = sync.fingerprint(k1)                                              # what the user looked at and compared
    _push_from_clone(tmp_path, bare, "evil2", _set_key("evil", k2))           # then another key takes its place in the repo
    sync.run(a)
    with pytest.raises(sync.SyncError, match="Nothing was confirmed"):
        sync.approve(a, "evil", shown)
    assert not ({k1, k2} & sync.confirmed_keys(a)) and "evil" not in sync.effective_recipients(a)
    sync.approve(a, "evil", sync.fingerprint(k2))                             # the key that stands there now, after looking again
    assert sync.effective_recipients(a)["evil"] == k2 and k1 not in sync.effective_recipients(a).values()


def test_approve_reads_the_list_again_when_it_holds_the_lock(pair, bare, tmp_path, monkeypatch):
    import contextlib
    a, _ = pair
    _, k1 = _new_identity()
    _, k2 = _new_identity()
    _push_from_clone(tmp_path, bare, "evil1", _evil_line(k1))
    sync.run(a)
    real = sync._lock

    @contextlib.contextmanager
    def swapping(home, wait=0.0):
        with real(home, wait) as got:
            _set_key("evil", k2)(home.path("secrets") / "recipients")        # a pull that finished right before the lock was ours
            yield got
    monkeypatch.setattr(sync, "_lock", swapping)
    with pytest.raises(sync.SyncError, match="Nothing was confirmed"):
        sync.approve(a, "evil", sync.fingerprint(k1))
    assert not ({k1, k2} & sync.confirmed_keys(a))


def test_approve_needs_the_sync_lock(pair, bare, tmp_path, monkeypatch):
    a, _ = pair
    _, pub = _new_identity()
    _push_from_clone(tmp_path, bare, "evil", _evil_line(pub))
    sync.run(a)
    monkeypatch.setattr(sync, "LOCK_WAIT", 0.3)
    with sync._lock(a) as got:                                                # a sync is running
        assert got
        with pytest.raises(sync.SyncError, match="another sync"):
            sync.approve(a, "evil", sync.fingerprint(pub))
    assert pub not in sync.confirmed_keys(a)
    sync.approve(a, "evil", sync.fingerprint(pub))                            # and goes through when it is free


def test_a_list_with_a_double_name_is_not_used_nothing_is_encrypted_and_it_warns(pair, bare, tmp_path):
    import pyrage
    a, b = pair
    add_b(a, b)
    evil, pub = _new_identity()
    _push_from_clone(tmp_path, bare, "dup", _evil_line(pub, "laptop"))        # a second line with the name of a's own
    r = sync.run(a)
    assert any("recipient list invalid" in w and "laptop" in w for w in r.warnings)
    assert sync.read_recipients(a) == {} and sync.effective_recipients(a) == {}
    assert "laptop~2" not in str(sync.read_list(a))                           # nothing is renamed quietly
    S.store_secret(a, "gh/z", "GH_TOKEN", "sk-test-NEW")                      # new: encrypted for nobody
    S.store_secret(a, "stripe/test", "STRIPE_KEY", KEY2)                      # changed: the .age stays as it was
    r = sync.run(a)
    assert not (a.path("secrets") / "gh" / "z.env.age").exists() and remote_blob(bare, "secrets/gh/z.env.age") is None
    blob = remote_blob(bare, "secrets/stripe/test.env.age")
    assert KEY.encode() in (sync.decrypt(a, blob) or b"") and KEY2.encode() not in (sync.decrypt(a, blob) or b"")
    with pytest.raises(Exception):
        pyrage.decrypt(blob, [evil])
    assert not sync._store(a)["conflicts"]                                    # a change that cannot be encrypted waits, it is no conflict
    line = checks.check_sync(a, pull=False)[0]
    assert line.status == checks.WARN and "recipient list invalid" in line.detail
    out = sync.status(a)
    assert any(x.startswith("recipients") and "INVALID" in x for x in out) and any("recipient list invalid" in x for x in out)
    for call in (lambda: sync.add_device(a, "third", _new_identity()[1]), lambda: sync.remove_device(a, "server"),
                 lambda: sync.approve(a, "laptop", "0" * 16), lambda: sync.approve(a, "laptop")):
        with pytest.raises(sync.SyncError, match="invalid"):
            call()
    sync.write_recipients(a, {"laptop": sync.public_key(a), "server": sync.public_key(b)})      # the user fixes the file
    r = sync.run(a)
    assert not any("invalid" in w for w in r.warnings)
    blob = remote_blob(bare, "secrets/stripe/test.env.age")                   # what had been waiting is made now
    assert KEY2.encode() in (sync.decrypt(a, blob) or b"") and remote_blob(bare, "secrets/gh/z.env.age") is not None


def test_a_list_that_could_deceive_is_invalid_and_prints_safely():
    _, pub = _new_identity()
    own = _new_identity()[1]
    for name in ("Laptop", "lаptop", "evil\x1b[2J", "a" * 40, "-x" * 20):                  # case, Cyrillic a, escape, too long
        rec, bad = sync._parse_list(f"laptop {own}\n{name} {pub}\n")
        assert bad and name not in rec
        assert all(c.isascii() and c.isprintable() for c in "".join(bad))
    for text in (f"x {pub} # comment\n", "x age1notakey\n", f"x {pub}\ny {pub}\n", f"x {pub}\nx {own}\n", "justonetoken\n",
                 f"x {pub} y\n"):
        assert sync._parse_list(text)[1], text
    rec, bad = sync._parse_list(f"﻿# a note\n\nx {pub}\n  y   {own}  \n")                  # comments, blanks and a BOM are fine
    assert rec == {"x": pub, "y": own} and not bad


def test_a_confirmed_name_that_carries_another_key_is_not_used_and_has_its_own_warning(pair, bare, tmp_path):
    import pyrage
    a, b = pair
    add_b(a, b)
    x1, k1 = _new_identity()
    x2, k2 = _new_identity()
    sync.add_device(a, "x", k1)
    sync.run(b)
    sync.approve(b, "x", sync.fingerprint(k1))
    assert sync.effective_recipients(b)["x"] == k1
    _push_from_clone(tmp_path, bare, "swap", _set_key("x", k2))
    sync.run(b)                                                               # the pull brings another key under the confirmed name
    assert "x" not in sync.effective_recipients(b)                            # not the old key (not listed), not the new one (unconfirmed)
    assert k1 not in sync.confirmed_keys(b)                                   # the old pair is out of what is trusted ...
    assert sync._store(b)["prev_fp"] == {"x": sync.fingerprint(k1)}           # ... only its fingerprint is kept, for the warning
    warns = sync.recipient_warnings(b)
    assert any(w.startswith("key of x has changed") and sync.fingerprint(k2) in w and sync.fingerprint(k1) in w for w in warns)
    assert not any("new device x" in w for w in warns)
    line = checks.check_sync(b, pull=False)[0]
    assert line.status == checks.WARN and "key of x has changed" in line.detail
    assert any("key of x has changed" in x for x in sync.status(b))
    S.store_secret(b, "gh/b", "GH_TOKEN", "sk-test-FROM-B")
    sync.run(b)
    blob = remote_blob(bare, "secrets/gh/b.env.age")
    for ident in (x1, x2):
        with pytest.raises(Exception):
            pyrage.decrypt(blob, [ident])
    sync.approve(b, "x", sync.fingerprint(k2))                                # after comparing the new key with its owner
    assert sync.effective_recipients(b)["x"] == k2 and k1 not in sync.confirmed_keys(b)
    assert pyrage.decrypt(remote_blob(bare, "secrets/gh/b.env.age"), [x2])


def test_add_device_does_not_replace_a_name_or_list_a_key_twice(pair):
    a, b = pair
    add_b(a, b)
    _, other = _new_identity()
    with pytest.raises(sync.SyncError, match="another key"):
        sync.add_device(a, "server", other)
    with pytest.raises(sync.SyncError, match="already"):
        sync.add_device(a, "third", sync.public_key(b))
    assert sync.read_recipients(a) == {"laptop": sync.public_key(a), "server": sync.public_key(b)}
    sync.add_device(a, "server", sync.public_key(b))                          # the same pair again is harmless (a repeated command)


def test_join_confirms_nothing_from_an_invalid_list(tmp_path, bare):
    a, b = tree(tmp_path, "a"), tree(tmp_path, "b")
    sync.init(a, bare, "laptop")
    _, pub = _new_identity()
    _push_from_clone(tmp_path, bare, "dup", _evil_line(pub, "laptop"))
    asked = []
    out = sync.join(b, bare, "server", confirm=lambda rec: asked.append(rec) or True)
    assert not asked                                                          # the user is not even asked to confirm it
    assert sync.confirmed_keys(b) == {sync.public_key(b)} and not sync.effective_recipients(b)
    assert any("invalid" in x for x in out)


# ------------------------------------------------------------------------------------------------ join has a way back
def snapshot(root: Path) -> tuple[dict[str, bytes], set[str]]:
    """Every file with its bytes and every folder of a tree: two equal snapshots are the same tree."""
    return ({p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()},
            {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_dir()})


def test_join_with_a_file_where_a_folder_is_stops_before_it_moves_anything(tmp_path, bare):
    a, b = tree(tmp_path, "a"), tree(tmp_path, "b")
    (a.root / "projects" / "plan").write_text("a file on a\n", encoding="utf-8")
    (b.root / "projects" / "plan").mkdir(parents=True)
    (b.root / "projects" / "plan" / "mine.md").write_text("b's own work\n", encoding="utf-8")
    (b.root / "lightmind.md").write_text((b.root / "lightmind.md").read_text("utf-8") + "\n<!-- b local tweak -->\n", encoding="utf-8")
    sync.init(a, bare, "laptop")
    before = snapshot(b.root)
    with pytest.raises(sync.SyncError, match="projects/plan"):
        sync.join(b, bare, "server")
    assert snapshot(b.root) == before and not (b.root / ".git").exists()      # the tree is exactly as it was
    Home(b.root)                                                              # and still opens
    shutil.rmtree(b.root / "projects" / "plan")                               # the user clears the collision
    sync.join(b, bare, "server")                                              # the same command now goes through
    assert (b.root / "lightmind.md.lokal").is_file() and (b.root / ".git").exists()


def test_join_that_fails_puts_every_moved_file_back_and_removes_what_it_made(tmp_path, bare, monkeypatch):
    a, b = tree(tmp_path, "a"), tree(tmp_path, "b")
    (b.path("servers") / "laptop" / "info.md").write_text("---\nhost: local\n---\nmine\n", encoding="utf-8")
    sync.init(a, bare, "laptop")
    before = snapshot(b.root)
    real = sync._bare_git

    def no_checkout(root, *args, **kw):                                       # the checkout fails after files were moved away
        return (1, "", "disk is full") if args[:1] == ("checkout",) else real(root, *args, **kw)
    monkeypatch.setattr(sync, "_bare_git", no_checkout)
    with pytest.raises(sync.SyncError, match="disk is full"):
        sync.join(b, bare, "server")
    assert snapshot(b.root) == before and not (b.root / ".git").exists()
    monkeypatch.setattr(sync, "_bare_git", real)
    real_key = sync.make_identity

    def no_key(home):                                                         # a failure after the checkout: the repo's files are in place
        raise OSError("no key for you")
    monkeypatch.setattr(sync, "make_identity", no_key)
    with pytest.raises(OSError):
        sync.join(b, bare, "server")
    assert snapshot(b.root) == before and not (b.root / ".git").exists()
    monkeypatch.setattr(sync, "make_identity", real_key)
    out = sync.join(b, bare, "server")                                        # and the tree was never in the way
    assert any("info.md.lokal" in x for x in out)


def test_join_interrupted_at_the_question_leaves_the_tree_as_it_was(tmp_path, bare):
    a, b = tree(tmp_path, "a"), tree(tmp_path, "b")
    sync.init(a, bare, "laptop")
    before = snapshot(b.root)

    def stop(rec):
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        sync.join(b, bare, "server", confirm=stop)
    assert snapshot(b.root) == before and not (b.root / ".git").exists()


def test_join_without_a_main_branch_or_a_reachable_repo_changes_nothing(tmp_path, bare):
    b = tree(tmp_path, "b")
    before = snapshot(b.root)
    with pytest.raises(sync.SyncError, match="no branch"):
        sync.join(b, bare, "server")                                          # the repo is empty
    assert snapshot(b.root) == before and not (b.root / ".git").exists()
    with pytest.raises(sync.SyncError):
        sync.join(b, str(tmp_path / "missing.git"), "server")
    assert snapshot(b.root) == before and not (b.root / ".git").exists()


def _drop_line(name: str):
    return lambda p: p.write_text("".join(ln for ln in p.read_text("utf-8").splitlines(True) if not ln.startswith(name + " ")),
                                  encoding="utf-8")


def test_an_old_key_that_comes_back_under_the_name_is_not_trusted_again(pair, bare, tmp_path):
    import pyrage
    a, b = pair
    add_b(a, b)
    x1, k1 = _new_identity()
    _, k2 = _new_identity()
    sync.add_device(a, "x", k1)
    sync.run(b)
    sync.approve(b, "x", sync.fingerprint(k1))
    _push_from_clone(tmp_path, bare, "swap", _set_key("x", k2))
    sync.run(b)
    _push_from_clone(tmp_path, bare, "back", _set_key("x", k1))                # the old key (compromised?) is written back
    sync.run(b)
    assert "x" not in sync.effective_recipients(b) and k1 not in sync.confirmed_keys(b)
    assert any("new device x" in w for w in sync.recipient_warnings(b))        # the key of before: not a change, and not trusted
    S.store_secret(b, "gh/b", "GH_TOKEN", "sk-test-FROM-B")
    sync.run(b)
    with pytest.raises(Exception):
        pyrage.decrypt(remote_blob(bare, "secrets/gh/b.env.age"), [x1])
    sync.approve(b, "x", sync.fingerprint(k1))                                 # only a new confirmation takes it in again
    assert sync.effective_recipients(b)["x"] == k1


def test_a_removed_device_whose_key_is_entered_again_is_not_trusted_again(pair, bare, tmp_path):
    import pyrage
    a, b = pair
    add_b(a, b)
    x1, k1 = _new_identity()
    sync.add_device(a, "x", k1)
    sync.run(b)
    sync.approve(b, "x", sync.fingerprint(k1))
    _push_from_clone(tmp_path, bare, "drop", _drop_line("x"))
    sync.run(b)
    assert k1 not in sync.confirmed_keys(b) and "x" not in sync._store(b)["prev_fp"]      # gone, and not remembered by name
    _push_from_clone(tmp_path, bare, "again", _evil_line(k1, "x"))
    sync.run(b)
    assert "x" not in sync.effective_recipients(b)
    assert any("new device x" in w for w in sync.recipient_warnings(b))
    S.store_secret(b, "gh/b", "GH_TOKEN", "sk-test-FROM-B")
    sync.run(b)
    with pytest.raises(Exception):
        pyrage.decrypt(remote_blob(bare, "secrets/gh/b.env.age"), [x1])


def test_a_removal_and_a_new_key_under_the_name_in_one_pull_count_as_a_change(pair, bare, tmp_path):
    a, b = pair
    add_b(a, b)
    _, k1 = _new_identity()
    _, k3 = _new_identity()
    sync.add_device(a, "x", k1)
    sync.run(b)
    sync.approve(b, "x", sync.fingerprint(k1))
    _push_from_clone(tmp_path, bare, "drop", _drop_line("x"))
    _push_from_clone(tmp_path, bare, "other", _evil_line(k3, "x"))             # b pulls both at once: the name never left, for b
    sync.run(b)
    assert k1 not in sync.confirmed_keys(b) and "x" not in sync.effective_recipients(b)
    assert any(w.startswith("key of x has changed") and sync.fingerprint(k3) in w for w in sync.recipient_warnings(b))


# ------------------------------------------------------------------------------------------------ last conditions of the re-check
def versions(bare: str, path: str) -> list[bytes]:
    """Every version of `path` that the remote's main ever had (newest first): all of it is readable by anyone with access to the repo."""
    revs = subprocess.run(["git", "-C", bare, "log", "--format=%H", "main", "--", path], capture_output=True, text=True).stdout.split()
    return [subprocess.run(["git", "-C", bare, "show", f"{r}:{path}"], capture_output=True).stdout for r in revs]


def opens(ident, blob: bytes) -> bool:
    import pyrage
    try:
        pyrage.decrypt(blob, [ident])
        return True
    except Exception:                                                     # noqa: BLE001
        return False


@pytest.fixture
def with_x(pair):
    """A and B are in; `x` is a third device of which only the key exists (nothing of it runs): A added it, B has confirmed it, so
    B writes for it too.  Returns (a, b, x identity, x public key)."""
    a, b = pair
    add_b(a, b)
    x, pub = _new_identity()
    sync.add_device(a, "x", pub)
    sync.run(b)
    sync.approve(b, "x", sync.fingerprint(pub))
    assert "x" in sync.effective_recipients(b)
    return a, b, x, pub


def test_join_refuses_a_list_that_gives_the_own_machine_name_to_another_key(tmp_path, bare):
    a, b, c = (tree(tmp_path, n) for n in "abc")
    S.store_secret(b, "srv/only", "SRV_TOKEN", "sk-test-SERVER-ONLY-SECRET")          # a secret the server had before it joined
    sync.init(a, bare, "laptop")
    evil, pub = _new_identity()
    _push_from_clone(tmp_path, bare, "plant", _evil_line(pub, "homeserver"))             # the name a server is likely to have
    before = snapshot(b.root)
    asked = []
    with pytest.raises(sync.SyncError, match="recipients already lists homeserver with a different key; refusing"):
        sync.join(b, bare, "homeserver", confirm=lambda rec: asked.append(rec) or True)  # the user's yes would not matter
    assert not asked and snapshot(b.root) == before and not (b.root / ".git").exists()
    sync.make_identity(c)                                                             # a tree that has its key already: its own line is fine
    sync.add_device(a, "third", sync.public_key(c))
    sync.join(c, bare, "third", confirm=lambda rec: True)
    assert sync.effective_recipients(c)["third"] == sync.public_key(c)


def test_join_checks_the_own_name_again_against_the_key_it_really_uses(tmp_path, bare, monkeypatch):
    a, b = tree(tmp_path, "a"), tree(tmp_path, "b")
    sync.init(a, bare, "laptop")
    _, pub = _new_identity()
    _push_from_clone(tmp_path, bare, "plant", _evil_line(pub, "homeserver"))
    before = snapshot(b.root)
    real, seen = sync._own_name_free, []

    def late_only(listed, name, own):                                     # the early check is skipped: only the one after the key exists
        seen.append(own)
        if len(seen) > 1:
            real(listed, name, own)
    monkeypatch.setattr(sync, "_own_name_free", late_only)
    with pytest.raises(sync.SyncError, match="different key"):
        sync.join(b, bare, "homeserver", confirm=lambda rec: True)
    assert len(seen) == 2 and seen[1] is not None
    assert snapshot(b.root) == before and not (b.root / ".git").exists()  # the late refusal puts everything back, the new key too


def test_a_secret_written_before_the_removal_was_pulled_leaves_no_readable_version_in_the_history(with_x, bare):
    a, b, x, pub = with_x
    sync.remove_device(a, "x")                                            # b has not pulled it
    S.store_secret(b, "gh/new", "GH_TOKEN", "sk-test-NEW-AFTER-REMOVAL")  # b still writes for x (and commits that)
    assert sync.run(b).status == "ok"                                     # the pull makes it again, without x
    vs = versions(bare, "secrets/gh/new.env.age")
    assert vs and not any(opens(x, v) for v in vs)                        # not the head, and not an older commit either
    assert sync.decrypt(a, vs[0]) is not None and sync.decrypt(b, vs[0]) is not None
    assert len(vs) == 1                                                   # the two commits went up as one


def test_a_push_that_has_to_pull_the_removal_first_does_not_push_the_old_version_either(with_x, bare, monkeypatch):
    a, b, x, pub = with_x
    S.store_secret(b, "gh/new", "GH_TOKEN", "sk-test-NEW-BEFORE-THE-PULL")
    real, calls = sync._pull, []

    def racing(home, branch):                                             # a removes x between b's pull and b's push
        out = real(home, branch)
        if not calls:
            calls.append(1)
            sync.remove_device(a, "x")
        return out
    monkeypatch.setattr(sync, "_pull", racing)
    assert sync.run(b).status == "ok"
    assert calls and "x" not in sync.read_recipients(b)                   # the push was rejected, pulled the removal, went again
    vs = versions(bare, "secrets/gh/new.env.age")
    assert vs and not any(opens(x, v) for v in vs)


def test_a_device_that_was_off_the_list_in_between_is_not_trusted_again_when_its_line_is_back(with_x, bare, tmp_path):
    a, b, x, pub = with_x
    sync.remove_device(a, "x")                                            # b does not pull meanwhile
    _push_from_clone(tmp_path, bare, "again", _evil_line(pub, "x"))       # the same line, written back
    S.store_secret(b, "gh/b1", "GH_TOKEN", "sk-test-WRITTEN-ON-B-AFTER-REMOVAL")
    r = sync.run(b)                                                       # b pulls the removal and the line in one go
    assert "x" in sync.read_recipients(b) and "x" not in sync.effective_recipients(b) and pub not in sync.confirmed_keys(b)
    assert r.status == "warn" and any("new device x" in w and "not confirmed" in w for w in r.warnings)
    vs = versions(bare, "secrets/gh/b1.env.age")
    assert vs and not any(opens(x, v) for v in vs)
    sync.approve(b, "x", sync.fingerprint(pub))                           # only a new confirmation takes it in again
    assert sync.effective_recipients(b)["x"] == pub


def test_remove_device_says_to_take_the_repo_access_away_first_and_names_every_secret_x_could_read(with_x, monkeypatch):
    a, b, x, pub = with_x
    S.store_secret(a, "old/acct", "OLD_TOKEN", "sk-test-OLD-TOKEN-VALUE")
    sync.run(a)                                                           # x was a recipient when this went up ...
    (a.path("secrets") / "old" / "acct.env").unlink()
    sync.run(a)                                                           # ... and it is deleted since, but still in the history
    real = sync.reencrypt_all

    def racing(home):                                                     # b writes for x between a's list change and a's push
        out = real(home)
        if home.root == a.root:
            S.store_secret(b, "gh/new", "GH_TOKEN", "sk-test-FROM-B-DURING-THE-REMOVAL")
            sync.run(b)
        return out
    monkeypatch.setattr(sync, "reencrypt_all", racing)
    out = sync.remove_device(a, "x")
    text = "\n".join(out)
    assert out[0].startswith("x removed")
    assert "deploy key" in text and text.index("deploy key") < text.index("change these secrets")      # said first
    assert "  stripe/test" in out and "  gh/new" in out                    # also what b wrote for x before it pulled the removal
    assert any(ln.startswith("  old/acct") and "deleted since" in ln for ln in out)


def test_a_refused_login_is_told_from_a_missing_network():
    refused = ["fatal: Authentication failed for 'https://example.invalid/r.git/'",
               "git@example.invalid: Permission denied (publickey).\nfatal: Could not read from remote repository.\n\n"
               "Please make sure you have the correct access rights\nand the repository exists.",
               "remote: Permission to me/r.git denied to deploy key.\nfatal: unable to access 'https://example.invalid/r.git/': "
               "The requested URL returned error: 403",
               "fatal: unable to access 'https://example.invalid/r.git/': The requested URL returned error: 401",
               "fatal: could not read Username for 'https://example.invalid': terminal prompts disabled"]
    network = ["fatal: unable to access 'https://example.invalid/r.git/': Could not resolve host: example.invalid",
               "ssh: connect to host example.invalid port 22: Connection timed out\nfatal: Could not read from remote repository.\n\n"
               "Please make sure you have the correct access rights\nand the repository exists.",
               "fatal: unable to access 'https://example.invalid/r.git/': Failed to connect to example.invalid port 443: Connection refused",
               "fatal: '/nowhere.git' does not appear to be a git repository\nfatal: Could not read from remote repository."]
    for err in refused:
        assert sync._net_fail(128, err)[0] == "auth", err
    for err in network:
        assert sync._net_fail(128, err)[0] == "offline", err
    assert sync._net_fail(-1, "timeout") == ("offline", "timeout")        # a timeout is never a login problem
    assert "Permission denied (publickey)" in sync._net_fail(128, refused[1])[1]      # the line that says it, not "and the repository exists."


def _refuse(m, command: str, err: str) -> None:
    """Let git's `command` fail the way a server does that refuses the login."""
    real = sync._g
    m.setattr(sync, "_g", lambda home, *args, **kw: (128, "", err) if args[:1] == (command,) else real(home, *args, **kw))


def test_run_says_auth_not_offline_when_the_repo_refuses_the_login_on_fetch_or_push(pair):
    a, _ = pair
    with pytest.MonkeyPatch.context() as m:
        _refuse(m, "fetch", "git@example.invalid: Permission denied (publickey).\nfatal: Could not read from remote repository.\n\n"
                            "Please make sure you have the correct access rights\nand the repository exists.")
        r = sync.run(a)
        assert r.status == "auth" and any(w.startswith("auth") and "Permission denied (publickey)" in w for w in r.warnings)
        assert not any(w.startswith("offline") for w in r.warnings)
        line = checks.check_sync(a, pull=False)[0]
        assert line.status == checks.WARN and "refused the login" in line.detail
        with pytest.raises(sync.SyncError, match=r"sync first \(auth\)"):
            sync.add_device(a, "y", _new_identity()[1])
    (a.root / "late.md").write_text("late\n", encoding="utf-8")
    with pytest.MonkeyPatch.context() as m:
        _refuse(m, "push", "remote: Permission to me/r.git denied to deploy key.\nfatal: unable to access 'https://example.invalid/r.git/': "
                           "The requested URL returned error: 403")
        r = sync.run(a)
    assert r.status == "auth" and any(w.startswith("auth") for w in r.warnings) and sync._ahead(a, "main") == 1
    assert sync.run(a).status == "ok" and sync._ahead(a, "main") == 0     # the login works again: it catches up


def test_fetch_and_push_get_the_longer_transfer_timeout(pair, monkeypatch):
    a, _ = pair
    assert sync.TRANSFER >= 300 > sync.NET
    seen, real = [], sync._g

    def spy(home, *args, **kw):
        seen.append((args[0], kw.get("timeout")))
        return real(home, *args, **kw)
    monkeypatch.setattr(sync, "_g", spy)
    (a.root / "t.md").write_text("t\n", encoding="utf-8")
    assert sync.run(a).status == "ok"
    assert ("fetch", sync.TRANSFER) in seen and ("push", sync.TRANSFER) in seen
    assert not any(t == sync.NET for call, t in seen if call in ("fetch", "push"))


def test_init_and_join_end_with_a_message_when_git_times_out_or_the_login_is_refused(tmp_path, bare):
    a, b = tree(tmp_path, "a"), tree(tmp_path, "b")
    before_a, before_b = snapshot(a.root), snapshot(b.root)
    real = subprocess.run

    def hang(cmd, *args, **kw):                                           # git does not answer within the time it is given
        if "ls-remote" in cmd or "fetch" in cmd:
            raise subprocess.TimeoutExpired(cmd, kw.get("timeout") or 0)
        return real(cmd, *args, **kw)
    with pytest.MonkeyPatch.context() as m:
        m.setattr(sync.subprocess, "run", hang)
        with pytest.raises(sync.SyncError, match="cannot reach the repo: timed out"):
            sync.init(a, bare, "laptop")
        with pytest.raises(sync.SyncError, match="cannot fetch the repo: timed out"):
            sync.join(b, bare, "server")
    assert snapshot(a.root) == before_a and not (a.root / ".git").exists()
    assert snapshot(b.root) == before_b and not (b.root / ".git").exists()
    denied = (128, "", "git@example.invalid: Permission denied (publickey).\nfatal: Could not read from remote repository.")
    with pytest.MonkeyPatch.context() as m:
        m.setattr(sync, "_bare_git", lambda root, *args, **kw: denied if "ls-remote" in args else (0, "", ""))
        with pytest.raises(sync.SyncError, match="refused the login"):
            sync.init(a, bare, "laptop")
    assert snapshot(a.root) == before_a and not (a.root / ".git").exists()


def test_status_and_the_checklist_say_that_a_commit_waits_for_its_push(pair):
    a, _ = pair
    assert not any("not pushed" in x for x in sync.status(a)) and "not pushed" not in checks.check_sync(a, pull=False)[0].detail
    (a.root / "wait.md").write_text("wait\n", encoding="utf-8")
    assert sync.run(a, pull=False, push=False).status == "ok" and sync._ahead(a, "main") == 1       # committed here, not pushed
    assert any(x.startswith("last sync") and x.endswith("(ok, 1 commit not pushed)") for x in sync.status(a))
    line = checks.check_sync(a, pull=False)[0]
    assert line.status == checks.OK and "1 commit not pushed" in line.detail
    (a.root / "wait2.md").write_text("wait\n", encoding="utf-8")
    sync.run(a, pull=False, push=False)
    assert any(x.endswith("(ok, 2 commits not pushed)") for x in sync.status(a))
    assert sync.run(a).status == "ok"
    assert not any("not pushed" in x for x in sync.status(a)) and "not pushed" not in checks.check_sync(a, pull=False)[0].detail


def test_a_file_named_age_goes_up_unread_only_when_it_starts_like_an_age_file(pair):
    a, _ = pair
    notes = a.root / "notes"
    notes.mkdir(exist_ok=True)
    (notes / "plain.txt.age").write_text(f"STRIPE_KEY={KEY}\n", encoding="utf-8")                  # a secret in plain text, only named .age
    (notes / "marked.age").write_text("<<<<<<< x\na\n=======\nb\n>>>>>>> y\n", encoding="utf-8")
    (notes / "head-only.age").write_text(f"age-encryption.org/v1 STRIPE_KEY={KEY}\n", encoding="utf-8")   # the header on the same line is none
    (notes / "real.age").write_bytes(sync.encrypt(f"STRIPE_KEY={KEY}\n".encode(), {"laptop": sync.public_key(a)}))
    r = sync.run(a)
    files = tracked(a.root)
    assert "notes/real.age" in files                                      # ciphertext passes
    assert not any(f in files for f in ("notes/plain.txt.age", "notes/marked.age", "notes/head-only.age"))
    assert any("notes/plain.txt.age" in w and "secret value" in w for w in r.warnings)
    assert any("notes/marked.age" in w and "conflict markers" in w for w in r.warnings) and not any(KEY in w for w in r.warnings)
