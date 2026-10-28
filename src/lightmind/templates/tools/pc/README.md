# pc

Controls the PC: screenshot of the whole screen, mouse, keyboard, list and focus windows (Windows only for windows). Every action prints what it did; `--dry-run` prints it and does nothing.

## Call

```bash
uv run tools/pc/pc.py screenshot --out screen.png     # prints the PNG path
uv run tools/pc/pc.py --dry-run click 640 400         # prints "[dry-run] click left at 640,400"
uv run tools/pc/pc.py move 640 400
uv run tools/pc/pc.py click 640 400 --button right --double
uv run tools/pc/pc.py type "hello"
uv run tools/pc/pc.py key ctrl+s                      # single key (enter) or combination (alt+tab)
uv run tools/pc/pc.py windows                         # titles of the open windows
uv run tools/pc/pc.py focus "Notepad"                 # brings the first window with that title part to the front
```

Screenshots without `--out` go to `$LM_RESULTS` (else `$LIGHTMIND_RUN_DIR`, else `out/`) as `screen-<time>.png`. Moving the mouse into a screen corner aborts a running action (pyautogui fail-safe). Exit code `0` on success, `1` when an action failed, `2` on bad input.

This tool acts on the user's real desktop. Use it only with their consent; for anything on the web use the browser tool.
