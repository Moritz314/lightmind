"""tools/vault/status.py: one line about the vault, run the way the on_start hook runs it."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from lightmind.init import init_tree

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@t"}


@pytest.fixture(scope="module")
def status_py(tmp_path_factory) -> Path:
    root = tmp_path_factory.mktemp("tree") / "lm"
    init_tree(root)
    return root / "tools" / "vault" / "status.py"


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, env=ENV, check=True, capture_output=True, text=True).stdout


def note(path: Path, name: str, text: str = "# n\n") -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / name).write_text(text, encoding="utf-8")


def status(status_py: Path, *args: str):
    p = subprocess.run([sys.executable, str(status_py), *args], capture_output=True, env=ENV, timeout=120)
    return p.returncode, p.stdout.decode("utf-8"), p.stderr.decode("utf-8")


def one_line(status_py: Path, path: Path) -> str:
    code, out, err = status(status_py, str(path))
    assert code == 0 and err == ""
    assert out.endswith("\n") and out.count("\n") == 1, out          # exactly one line
    return out.rstrip("\n")


@pytest.fixture
def remote(tmp_path) -> Path:
    """A bare remote with two notes on `main`, and a clone of it."""
    seed = tmp_path / "seed"
    seed.mkdir()
    git(seed, "init", "-q", "-b", "main")
    note(seed, "a.md")
    note(seed / "sub", "b.md")
    git(seed, "add", "-A")
    git(seed, "commit", "-q", "-m", "first")
    bare = tmp_path / "remote.git"
    git(tmp_path, "clone", "-q", "--bare", str(seed), str(bare))
    git(tmp_path, "clone", "-q", str(bare), str(tmp_path / "vault"))
    return bare


def test_connected_and_clean(status_py, remote, tmp_path):
    assert one_line(status_py, tmp_path / "vault") == "Vault connected · 2 notes · main · clean"


def test_dirty_working_tree(status_py, remote, tmp_path):
    note(tmp_path / "vault", "c.md")
    assert one_line(status_py, tmp_path / "vault") == "Vault connected · 3 notes · main · changed"


def test_pulls_news_from_the_remote(status_py, remote, tmp_path):
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", str(remote), str(other))
    note(other, "new.md")
    git(other, "add", "-A")
    git(other, "commit", "-q", "-m", "second")
    git(other, "push", "-q", "origin", "main")
    assert one_line(status_py, tmp_path / "vault") == "Vault connected · 3 notes · main · clean"
    assert (tmp_path / "vault" / "new.md").is_file()


def test_offline_is_tolerated_and_named(status_py, remote, tmp_path):
    shutil.rmtree(remote, onerror=lambda f, p, e: (os.chmod(p, 0o700), f(p)))
    assert one_line(status_py, tmp_path / "vault") == \
        "Vault connected · 2 notes · main · clean · pull failed (offline?)"


def test_one_note_is_singular(status_py, tmp_path):
    v = tmp_path / "v"
    v.mkdir()
    git(v, "init", "-q", "-b", "main")
    note(v, "only.md")
    git(v, "add", "-A")
    git(v, "commit", "-q", "-m", "x")
    assert one_line(status_py, v) == "Vault connected · 1 note · main · clean · no remote"


def test_git_and_obsidian_folders_and_other_files_are_not_notes(status_py, tmp_path):
    v = tmp_path / "v"
    note(v, "a.md")
    note(v / ".obsidian", "workspace.md")
    note(v / ".git" / "x", "y.md")
    note(v / "sub", "b.MD")
    note(v / "sub", "c.txt")
    assert one_line(status_py, v) == "Vault present · 2 notes · no git repo of its own"


def test_repo_without_a_commit(status_py, tmp_path):
    v = tmp_path / "v"
    v.mkdir()
    git(v, "init", "-q", "-b", "main")
    assert one_line(status_py, v) == "Vault connected · 0 notes · main · clean · no remote"


def test_plain_folder_is_not_pulled(status_py, tmp_path):
    v = tmp_path / "v"
    note(v, "a.md")
    assert one_line(status_py, v) == "Vault present · 1 note · no git repo of its own"


def test_folder_inside_another_repo_is_left_alone(status_py, remote, tmp_path):
    outer = tmp_path / "vault"                      # a clone with a remote: a pull here would change files
    note(outer / "inner", "x.md")
    sha = git(outer, "rev-parse", "HEAD")
    assert one_line(status_py, outer / "inner") == "Vault present · 1 note · no git repo of its own"
    assert git(outer, "rev-parse", "HEAD") == sha


def test_missing_folder_and_missing_argument_are_one_warning_line(status_py, tmp_path):
    assert one_line(status_py, tmp_path / "nowhere").startswith("Vault not found: ")
    code, out, _ = status(status_py)
    assert code == 0 and out == "Vault: no folder given (status.py <folder>)\n"


def test_runs_as_the_hook_does_in_a_fresh_tree(tmp_path):
    """The shipped on_start command, filled in and run by the shell of the settings in the tree root."""
    from lightmind.files import Home
    from lightmind.tools import resolve_shell
    root = tmp_path / "lm"
    init_tree(root)
    home = Home(root)
    entry = home.settings["on_start"][0]
    assert entry["show"] is True and entry["context"] is True
    cmd = home.fill(entry["run"])
    assert cmd == 'uv run tools/vault/status.py "vault"'
    cmd = cmd.replace("uv run ", f'"{Path(sys.executable).as_posix()}" ', 1)      # python instead of uv, same command
    done = subprocess.run([resolve_shell(str(home.settings["shell"])), "-c", cmd], cwd=root, input="{}",
                          capture_output=True, text=True, encoding="utf-8")
    assert done.returncode == 0 and done.stderr == ""
    assert done.stdout.strip() == "Vault present · 1 note · no git repo of its own"      # the README of vault/
