# projects

Folder for projects: the memory and the assets of a piece of work, so every chat and run in that project starts from the same facts.

## Format

```text
projects/<name>/
├─ projectMemory.md   frontmatter: settings of the project. Text: facts and decisions
├─ projectAssets.md   the files and links that belong to the project
└─ tools.md           what the project adds to the agent's tools (same format as in agents/)
```

`projectMemory.md` frontmatter, all keys optional:

| Key | Meaning |
|---|---|
| `provider`, `model` | Account and model for chats and runs in this project |
| `repo` | Git repository of the project |
| `path` | Folder on disk where the work lives |
| `home_server` | The server (folder in `servers/`) the project runs on |

The text holds facts and decisions: short, dated, and only what stays true. Put what changes every day into the work itself.

`projectAssets.md` is a list under `# Assets`, one row per item: `- path or URL | note`.

`tools.md` has the sections `# Skills`, `# Tools`, `# Secrets`, `# Servers` as described in `agents/README.md`. Listing a secret or a server grants it while the project is active, so writing this `tools.md` asks the user for a confirmation. A project gets grants from this file only: a `permissions:` block in `projectMemory.md` is ignored, because that file is not protected.

## Use

Memory and assets are loaded into the context of every chat and run in the project, after the master prompt and the agent's instruction. The project is chosen when a chat or run starts.

## Create a project

1. Copy `_example/` to `<name>/`.
2. Write what an agent must know to start: goal, customer, decisions made, where the work lives.
3. List the assets and the tools the project needs.

Files and folders starting with `_` are ignored.
