"""`lm skill add` and `lm skill list`: fetching skills from a skills repository (src/lightmind/skillrepo.py), offline."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from lightmind import cli, skillrepo
from lightmind.files import Home

GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]


def make_source(root: Path, index: bool = False) -> Path:
    """A skills repository as a plain folder: skills/hello (with a script) and skills/other."""
    (root / "skills/hello/scripts").mkdir(parents=True)
    (root / "skills/hello/SKILL.md").write_text("---\nname: hello\ndescription: Says hello.\n---\nBody\n", encoding="utf-8")
    (root / "skills/hello/scripts/x.py").write_text("print('x')\n", encoding="utf-8")
    (root / "skills/other").mkdir()
    (root / "skills/other/SKILL.md").write_text("---\nname: other\ndescription: Another one.\n---\nBody\n", encoding="utf-8")
    if index:
        entries = [{"name": "hello", "description": "From the index.", "path": "skills/hello", "files": ["SKILL.md"]},
                   {"name": "other", "description": "Other, indexed.", "path": "skills/other", "files": ["SKILL.md"]},
                   {"name": "../evil", "description": "x", "path": "skills/../evil"},      # bad name
                   {"name": "sneaky", "description": "x", "path": "skills/hello"},         # path is not skills/<name>
                   {"name": "Upper", "description": "x", "path": "skills/Upper"}]
        (root / "index.json").write_text(json.dumps({"format": 1, "name": "t", "skills": entries}), encoding="utf-8")
    return root


def git_repo(src: Path) -> str:
    subprocess.run(["git", "init", "-q", str(src)], check=True)
    subprocess.run([*GIT, "-C", str(src), "add", "-A"], check=True)
    subprocess.run([*GIT, "-C", str(src), "commit", "-q", "-m", "init"], check=True)
    return subprocess.run(["git", "-C", str(src), "rev-parse", "HEAD"], capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def src(tmp_path) -> Path:
    return make_source(tmp_path / "src")


def run(tree, capsys, *args):
    capsys.readouterr()
    code = cli.main(["--home", str(tree), *args])
    out, err = capsys.readouterr()
    return code, out, err


def record(tree) -> dict:
    return json.loads((Home.open(tree).path("state") / "skills.json").read_text(encoding="utf-8"))


def set_source(tree: Path, value: str) -> None:
    cfg = tree / "config.md"
    text = cfg.read_text(encoding="utf-8")
    new = re.sub(r"skills:\n  source:[^\n]*", lambda m: f"skills:\n  source: {value}", text, count=1)
    assert new != text
    cfg.write_text(new, encoding="utf-8")


# ------------------------------------------------------------------ add
def test_add_from_a_folder_copies_the_whole_skill(tree, capsys, src):
    code, out, err = run(tree, capsys, "skill", "add", "hello", "--from", str(src))
    assert code == 0, err
    assert (tree / "skills/hello/SKILL.md").read_text(encoding="utf-8").startswith("---\nname: hello")
    assert (tree / "skills/hello/scripts/x.py").is_file()
    rec = record(tree)
    assert rec["hello"]["source"] == str(src) and rec["hello"]["commit"] == "" and rec["hello"]["installed"]
    assert not (tree / "skills/other").exists()                       # counter-check: only the asked skill was copied


def test_add_from_a_git_repo_records_the_commit(tree, capsys, src):
    commit = git_repo(src)
    code, out, err = run(tree, capsys, "skill", "add", "hello", "--from", "file://" + str(src))
    assert code == 0, err
    assert record(tree)["hello"]["commit"] == commit
    assert (tree / "skills/hello/scripts/x.py").is_file()
    assert commit[:10] in out


def test_the_added_skill_appears_in_the_core_skill_list(tree, capsys, src):
    assert "hello" not in [s.name for s in Home.open(tree).list_skills()]          # counter-check
    run(tree, capsys, "skill", "add", "hello", "--from", str(src))
    skills = {s.name: s for s in Home.open(tree).list_skills()}
    assert skills["hello"].description == "Says hello."


def test_the_added_skill_passes_check(tree, capsys, src):
    run(tree, capsys, "skill", "add", "hello", "--from", str(src))
    code, out, err = run(tree, capsys, "--json", "check")
    rows = json.loads(out)
    assert not [r for r in rows if r["status"] == "error" and "hello" in r["detail"] + r["label"]]


# ------------------------------------------------------------------ list
def test_list_shows_tree_skills_with_their_source(tree, capsys, src):
    run(tree, capsys, "skill", "add", "hello", "--from", str(src))
    code, out, _ = run(tree, capsys, "--json", "skill", "list")
    rows = {r["name"]: r for r in json.loads(out)}
    assert rows["hello"]["source"] == str(src) and rows["hello"]["description"] == "Says hello."
    assert rows["text-stats"]["source"] == ""                         # shipped with the tree, not installed
    code, text, _ = run(tree, capsys, "skill", "list")
    assert "hello" in text and str(src) in text


def test_list_available_scans_a_source_without_index(tree, capsys, src):
    code, out, err = run(tree, capsys, "--json", "skill", "list", "--available", "--from", str(src))
    assert code == 0, err
    assert {(r["name"], r["description"]) for r in json.loads(out)} == {("hello", "Says hello."), ("other", "Another one.")}
    assert "hello" not in run(tree, capsys, "--json", "skill", "list")[1]       # counter-check: listing installs nothing


def test_list_available_uses_index_json_and_drops_unsafe_entries(tree, capsys, tmp_path):
    s = make_source(tmp_path / "idx", index=True)
    code, out, err = run(tree, capsys, "--json", "skill", "list", "--available", "--from", str(s))
    assert code == 0, err
    assert {r["name"]: r["description"] for r in json.loads(out)} == {"hello": "From the index.", "other": "Other, indexed."}


def test_a_broken_index_falls_back_to_the_scan(tree, capsys, src):
    (src / "index.json").write_text("{not json", encoding="utf-8")
    code, out, _ = run(tree, capsys, "--json", "skill", "list", "--available", "--from", str(src))
    assert {r["name"] for r in json.loads(out)} == {"hello", "other"}


def test_list_available_from_a_git_repo(tree, capsys, tmp_path):
    s = make_source(tmp_path / "g", index=True)
    git_repo(s)
    code, out, err = run(tree, capsys, "--json", "skill", "list", "--available", "--from", "file://" + str(s))
    assert code == 0, err
    assert {r["name"] for r in json.loads(out)} == {"hello", "other"}


# ------------------------------------------------------------------ the setting
def test_config_source_is_used_without_from_and_from_beats_it(tree, capsys, src, tmp_path):
    other = make_source(tmp_path / "other-src")
    (other / "skills/other").rename(other / "skills/only-here")
    set_source(tree, str(src))
    code, out, _ = run(tree, capsys, "--json", "skill", "list", "--available")
    assert {r["name"] for r in json.loads(out)} == {"hello", "other"}                    # from the setting
    code, out, _ = run(tree, capsys, "--json", "skill", "list", "--available", "--from", str(other))
    assert {r["name"] for r in json.loads(out)} == {"hello", "only-here"}                # --from wins
    assert run(tree, capsys, "skill", "add", "hello")[0] == 0 and (tree / "skills/hello/SKILL.md").is_file()


def test_the_default_source_is_the_public_repository_when_nothing_is_set(tree):
    assert skillrepo.default_source(Home.open(tree)) == skillrepo.DEFAULT_SOURCE == "https://github.com/Moritz314/lightmind-skills"
    assert skillrepo.default_source(Home.open(tree), "/x") == "/x"                      # counter-check


# ------------------------------------------------------------------ refusals
def test_unknown_skill_names_what_the_source_offers(tree, capsys, src):
    code, out, err = run(tree, capsys, "skill", "add", "nope", "--from", str(src))
    assert code == 1 and "hello" in err and "other" in err and "nope" in err
    assert not (tree / "skills/nope").exists()


@pytest.mark.parametrize("bad", ["../x", "A", "a b", "x_y", "x" * 65])
def test_bad_names_are_refused_and_nothing_is_written(tree, capsys, src, bad):
    code, out, err = run(tree, capsys, "skill", "add", bad, "--from", str(src))
    assert code in (1, 2)
    assert not any(p.name.startswith(".skill-add") for p in tree.iterdir())
    assert not (tree.parent / "x").exists()


def test_a_good_name_passes_the_rule():
    assert skillrepo.NAME_RE.fullmatch("hello-2")                                       # counter-check of the refusals above


def test_a_missing_or_empty_name_is_refused(tree, capsys, src):
    assert run(tree, capsys, "skill", "add", "--from", str(src))[0] == 2
    assert run(tree, capsys, "skill", "add", "", "--from", str(src))[0] in (1, 2)


def test_a_skill_without_skill_md_is_refused(tree, capsys, src):
    (src / "skills/hello/SKILL.md").unlink()
    code, out, err = run(tree, capsys, "skill", "add", "hello", "--from", str(src))
    assert code == 1 and "SKILL.md" in err and not (tree / "skills/hello").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need rights on Windows")
def test_a_symlink_in_the_source_is_refused_and_leaves_nothing(tree, capsys, src):
    os.symlink("/etc/passwd", src / "skills/hello/scripts/link")
    code, out, err = run(tree, capsys, "skill", "add", "hello", "--from", str(src))
    assert code == 1 and "symlink" in err
    assert not (tree / "skills/hello").exists() and not any(p.name.startswith(".skill-add") for p in tree.iterdir())
    assert run(tree, capsys, "skill", "add", "other", "--from", str(src))[0] == 0        # counter-check: a clean skill works


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need rights on Windows")
def test_a_skill_folder_that_is_a_symlink_out_of_skills_is_refused(tree, capsys, src, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text("---\nname: evil\n---\n", encoding="utf-8")
    os.symlink(outside, src / "skills/evil")
    code, out, err = run(tree, capsys, "skill", "add", "evil", "--from", str(src))
    assert code == 1 and not (tree / "skills/evil").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need rights on Windows")
def test_a_repository_whose_skills_folder_is_a_symlink_is_refused(tree, capsys, tmp_path):
    outside = make_source(tmp_path / "outside")                # a folder of this machine the repo points at
    repo = tmp_path / "repo"
    repo.mkdir()
    os.symlink(outside / "skills", repo / "skills")
    (repo / "README.md").write_text("x\n", encoding="utf-8")
    git_repo(repo)
    code, out, err = run(tree, capsys, "skill", "add", "hello", "--from", repo.as_uri())
    assert code == 1 and "symlink" in err and not (tree / "skills/hello").exists()
    assert run(tree, capsys, "skill", "add", "hello", "--from", str(outside))[0] == 0     # counter-check: the real folder works


def test_a_too_big_skill_is_refused_with_the_limit_in_the_message(tree, capsys, src, monkeypatch):
    monkeypatch.setattr(skillrepo, "MAX_FILES", 1)
    code, out, err = run(tree, capsys, "skill", "add", "hello", "--from", str(src))
    assert code == 1 and "too big" in err and "1 files" in err and not (tree / "skills/hello").exists()
    assert run(tree, capsys, "skill", "add", "other", "--from", str(src))[0] == 0        # counter-check: one file is within the limit


def test_a_broken_source_is_one_clear_line(tree, capsys, tmp_path):
    code, out, err = run(tree, capsys, "skill", "add", "hello", "--from", str(tmp_path / "does-not-exist"))
    assert code == 1 and err.count("\n") == 1 and "failed" in err


def test_an_option_is_no_source(tree, capsys):
    code, out, err = run(tree, capsys, "skill", "add", "hello", "--from=--upload-pack=x")
    assert code == 1 and "not a folder" in err


# ------------------------------------------------------------------ existing folder, --force
def test_an_existing_skill_is_refused_without_force(tree, capsys, src):
    run(tree, capsys, "skill", "add", "hello", "--from", str(src))
    (tree / "skills/hello/mine.txt").write_text("keep me", encoding="utf-8")
    code, out, err = run(tree, capsys, "skill", "add", "hello", "--from", str(src))
    assert code == 1 and "--force" in err
    assert (tree / "skills/hello/mine.txt").is_file()                                      # untouched


def test_force_moves_the_old_folder_to_the_state_backups(tree, capsys, src):
    run(tree, capsys, "skill", "add", "hello", "--from", str(src))
    (tree / "skills/hello/mine.txt").write_text("keep me", encoding="utf-8")
    code, out, err = run(tree, capsys, "skill", "add", "hello", "--from", str(src), "--force")
    assert code == 0, err
    assert not (tree / "skills/hello/mine.txt").exists()
    backups = list((Home.open(tree).path("state") / "skill-backups").glob("hello-*"))
    assert len(backups) == 1 and (backups[0] / "mine.txt").read_text(encoding="utf-8") == "keep me"
    assert str(backups[0]) in out
    assert [s.name for s in Home.open(tree).list_skills()].count("hello") == 1               # the backup is not a skill


def test_json_output_of_add(tree, capsys, src):
    code, out, _ = run(tree, capsys, "--json", "skill", "add", "hello", "--from", str(src))
    d = json.loads(out)
    assert code == 0 and d["name"] == "hello" and d["source"] == str(src) and d["backup"] == ""


def test_the_key_is_known_and_check_does_not_flag_the_template(tree, capsys):
    from lightmind.files import CONFIG_KEYS
    assert CONFIG_KEYS["skills"] == ("source",)
    code, out, _ = run(tree, capsys, "--json", "check")
    assert not [r for r in json.loads(out) if "skills" in r["detail"] and "config" in r["detail"]]
