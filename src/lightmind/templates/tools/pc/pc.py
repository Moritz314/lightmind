# /// script
# requires-python = ">=3.11"
# dependencies = ["mss", "pyautogui", "pywinauto; sys_platform == 'win32'"]
# ///
"""PC control: screenshot, mouse, keyboard, windows.  Every action prints what it did.

    uv run pc.py [--dry-run] screenshot [--out FILE]
    uv run pc.py [--dry-run] move X Y | click [X Y] [--button left|right|middle] [--double]
    uv run pc.py [--dry-run] type TEXT | key ctrl+c | windows | focus TITLE
"""
import argparse
import os
import sys
import time
from pathlib import Path


def split_keys(spec: str) -> list[str]:
    keys = [k.strip().lower() for k in spec.split("+") if k.strip()]
    if not keys:
        raise ValueError("no key given")
    return keys


def describe(args) -> str:
    """One line saying what the action does (printed before it runs, and for --dry-run instead)."""
    c = args.cmd
    if c == "screenshot":
        return f"screenshot -> {args.out or '<run folder>/screen-<time>.png'}"
    if c == "move":
        return f"move mouse to {args.x},{args.y}"
    if c == "click":
        where = f"{args.x},{args.y}" if args.x is not None else "current position"
        return f"{'double-' if args.double else ''}click {args.button} at {where}"
    if c == "type":
        return f"type {len(args.text)} characters"
    if c == "key":
        return f"press key {'+'.join(split_keys(args.keys))}"
    if c == "windows":
        return "list windows"
    return f"focus window with title containing {args.title!r}"


def shot_path(out: str | None) -> Path:
    if out:
        return Path(out)
    d = Path(os.environ.get("LM_RESULTS") or os.environ.get("LIGHTMIND_RUN_DIR") or "out")
    return d / f"screen-{int(time.time())}.png"


def list_windows() -> list[str]:
    from pywinauto import Desktop
    return [w.window_text() for w in Desktop(backend="uia").windows() if w.window_text().strip()]


def focus_window(title: str) -> str:
    from pywinauto import Desktop
    for w in Desktop(backend="uia").windows():
        if title.lower() in w.window_text().lower():
            w.set_focus()
            return w.window_text()
    raise LookupError(f"no window with {title!r} in its title")


def run(args) -> None:
    c = args.cmd
    if c == "screenshot":
        import mss
        import mss.tools
        p = shot_path(args.out)
        p.parent.mkdir(parents=True, exist_ok=True)
        with mss.mss() as sct:
            img = sct.grab(sct.monitors[0])
            mss.tools.to_png(img.rgb, img.size, output=str(p))
        print(p.resolve())
    elif c == "windows":
        for t in list_windows():
            print(t)
    elif c == "focus":
        print(f"focused: {focus_window(args.title)}")
    else:
        import pyautogui
        pyautogui.FAILSAFE = True
        if c == "move":
            pyautogui.moveTo(args.x, args.y, duration=0.2)
        elif c == "click":
            pyautogui.click(x=args.x, y=args.y, button=args.button, clicks=2 if args.double else 1)
        elif c == "type":
            pyautogui.write(args.text, interval=0.02)
        elif c == "key":
            keys = split_keys(args.keys)
            if len(keys) > 1:
                pyautogui.hotkey(*keys)
            else:
                pyautogui.press(keys[0])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="print what would happen, do nothing")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("screenshot").add_argument("--out")
    m = sub.add_parser("move")
    m.add_argument("x", type=int)
    m.add_argument("y", type=int)
    c = sub.add_parser("click")
    c.add_argument("x", type=int, nargs="?")
    c.add_argument("y", type=int, nargs="?")
    c.add_argument("--button", choices=["left", "right", "middle"], default="left")
    c.add_argument("--double", action="store_true")
    sub.add_parser("type").add_argument("text")
    sub.add_parser("key").add_argument("keys", help="e.g. enter, ctrl+c, alt+tab")
    sub.add_parser("windows")
    sub.add_parser("focus").add_argument("title")
    args = ap.parse_args(argv)
    if args.cmd == "click" and (args.x is None) != (args.y is None):
        ap.error("click needs both X and Y, or neither")
    try:
        line = describe(args)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    if args.dry_run:
        print(f"[dry-run] {line}")
        return 0
    print(line)
    try:
        run(args)
    except Exception as e:
        print(f"failed: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
