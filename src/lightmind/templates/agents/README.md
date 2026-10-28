# agents

Folder for agents: a role with its own instructions, tools and limits. An agent is started for a task and works until the task is done.

## Format

One folder per agent, named like the agent.

```text
agents/<name>/
├─ agentMaster.md   settings in the frontmatter, role and working rules as the text
└─ tools.md         what the agent may use
```

`agentMaster.md` frontmatter, all keys optional except `description`:

| Key | Meaning |
|---|---|
| `name` | The folder name |
| `description` | One line: what the agent is for |
| `provider` | A file name from `providers/` or an alias. Empty means `defaults.provider` |
| `model` | A model of that provider or a tier (`strong`, `medium`, `cheap`). Empty means `defaults.model`. A fixed model stays fixed when the provider switches |
| `effort` | `low`, `medium`, `high`, `max` |
| `context_limit` | `100k`, `200k`, `1m` |
| `budget` | `tokens` and optionally `usd`, per run |
| `lifetime` | `task` (ends with the task) or `resident` (keeps running) |
| `mode` | A mode from `permissions.md`. Empty means `defaults.mode` |
| `project` | A folder in `projects/`: the agent's default project. A chat or run with this agent loads that project (its memory and assets) when none is chosen; `-p` wins. It grants nothing: rights stay in `tools.md` and `permissions`. `lm lint` checks that the project exists |
| `may_spawn` | Agents this agent may start with `spawn`: `[a, b]` those, `["*"]` all. Not set: only the plain helper (`spawn` without `agent`: a copy of itself that cannot spawn). `[]`: no `spawn` at all. A sub-agent always runs with the rights and mode of the chat that started it, never looser, whatever mode or permissions its own file has |
| `data` | Extra files loaded into its context |
| `permissions` | Extra rules for this agent as `allow`, `ask` and `deny` lists |

The text below the frontmatter is the agent's instruction: role, tone, working rules, limits, when it is done.

`tools.md` lists what the agent can use: one `# Section` per kind, one `- name` row per entry.

| Section | Entries | Effect |
|---|---|---|
| `# Skills` | folders in `skills/` | They appear in the agent's skill index. Without a `tools.md` the agent sees all skills |
| `# Tools` | folders in `tools/`, built-ins such as `bash`, `read`, `write` | Listed for the agent |
| `# Secrets` | `service/account`, a folder in `secrets/` and an account | Grants the secret: the core may inject it into the agent's tools |
| `# Servers` | folders in `servers/` | Grants ssh access to that server |

Listing a secret or a server is the grant. Nothing else is needed. Because these files hold grants, every write to `agentMaster.md` or `tools.md` asks the user for a confirmation, in every mode. A malformed secret or server entry (a glob, a path, a stray character) is skipped and grants nothing.

## Use

An agent is started by name, by a trigger, by a workflow or by another agent (`spawn`). It gets the master prompt, its mode, its instruction and its tools. The rules of `permissions.md` apply to everything it does.

## Example

`example-agent/` is a working agent: it reads and counts with the skill `text-stats` and changes nothing. Try it with `lm run -a example-agent "What is in skills/?"`. `_example/` lists every key and is the copy template; `_example-lead/` shows a lead that splits a job over worker agents.

## Create an agent

1. Copy `_example/` to `<name>/`.
2. Fill `agentMaster.md`: description, limits, instruction.
3. List the skills, tools, secrets and servers it needs in `tools.md`. Grant as little as possible.
4. Test it with a small task before you give it real work.

Files and folders starting with `_` are ignored.
