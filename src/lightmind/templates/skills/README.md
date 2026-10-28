# skills

Folder for skills: instructions the AI reads when a task needs them. A skill can bring scripts and reference files.

## Format

One folder per skill, in the Claude Code skill format. Skills from the internet or from Claude Code can be copied in unchanged.

```text
skills/<name>/
├─ SKILL.md        required: frontmatter and instructions
├─ scripts/        optional: scripts the skill tells the AI to run
└─ references/     optional: long material, read only when needed
```

`SKILL.md` frontmatter:

| Key | Meaning |
|---|---|
| `name` | The folder name: lowercase letters, digits and `-` |
| `description` | One or two sentences: what the skill does and when to use it. The skill index shows only this text, so name the triggers |
| `preload_when` | Optional regex. Models without tool use get the skill inlined when the task matches |
| `requires` | Optional list of other skills this one needs |

A single `<name>.md` file in this folder works as a skill too.

## Use

The AI sees one line per skill in its context: name, description, path. It reads the whole `SKILL.md` before it uses the skill and follows it. A skill is found at the next start; nothing is registered. Files and folders starting with `_` are ignored.

## Example

`text-stats/` is a working skill: it tells the AI when and how to run the example tool `tools/text-stats/`. More skills: `lm skill list --available` shows the skills of the Lightmind skills repository, `lm skill add <name>` copies one into this folder.

## Create a skill

1. Copy `_example/` to `<name>/`.
2. Set `name` and a `description` that says when to use the skill.
3. Write the steps as short commands to the AI. Keep `SKILL.md` short and move long material to `references/`.
4. If the skill runs a script, put it in `scripts/`. Python scripts declare their dependencies inline (PEP 723) and run with `uv run`.
