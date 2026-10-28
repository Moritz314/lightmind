# tools

Folder for tools: scripts the AI runs to do what text alone cannot, such as reading a PDF, sending a mail or calling an API.

## Format

One folder per tool: a script and a short `README.md` that says what the tool does and how to call it.

```text
tools/<name>/
├─ README.md       what the tool does, the exact command line, what it prints
├─ <script>.py     the script (or .sh)
└─ tool.md         optional manifest, see below
```

Tools are not function calls. The AI runs them with the bash tool, exactly as the skill or the tool's `README.md` says, for example `uv run tools/<name>/<script>.py --file a.pdf`. Put longer instructions on when and how to use a tool into a skill.

Python scripts declare their dependencies inline (PEP 723). `uv run` installs them into the tree's own cache on first use, so nothing is installed system-wide:

```python
# /// script
# requires-python = ">=3.11"
# dependencies = ["pymupdf"]
# ///
```

Print the result to stdout and errors to stderr, and exit with `0` on success. Write large results to a file and print its path.

## Optional manifest tool.md

`tool.md` has YAML frontmatter. Add it when the core has to know more about the tool:

| Key | Meaning |
|---|---|
| `name`, `description` | Name and one-line purpose |
| `run` | Command, started in the tool folder, e.g. `uv run example.py` |
| `params` | Parameters with `type`, `required`; for paths `kind: read-path` or `write-path` |
| `secrets` | Secret folders the tool needs, e.g. `[mail]`. The core injects them as environment variables when they are granted |
| `category` | `none`, `send`, `money`, `delete`, `deploy` or `account`. Permission rules can ask before such a tool runs |
| `match` | Parameter that rules can match, as in `tool(send-mail:*@example.com)` |
| `timeout` | Seconds |

With `tools.as_functions: true` in `lightmind.md`, every tool that has a `tool.md` is also offered to the model as a function, and the script then gets its parameters as JSON on stdin. The default is `false`.

## Use

A tool is found at the next start; nothing is registered. Files and folders starting with `_` are ignored. `vault/` holds the vault status tool that runs at session start.

## Example

`text-stats/` is a working tool with all three files (script, `README.md`, `tool.md`): `uv run tools/text-stats/text_stats.py README.md`. `_example/` is the minimal copy template.

## Create a tool

1. Copy `_example/` to `<name>/` and rename the script.
2. Write the script. One tool does one job.
3. Fill `README.md`: purpose, the exact command line, what it prints.
4. Run it once by hand: `uv run tools/<name>/<script>.py ...`.
5. If the tool needs secrets, add a `tool.md` with `secrets:`.
