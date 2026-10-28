"""Browser tool against a real browser (needs playwright + chrome/chromium): run with -m live."""
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "src" / "lightmind" / "templates" / "tools" / "browser" / "browser.py"
PAGE = "data:text/html,<title>T</title><h1>Hello</h1><input id=a><button onclick=\"document.title='clicked'\">go</button>"


@pytest.mark.live
def test_browser_open_and_screenshot(tmp_path, monkeypatch):
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(tmp_path / "browsers"))
    r = subprocess.run([sys.executable, str(SCRIPT), "open", PAGE, "--steps", '[{"click":"button"}]'],
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0 and "# Hello" in r.stdout and "# clicked" in r.stdout
    out = tmp_path / "s.png"
    r = subprocess.run([sys.executable, str(SCRIPT), "screenshot", PAGE, "--out", str(out)],
                       capture_output=True, text=True, timeout=300)
    assert r.returncode == 0 and out.stat().st_size > 100
