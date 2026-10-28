# vault

Updates the vault and prints one status line. It runs at session start (`on_start` in `lightmind.md`), so the user and the AI see at once whether the knowledge base is there.

## Call

```bash
uv run tools/vault/status.py vault
```

The argument is the vault folder (`paths.vault`). The tool runs `git pull --ff-only` there, quietly and without failing when offline, and prints one line:

```text
Vault verbunden · 368 notes · main · clean
```

This script runs as a hook, without a rule check, so every write to `tools/vault/` asks the user for a confirmation (`permissions.md`, `grant_files`).

Notes are the `*.md` files outside `.git` and `.obsidian`. The last part is `clean` or `changed` (uncommitted changes). Extra parts: `no remote` when the repo has no upstream, `pull failed (offline?)` when the pull failed. A vault folder that is not a git repo of its own prints `no git repo of its own`, a missing folder prints a warning. The exit code is always `0`. Standard library only, no dependencies.
