import os
import shutil
import sys
from importlib import resources
from pathlib import Path

import pytest

from lightmind import launch, settings, upload
from lightmind.files import Home
from lightmind.init import init_tree
from lightmind.providers.fake import FakeProvider, call, say

OVERLAY = Path(__file__).parent / "fixtures" / "overlay"


def templates_dir() -> Path:
    return Path(str(resources.files("lightmind") / "templates"))



def pytest_configure(config):
    """The tests run on Buildbox, never on the Windows laptop: local runs froze it twice on 2026-10-23.
    LM_LOCAL_TESTS=1 lets a single Windows-only test through."""
    if sys.platform == "win32" and os.environ.get("LM_LOCAL_TESTS") != "1":
        pytest.exit(
            "Lightmind tests run on Buildbox: git push -q buildbox <branch>, then "
            "ssh buildbox '~/lightmind-build/test.sh <branch> [pytest args]'. "
            "A single Windows-only test: set LM_LOCAL_TESTS=1.",
            returncode=4,
        )

@pytest.fixture(autouse=True)
def no_runtime_build(monkeypatch):
    """`lm update` builds a runtime through the tree's launcher (runtime.build_now): not in the tests, which would start
    a real uv.  The tests of that step name what they call."""
    monkeypatch.setenv("LM_NO_BUILD", "1")


@pytest.fixture(autouse=True)
def no_systemd_scope(monkeypatch):
    """`lm run --detach` would probe and use `systemd-run --user --scope` (proc.py): not in the tests."""
    monkeypatch.setenv("LM_NO_SCOPE", "1")


@pytest.fixture(autouse=True)
def no_boot_checklist(monkeypatch):
    """The Orbit tests start on the landing; the boot checklist (real git / ssh calls) has its own tests."""
    monkeypatch.setenv("LM_NO_BOOT", "1")


@pytest.fixture(autouse=True)
def no_gc_freeze(monkeypatch):
    """The start screen freezes what the process has built so far (`gc.freeze`): nothing frozen is ever freed, and every
    Orbit test builds a whole app, so the tests leave it off (tests/test_tui_frames.py switches it on for itself)."""
    monkeypatch.setenv("LM_NO_FREEZE", "1")


@pytest.fixture(autouse=True)
def no_terminal_spawn(monkeypatch):
    """No test opens a real terminal tab or window: `launch.spawn` fails loudly unless a test replaces it."""
    def refuse(plan, tree, *args, **kw):
        raise AssertionError(f"a test tried to start a terminal: {plan.argv}")
    monkeypatch.setattr(launch, "spawn", refuse)


@pytest.fixture(autouse=True)
def no_file_dialog(monkeypatch):
    """No test opens a real file dialog (`/upload`): the one place that starts the dialog process fails loudly."""
    def refuse(argv, env):
        raise AssertionError(f"a test tried to open a file dialog: {argv[:1]}")
    monkeypatch.setattr(upload, "_run", refuse)


@pytest.fixture
def tree(tmp_path) -> Path:
    """A fresh tree made by `init_tree` from the shipped templates, plus tests/fixtures/overlay:
    a pdf tool, two skills, an agent and a provider account whose login is a secret.  The shipped
    tree itself holds no such items (test_tree*.py checks it without the overlay).  `sessions.open` is
    `replace`, as before tabs: a chat opens in the screen that asked (the tests of tabs set it themselves)."""
    dst = tmp_path / "lm"
    init_tree(dst)
    shutil.copytree(OVERLAY, dst, dirs_exist_ok=True)
    cfg = dst / "config.md"
    cfg.write_bytes(settings.edit_text(cfg.read_bytes().decode("utf-8"), {("sessions", "open"): "replace"}).encode("utf-8"))
    return dst


@pytest.fixture
def home(tree) -> Home:
    return Home(tree)


LEGACY_PERMISSIONS = Path(__file__).parent / "fixtures" / "rechte" / "template-before-permissions.md"


def legacy_modes(root: Path | Home) -> Path:
    """Turn a tree back into the older layout: no mode files, the modes in the `modes:` block of permissions.md (the
    shipped file of the version before modes/).  For tests of that path, which the core still reads.  Returns the
    permissions file."""
    root = root.root if isinstance(root, Home) else Path(root)
    shutil.rmtree(root / "modes", ignore_errors=True)
    (root / "rechte.md").unlink(missing_ok=True)          # a catalog without mode files is a switched tree that lost them
    shutil.copy(LEGACY_PERMISSIONS, root / "permissions.md")
    return root / "permissions.md"


@pytest.fixture
def fake():
    """Factory: fake(script) -> FakeProvider."""
    return lambda script, **kw: FakeProvider(script, **kw)


__all__ = ["say", "call"]


TUI_MODULES = ("test_tui", "test_tui_columns", "test_tui_frames", "test_tui_queue", "test_tui_safety", "test_tui_settings",
               "test_tui_slash", "test_tui_tabs", "test_tui_todos", "test_tui_wishes")
MIXED_MODULES = ("test_boot", "test_release", "test_remote", "test_spawn", "test_subagents", "test_todos", "test_serve")


def pytest_collection_modifyitems(items):
    """Every test that starts a whole Textual app gets the marker `tui`: the files made of such tests as a whole, and in the
    mixed files the tests that use the Pilot (`running(` / `run_test`) or the Orbit app.  `-m "not tui"` is the quick run."""
    import inspect
    for item in items:
        mod = item.module.__name__.rsplit(".", 1)[-1]
        if mod in TUI_MODULES:
            item.add_marker(pytest.mark.tui)
        elif mod in MIXED_MODULES and getattr(item, "obj", None) is not None:
            try:
                src = inspect.getsource(item.obj)
            except (OSError, TypeError):
                continue
            if "running(" in src or "run_test" in src or "Orbit(" in src:
                item.add_marker(pytest.mark.tui)
