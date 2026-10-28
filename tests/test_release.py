"""Version and codename: the product is Lightmind, the series 0.1.x is `orbit`, and 0.1.n shows as `orbit vn`."""
import tomllib
from pathlib import Path

import pytest

import lightmind
from lightmind import checks, cli
from lightmind.tui.boot import BootScreen
from lightmind.tui.screens import MapScreen

from test_boot import L, boot_text, fake_groups
from test_tui_support import plain, running


@pytest.mark.parametrize("version, name", [
    ("0.1.1", "orbit v1"), ("0.1.7", "orbit v7"), ("0.1.0", "orbit v0"), ("0.1.12", "orbit v12"), ("0.1.1rc1", "orbit v1"),
    ("0.2.0", ""), ("0.10.0", ""), ("1.1.1", ""), ("0.1", ""), ("", ""), ("orbit", ""),
])
def test_the_codename_of_a_version(version, name):
    assert lightmind.codename(version) == name


def test_title_and_version_text():
    assert lightmind.title("0.1.1") == "Lightmind 0.1.1 · orbit v1"
    assert lightmind.title("0.1.7") == "Lightmind 0.1.7 · orbit v7"
    assert lightmind.title("0.2.0") == "Lightmind 0.2.0"
    assert lightmind.version_text("0.1.1") == "lm 0.1.1 (orbit v1)"
    assert lightmind.version_text("0.2.0") == "lm 0.2.0"


def test_this_release_is_0_1_2_orbit_v2_and_pyproject_says_the_same():
    assert lightmind.__version__ == "0.1.3" and lightmind.codename() == "orbit v3"
    root = Path(lightmind.__file__).resolve().parents[2]
    assert tomllib.loads((root / "pyproject.toml").read_text("utf-8"))["project"]["version"] == lightmind.__version__


def test_lm_version_prints_the_codename(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["--version"])
    assert e.value.code == 0 and capsys.readouterr().out.strip() == "lm 0.1.3 (orbit v3)"


@pytest.mark.parametrize("version, shown", [("0.1.7", "lm 0.1.7 (orbit v7)"), ("0.2.0", "lm 0.2.0")])
def test_lm_version_follows_the_version(capsys, monkeypatch, version, shown):
    monkeypatch.setattr(lightmind, "__version__", version)
    with pytest.raises(SystemExit):
        cli.main(["--version"])
    assert capsys.readouterr().out.strip() == shown


def test_the_runtime_line_of_lm_check_shows_the_codename(home, monkeypatch):
    line = checks.check_runtime(home)[0]
    assert line.label == "runtime" and line.detail.startswith("lightmind 0.1.3 · orbit v3 · python ")
    monkeypatch.setattr(lightmind, "__version__", "0.2.0")
    monkeypatch.setattr(checks, "__version__", "0.2.0")
    detail = checks.check_runtime(home)[0].detail
    assert detail.startswith("lightmind 0.2.0 · python ") and "orbit" not in detail


async def test_the_head_of_the_boot_checklist_and_the_foot_of_the_start_screen(home, monkeypatch):
    monkeypatch.delenv("LM_NO_BOOT")
    fake_groups(monkeypatch, [("a", 0.0, [L("ok", "alpha", "fine")])])
    async with running(home, anim=False) as (app, pilot):
        assert isinstance(app.screen, BootScreen)
        assert boot_text(app).startswith("Lightmind 0.1.3 · orbit v3  ·  checking the tree")
    monkeypatch.setenv("LM_NO_BOOT", "1")
    async with running(home, anim=False) as (app, pilot):
        assert isinstance(app.screen, MapScreen)
        assert plain(app, "#foot").splitlines()[-1].strip() == "Lightmind 0.1.3 · orbit v3"
