# example

Example tool: counts the lines, words and characters of a text file. Copy this folder to start a new tool.

## Call

```bash
uv run tools/_example/example.py notes.txt
uv run tools/_example/example.py notes.txt --json
```

Prints `lines words chars` on one line, or a JSON object with `--json`. Exit code `1` when the file cannot be read.

## Make it your own

- Rename the folder and the script, then change the commands above.
- Add dependencies to the `dependencies` list in the header of the script.
- Delete what you do not need. A tool does one job.
