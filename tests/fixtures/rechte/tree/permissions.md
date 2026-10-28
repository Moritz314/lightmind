---
# Enforced by the core; the AI sees only the mode text. Layers, rule syntax and settings: see the text below.
fallback: allow
deny:
  - secret(*)                        # usable only where a layer grants it
  - secret-write(*)
  - read({system}/**)                # the engine; a plain deny, so that the agent chat-bot can lift it
  - write({system}/**)
  - edit({system}/**)
locked:
  deny:                              # the fixed limits of rechte.md: no mode, agent or session lifts them
    - read({secrets}/**/*.env)
    - write({secrets}/**/*.env)
    - edit({secrets}/**/*.env)
    - bash(*{secrets}/*.env*)        # catches the obvious ways, not every way
    - bash(*{claude}*)
    - bash(*vaultwarden*)
    - bash(*schluesseldienst*)
# Files that grant access (number 13 of rechte.md). Why each one: "Files that hold grants" below.
grant_files: &grant_files
  - write({permissions})
  - write({entry})
  - write({config})
  - write({modes}/**)
  - write({rights})
  - write({agents}/*/{file_agent})
  - write({agents}/*/{file_agent_tools})
  - write({workflows}/*/{file_agent})
  - write({workflows}/*/{file_agent_tools})
  - write({projects}/*/{file_project_tools})
  - write({providers}/*.md)
  - write({tools}/vault/**)          # hook scripts run without a rule check
  - write({tools}/watch/**)
  - write({triggers}/**)
  - write({runs}/*/events.jsonl)
  - write({runs}/*/meta.json)
  - write({runs}/*/session.md)
# Read only by cores older than modes/ (the same three modes); delete once every machine ran `lm update`.
modes:
  auto:
    label: Auto
    color: green
    allow: [secret(crm/api-1)]
    ask: [category(money), category(send)]
    locked:
      ask: *grant_files
    text: Work autonomously on the task. Ask the user before you spend money or send anything to customers.
  plan:
    label: Plan
    color: blue
    ask: [bash(*)]
    locked:
      deny: [write(**), edit(**), category(send), category(money), category(delete), category(deploy), category(account), pc-control(*)]
    text: Research and plan. Change nothing. End with the plan as your answer and wait until the user says go.
  bypass:
    label: Bypass
    color: red
    allow: [secret(crm/api-1)]
    locked:
      deny:
        - write({config})
        - edit({config})
    text: Do not ask questions. Decide and act. Secrets work only where the session or the agent grants them.
audit: { secret_use: on, secret_write: on, pc: on, browser: on, send: on, money: on, delete: on, deploy: on, screenshots: thumb }
money_limit: 20 USD/day
service_token: lm-services/main
sandbox: off
---

# Permissions

Rules for what the AI may do. Open by default: only the values of secrets and the engine folder `system/` are locked. What each mode allows, asks or blocks is in `modes/` (one file per mode, naming numbers of the catalog `rechte.md`); see `modes/README.md`. Change a rule by editing the file; the next tool call uses the new version.

## Layers

The core checks four layers: this file, then the mode, then the agent, then the session. Later layers win; inside a layer deny beats ask beats allow. `locked` rules are checked first, and no later layer can lift them. Without any matching rule `fallback` applies.

## Rule tools

| Rule | Pattern is matched against | Example |
|---|---|---|
| `read`, `write`, `edit` | the path, relative to the tree or absolute; `*` is one segment, `**` any depth; a write rule also covers edit | `write(projects/**)` |
| `bash` | one command of the shell line; `*` matches anything | `bash(git push*)` |
| `shell-unparsed` | a shell line the core cannot read safely; it asks unless a rule names it | `shell-unparsed` |
| `web` | `domain:<glob>` or a URL glob | `web(domain:*.example.com)` |
| `ssh`, `server` | `<server>` or `<server>:<command>` | `server(buildbox)` |
| `secret`, `secret-write` | `<service>` or `<service>/<account>`: inject, or create and change | `secret(stripe/main)` |
| `tool` | tool name, optionally `<name>:<value of the match parameter>` | `tool(send-mail)` |
| `spawn`, `skill` | agent name (empty for a copy of oneself), skill name | `spawn(webdev)` |
| `category` | `send`, `money`, `delete`, `deploy`, `account` (from `tool.md`) | `category(send)` |
| `*` | everything except `shell-unparsed` | `*` |

`{key}` in a pattern is a location from `paths:` of `lightmind.md`, for example `{secrets}`. A rule can end with `until 2026-12-31`; it stops applying on that day.

## Grants

Secrets stay denied until a layer allows them: the agent lists the secret under `# Secrets` in its `tools.md`, a session allows it with `allow: secret(name/*)`, or a mode file names it under `geheimnisse:`.

## Files that hold grants

`grant_files` lists the files that hold grants: this file, `lightmind.md`, `config.md`, the mode files and `rechte.md`, the `agentMaster.md` and `tools.md` of agents, workflows and projects, the provider files, the scripts the hooks run, the triggers and the run files a chat is resumed from. An agent that could write them freely could grant itself every secret, so they are number 13 of `rechte.md`: auto asks before each write (a fixed ask, nobody can lift it), plan blocks all writes. The reasons per line:

- Hook scripts (`tools/vault/`, `tools/watch/` ...): hook commands run without a rule check, as trusted as `lightmind.md`. Add the folder of every script you hook in yourself.
- Triggers: a trigger starts a task with the grants of the agent it names, which may be another agent than the writer.
- `config.md` (also number 14): its `defaults.mode` picks the mode of every new chat. While a mode lets an agent write it unasked, the core ignores the file.
- `events.jsonl`, `meta.json`, `session.md` of a run: a chat is resumed from them. What a resumed chat may do is kept in `system/state/`, which agents cannot reach; `log.md` stays free for progress notes.

Remove a line to hand that file to the agents. The asks catch writes with the `write` and `edit` tools, not every way a shell command could change a file.

## Settings

| Key | Meaning |
|---|---|
| `fallback` | `allow`, `ask` or `deny` when no rule matches |
| `audit` | Which kinds of action are written to the audit index in `runs/_audit/` |
| `money_limit` | Spending limit, `<amount> USD/<day, week or month>`; `off` removes it |
| `service_token` | Secret that protects the Lightmind daemons (lmd, pcd, browserd); `off` disables the check |
| `sandbox` | Reserved for a later OS sandbox; `off` |
