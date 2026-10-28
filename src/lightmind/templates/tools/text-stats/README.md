# text-stats

The example tool of a new tree: counts lines, words and characters of text files. It shows the format every tool in `tools/` follows: one folder, one script with its dependencies inline (PEP 723), this `README.md`, and an optional `tool.md`. The skill `skills/text-stats/` tells the AI when to use it.

## Call

```bash
uv run tools/text-stats/text_stats.py README.md notes.txt
uv run tools/text-stats/text_stats.py skills            # a folder: its *.md files, not deeper
uv run tools/text-stats/text_stats.py README.md --json
```

## Output

A table with one row per file (`file lines words chars`) and a `total` row when there are several files. With `--json` a list of objects `{"file", "lines", "words", "chars"}`; a file that cannot be read has `"error"` instead.

Exit code `0` when every file was read, `1` when one could not be read, `2` when no file was found.

## Files

| File | Purpose |
|---|---|
| `text_stats.py` | The script. The block `# /// script` at the top lists its Python version and dependencies (none here); `uv run` installs them into the tree's cache on first use |
| `tool.md` | The manifest: name, description, how the core runs it, its category. Needed only when the core has to know more about the tool, see `tools/README.md` |
| `README.md` | This file: what the tool does, the exact command line, what it prints |

To build your own tool, copy `tools/_example/` (or this folder) to `tools/<name>/` and follow `tools/README.md`.
