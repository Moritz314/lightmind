# Lightmind

Lightmind is a small, Markdown-driven agent harness. An agent is tools plus instructions plus data, checked by permission rules. Everything is a file: skills, tools, agents, triggers, workflows and settings live in one folder, called "the tree". You can read, edit and version all of it with normal tools.

The Python package is `lightmind`, the command line is `lm`.

Website: https://lightmind.kreis.systems

## Status

Early project. There is no warranty and no promised support (see the license). Windows and Linux are tested; macOS is untested.

## Requirements

- Python 3.11 or newer
- [uv](https://docs.astral.sh/uv/) and git (on Windows: Git for Windows; the AI's shell is Git Bash)
- A Claude subscription, an Anthropic API key or an OpenRouter key (see Accounts)

## Install

```
uv tool install git+https://github.com/Moritz314/lightmind
```

## First steps

```
lm init ~/lightmind      # creates a tree from templates: core files plus one example each of skill, agent, tool, trigger
cd ~/lightmind
lm login claude-abo      # once; logs in with a Claude subscription in the browser, the login stays in the tree (system/claude/)
lm                       # opens the terminal UI
```

`lm` finds the tree via `--home`, `$LIGHTMIND_HOME` or `~/lightmind`.

Other useful commands:

- `lm run -a <agent> "task"` runs an agent on a task
- `lm q` opens a quick chat
- `lm resume` continues an earlier chat
- `lm check` shows a health checklist of the tree
- `lm update` adds template files that are new since the tree was made; it never overwrites your changes

## Skills

Skills are folders with a `SKILL.md` that tell the agent how to do a task. They are published in [lightmind-skills](https://github.com/Moritz314/lightmind-skills).

```
lm skill list --available                   # what can be fetched
lm skill add <name>                         # fetches it into skills/<name>/
lm skill add <name> --from <git-url|path>   # another source
```

Base skills: programming, delegation, inquisition, browser, triggers, ultracode, skill-creator.

## Folder layout of a tree

| Folder / file | Content |
|---|---|
| `skills/` | skills (`SKILL.md` plus scripts) |
| `tools/` | tools: scripts the agent runs |
| `agents/` | agent definitions |
| `triggers/` | scheduled or event-driven runs |
| `workflows/` | workflow scripts |
| `providers/` | model accounts and providers |
| `secrets/` | descriptions of secrets (values never go into Markdown) |
| `runs/` | one folder per run |
| `permissions.md` | the permission rules |
| `system/` | technical engine and logins; do not edit by hand |

## Accounts

The default is a Claude subscription (`lm login claude-abo`). For an Anthropic API key or OpenRouter, copy an example in `providers/` and run `lm secret set anthropic/api-1`. Details are in `providers/README.md` inside the tree.

## Development

```
uv sync --extra dev --extra tui
uv run pytest -q
```

Tests marked `live` and `install` are excluded by default.

The contracts between the modules of the core are described in [INTERFACES.md](INTERFACES.md).

## Help

- Bugs: [GitHub Issues](https://github.com/Moritz314/lightmind/issues)
- Questions: [GitHub Discussions](https://github.com/Moritz314/lightmind/discussions)

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md).

## License

Apache License 2.0, see [LICENSE](LICENSE). The name "Lightmind" and the logos are not licensed, see [NOTICE](NOTICE).

---

## Kurz auf Deutsch

Lightmind ist ein kleines, Markdown-basiertes Agenten-System. Ein Agent besteht aus Werkzeugen, Anweisungen und Daten und wird von Berechtigungsregeln geprüft. Alles ist eine Datei (Skills, Tools, Agenten, Trigger, Workflows, Einstellungen) in einem Ordner, dem "Tree".

Status: frühes Projekt, ohne Gewährleistung und ohne zugesagten Support. Windows und Linux sind getestet, macOS nicht.

Installation: `uv tool install git+https://github.com/Moritz314/lightmind`, dann `lm init ~/lightmind`, `cd ~/lightmind`, einmal `lm login claude-abo`, danach startet `lm` die Terminal-Oberfläche. Skills holst du mit `lm skill add <name>`.

Fehler bitte als GitHub Issue melden, Fragen in den GitHub Discussions. Lizenz: Apache 2.0; der Name "Lightmind" und die Logos sind nicht lizenziert (siehe NOTICE).
