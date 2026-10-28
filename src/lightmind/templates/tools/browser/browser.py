# /// script
# requires-python = ">=3.11"
# dependencies = ["playwright>=1.45"]
# ///
"""Browser tool (Playwright): read a page as text/markdown, take a screenshot, click and fill.

    uv run browser.py open URL [--format text|md] [--steps JSON|@file] [--profile NAME] [--headed]
    uv run browser.py screenshot URL [--out FILE] [--full] [--steps JSON|@file] [--profile NAME]
"""
import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ACTIONS = ("goto", "click", "fill", "press", "wait", "scroll")

MD_JS = """() => {
  const out = [];
  const walk = (n) => {
    if (n.nodeType === 3) { out.push(n.textContent.replace(/\\s+/g, ' ')); return; }
    if (n.nodeType !== 1) return;
    const t = n.tagName.toLowerCase();
    if (['script', 'style', 'noscript', 'svg', 'head'].includes(t)) return;
    if (/^h[1-6]$/.test(t)) { out.push('\\n\\n' + '#'.repeat(+t[1]) + ' '); n.childNodes.forEach(walk); out.push('\\n\\n'); return; }
    if (t === 'a' && n.href) { out.push('['); n.childNodes.forEach(walk); out.push('](' + n.href + ')'); return; }
    if (t === 'li') { out.push('\\n- '); n.childNodes.forEach(walk); return; }
    if (['p', 'div', 'section', 'tr', 'br', 'ul', 'ol'].includes(t)) out.push('\\n');
    n.childNodes.forEach(walk);
    if (['p', 'div', 'section'].includes(t)) out.push('\\n');
  };
  walk(document.body);
  return out.join('').replace(/[ \\t]+\\n/g, '\\n').replace(/\\n{3,}/g, '\\n\\n').trim();
}"""


def parse_steps(text: str | None) -> list[tuple[str, tuple]]:
    """Parse a JSON step list into [(action, args)].  `text` may be JSON or `@file`.

    Steps: {"goto": url} {"click": sel} {"fill": [sel, text]} or {"fill": {"selector": s, "text": t}}
    {"press": key} {"wait": ms | selector} {"scroll": pixels}.  Raises ValueError on bad input."""
    if not text:
        return []
    if text.startswith("@"):
        text = Path(text[1:]).read_text(encoding="utf-8")
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        raise ValueError(f"steps are not valid JSON: {e}") from e
    if not isinstance(raw, list):
        raise ValueError("steps must be a JSON list")
    steps = []
    for i, st in enumerate(raw):
        if not isinstance(st, dict) or len(st) != 1:
            raise ValueError(f"step {i}: expected an object with exactly one action, got {st!r}")
        (act, arg), = st.items()
        if act not in ACTIONS:
            raise ValueError(f"step {i}: unknown action {act!r} (known: {', '.join(ACTIONS)})")
        if act == "fill":
            if isinstance(arg, dict):
                arg = [arg.get("selector"), arg.get("text")]
            if not (isinstance(arg, list) and len(arg) == 2 and all(isinstance(a, str) for a in arg)):
                raise ValueError(f"step {i}: fill needs [selector, text]")
            steps.append((act, (arg[0], arg[1])))
        elif act in ("wait", "scroll"):
            if not isinstance(arg, (int, float, str)) or isinstance(arg, bool):
                raise ValueError(f"step {i}: {act} needs a number or a selector")
            steps.append((act, (arg,)))
        else:
            if not isinstance(arg, str):
                raise ValueError(f"step {i}: {act} needs a string")
            steps.append((act, (arg,)))
    return steps


def profile_dir(name: str, browsers: str | None = None) -> Path:
    """system/browsers/profiles/<name> (PLAYWRIGHT_BROWSERS_PATH is system/browsers)."""
    if not name or any(c in name for c in "/\\:") or name.startswith("."):
        raise ValueError(f"bad profile name {name!r}")
    base = Path(browsers or os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or Path.home() / ".lightmind-browsers")
    return base / "profiles" / name


def out_dir() -> Path:
    d = Path(os.environ.get("LM_RESULTS") or os.environ.get("LIGHTMIND_RUN_DIR") or "out")
    d.mkdir(parents=True, exist_ok=True)
    return d


def apply_steps(page, steps) -> None:
    for act, args in steps:
        print(f"step: {act} {' '.join(str(a) for a in args)}", file=sys.stderr)
        if act == "goto":
            page.goto(args[0], wait_until="domcontentloaded")
        elif act == "click":
            page.click(args[0], timeout=15000)
        elif act == "fill":
            page.fill(args[0], args[1], timeout=15000)
        elif act == "press":
            page.keyboard.press(args[0])
        elif act == "scroll":
            page.mouse.wheel(0, int(args[0]))
        elif act == "wait":
            if isinstance(args[0], (int, float)):
                page.wait_for_timeout(args[0])
            else:
                page.wait_for_selector(args[0], timeout=30000)
        page.wait_for_timeout(300)


def launch(pw, profile: Path, headed: bool):
    """Persistent context: installed Chrome when present, else bundled chromium (installed on first use)."""
    profile.mkdir(parents=True, exist_ok=True)
    kw = dict(headless=not headed, viewport={"width": 1280, "height": 900})
    try:
        return pw.chromium.launch_persistent_context(str(profile), channel="chrome", **kw)
    except Exception:
        pass
    try:
        return pw.chromium.launch_persistent_context(str(profile), **kw)
    except Exception as e:
        if "xecutable" not in str(e):
            raise
    print("installing chromium (first use) ...", file=sys.stderr)
    subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)
    return pw.chromium.launch_persistent_context(str(profile), **kw)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("open", "screenshot"):
        s = sub.add_parser(name)
        s.add_argument("url")
        s.add_argument("--steps", help="JSON step list or @file, run after the page loaded")
        s.add_argument("--profile", default="default", help="persistent profile name (keeps logins)")
        s.add_argument("--headed", action="store_true", help="show the window")
        if name == "open":
            s.add_argument("--format", choices=["text", "md"], default="md")
        else:
            s.add_argument("--out", help="PNG path (default: run folder)")
            s.add_argument("--full", action="store_true", help="full page")
    args = ap.parse_args(argv)
    try:
        steps = parse_steps(args.steps)
        prof = profile_dir(args.profile)
    except (ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        ctx = launch(pw, prof, args.headed)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(args.url, wait_until="domcontentloaded", timeout=45000)
            apply_steps(page, steps)
            if args.cmd == "open":
                print(f"# {page.title()}\n{page.url}\n")
                print(page.evaluate(MD_JS) if args.format == "md" else page.inner_text("body"))
            else:
                path = Path(args.out) if args.out else out_dir() / f"shot-{int(time.time())}.png"
                path.parent.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(path), full_page=args.full)
                print(path.resolve())
        finally:
            ctx.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
