# browser

Playwright browser: read a page as markdown or text, take a screenshot, click and fill with a small JSON step list. Logins stay: every `--profile NAME` is a persistent profile in `system/browsers/profiles/NAME`.

## Call

```bash
uv run tools/browser/browser.py open https://example.com
uv run tools/browser/browser.py open URL --format text --profile shop --headed
uv run tools/browser/browser.py screenshot URL --full --out shot.png
uv run tools/browser/browser.py open URL --steps '[{"fill":["#user","me"]},{"click":"button[type=submit]"},{"wait":1000}]'
```

- `open` prints `# <title>`, the final URL and the page as markdown (`--format md`, default) or plain text.
- `screenshot` prints the path of the PNG (default `shot-<time>.png` in `$LM_RESULTS`, else `$LIGHTMIND_RUN_DIR`, else `out/`).
- `--steps` is a JSON list (or `@file.json`), run in order after the page loaded. One action per step: `{"goto": url}`, `{"click": selector}`, `{"fill": [selector, text]}`, `{"press": "Enter"}`, `{"wait": ms-or-selector}`, `{"scroll": pixels}`. A bad list exits `2` before a browser starts.
- Headless by default, `--headed` shows the window (for a first login by hand).

First use installs chromium into `PLAYWRIGHT_BROWSERS_PATH` (`playwright install chromium`), unless Chrome is installed: then it is used (`channel="chrome"`). The first call needs network and takes a minute. Exit code `0` on success, `1` on a browser or page error.

Secrets (passwords) never go into the step list in plain text; pass them as environment variables the rule grants and build the JSON with them in the shell.
