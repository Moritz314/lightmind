---
name: text-stats
description: Count lines, words and characters of text files or of the Markdown files in a folder, with the example tool tools/text-stats. Use for "how long is", "count the words", "how many lines", "text statistics". Example skill of a new tree.
---

# text-stats

This is the example skill of a new tree. It shows the format: frontmatter with `name` and `description` (the skill index shows only the description, so it names the trigger words), then short steps for the AI. The format is explained in `skills/README.md`.

1. Collect the files or folders the user named. A folder counts its `*.md` files, not deeper.
2. Run `uv run tools/text-stats/text_stats.py <files or folders>` from the tree root. Add `--json` when you need the numbers for further work.
3. Exit code `1` means a file could not be read: name it and go on with the rest. Exit code `2` means nothing was found: check the paths.
4. Answer with the numbers in one or two sentences, or the table when there are several files. When a run task asks for a file, write the table to `$LM_RESULTS/text-stats.md` and give its path.
