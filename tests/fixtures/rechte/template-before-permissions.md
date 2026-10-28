---
# permissions.md: what the core allows. The core enforces these rules; the AI never sees them, it only
# gets the text of the active mode. Normally open: everything is allowed unless a rule below says no.
# Rule syntax: tool or tool(pattern), for example read(**), write(projects/**), bash(git push*),
# web(domain:*.example.com), secret(name/account), tool(name), category(send).
# {key} in a pattern is a location from lightmind.md paths, for example {secrets}.
# Layers, later wins: this file, then the mode preset, then the agent, then the session. Inside a layer
# deny beats ask beats allow. `locked` rules are checked first and no later layer can lift them.
fallback: allow
deny:
  - secret(*)                        # a secret is usable only where a layer grants it, e.g. allow: secret(name/*)
  - secret-write(*)                  # creating or changing secrets needs a grant too, e.g. allow: secret-write(name/*)
locked:
  deny:
    - read({secrets}/**/*.env)       # secret values: never read, never written, in every mode
    - write({secrets}/**/*.env)
    - edit({secrets}/**/*.env)
    - bash(*{secrets}/*.env*)        # catches the obvious ways, not every way
    - read({system}/**)              # the engine: runtime, Claude logins, tokens, money ledger, approvals
    - write({system}/**)
    - edit({system}/**)
    - bash(*{claude}*)               # the Claude logins, same obvious ways
# Files that hold grants. Listing a secret in an agent's tools.md, a hook in lightmind.md or a secret in a
# provider file is what lets that agent or hook use it. With everything else open, an agent that could write
# these files could grant itself every secret and defeat the one default protection there is. So writing
# them always asks the user: a locked ask, which no agent, session or mode can lift. Bypass asks nothing, so it denies them. It is an
# ask and not a deny, so creating an agent stays possible with one confirmation. Plan mode denies every
# write anyway. A write rule also governs edit. The modes below use this list; copy it into every new mode.
# config.md counts as well: its defaults.mode picks the mode of every new chat, and bypass asks nothing.
# Hook scripts count as well: the commands in on_start, on_prompt and on_end run without a rule check, as
# trusted as lightmind.md itself. So the script the template hooks in (tools/vault/) is listed too, and a
# script you hook in yourself belongs here as well. A trigger counts too: it starts a task with the grants
# of the agent it names, which may be another agent than the one that wrote the trigger.
# The run files a chat is resumed from are on the list too: the history in events.jsonl, the cost and status
# in meta.json, the state shown in session.md. What a resumed chat may do (agent, mode, account, session
# rules) is kept in the state folder, which is locked above; these three are the rest of the record. log.md
# is not listed: agents write their progress there.
# To hand a file to the agents, delete its line (or the whole list and the two `*grant_files` lines).
grant_files: &grant_files
  - write({permissions})
  - write({entry})
  - write({config})                  # the settings page: mode there decides whether questions are asked at all
  - write({agents}/*/{file_agent})
  - write({agents}/*/{file_agent_tools})
  - write({workflows}/*/{file_agent})
  - write({workflows}/*/{file_agent_tools})
  - write({projects}/*/{file_project_tools})
  - write({providers}/*.md)
  - write({tools}/vault/**)          # the script of the on_start hook: hook scripts run without a rule check
  - write({tools}/watch/**)          # the tick runs this check tool every beat, without a rule check
  - write({tools}/ntfy/**)           # the watch runs these two to tell the user, also without a rule check
  - write({tools}/telegram/**)
  - write({triggers}/**)             # a trigger starts a task with the grants of ANOTHER agent
  - write({runs}/*/events.jsonl)     # a chat is resumed from its run files, not from what an agent wrote there
  - write({runs}/*/meta.json)
  - write({runs}/*/session.md)
modes:
  auto:
    label: Auto
    color: green
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
    # allow: [secret(mailhost/*)]  # example: a secret this mode may use; the session or the agent can grant others
    # Bypass never asks: the core turns every ask into allow in this mode. The files that hold grants are a locked
    # deny here instead of a locked ask: an agent that could write permissions.md, a tools.md or a hook script
    # without a question could lift the locked denies above (secret values, the engine). config.md is one of them.
    locked:
      deny: *grant_files
    text: Do not ask questions. Decide and act. Secrets work only where the session or the agent grants them.
audit: { secret_use: on, secret_write: on, pc: on, browser: on, send: on, money: on, delete: on, deploy: on, screenshots: thumb }
money_limit: 20 USD/day
service_token: lm-services/main      # secret for lmd, pcd and browserd; "off" disables the token check
sandbox: off                         # reserved for a later OS sandbox
---

# Permissions

Rules for what the AI may do. Open by default: only the values of secrets and the engine folder `system/` are locked, and writing the files that hold grants asks first. Change a rule by editing this file; the next tool call uses the new version.

## Rule tools

| Rule | Pattern is matched against | Example |
|---|---|---|
| `read`, `write`, `edit` | the path, relative to the tree or absolute; `*` is one segment, `**` any depth | `write(projects/**)` |
| `bash` | one command of the shell line; `*` matches anything | `bash(git push*)` |
| `web` | `domain:<glob>` or a URL glob | `web(domain:*.example.com)` |
| `ssh`, `server` | `<server>` or `<server>:<command>` | `server(buildbox)` |
| `secret`, `secret-write` | `<service>` or `<service>/<account>`: inject, or create and change | `secret(stripe/main)` |
| `tool` | tool name, optionally `<name>:<value of the match parameter>` | `tool(send-mail)` |
| `spawn`, `skill` | agent name, skill name | `spawn(webdev)` |
| `category` | `send`, `money`, `delete`, `deploy`, `account` (from `tool.md`) | `category(send)` |
| `*` | everything | `*` |

A rule can end with `until 2026-12-31`; it stops applying on that day.

## Grants

Secrets stay denied until a layer allows them. The usual way is listing the secret under `# Secrets` in the agent's `tools.md`, which grants it to that agent. A session can grant with `allow: secret(name/*)`, and a mode preset can carry the same line (see the comment in `bypass`).

## Files that hold grants

The grants live in files: `tools.md` of agents, workflows and projects, the `permissions:` block of an agent, this file, the hooks in `lightmind.md`, and the secret named in a provider file. `config.md` is on the list as well, because its `defaults.mode` chooses the mode of every new chat and `bypass` asks nothing. A project gets its grants from its `tools.md` only; a `permissions:` block in `projectMemory.md` is ignored. The scripts the hooks run belong to the same group: hook commands run without a rule check, so `tools/vault/`, which the template hooks in, is listed as well. So do the triggers: a trigger starts a task with the grants of the agent it names, and that can be another agent than the one that wrote the trigger. Creating a trigger stays possible, with one confirmation. If an agent could write these files freely, it could grant itself every secret, and the only default protection would mean nothing. So `grant_files` lists them, and the mode `auto` carries the list as a locked ask: every write or edit of one of these files asks the user, once per write, and no agent, session or mode can lift that. The mode `bypass` asks nothing, so it carries the list as a locked deny: in bypass an agent cannot write these files at all. Creating an agent therefore takes a confirmation for `agentMaster.md` and one for `tools.md`. Plan mode denies all writes.

The last three lines of `grant_files` protect the run files a chat is resumed from (`events.jsonl`, `meta.json`, `session.md`; `log.md` stays free for agents). The core keeps what a resumed chat may do in `system/state/`, which agents cannot read or write, so these asks guard the history and the numbers, not the grants.

Remove a line of `grant_files` to hand that file to the agents. Add a line for every script you hook in yourself. A new mode must carry the list as well (`locked: ask: *grant_files`), otherwise it has no such protection. The asks catch writes made with the `write` and `edit` tools, not every way a shell command could change a file.

## Modes

A mode is a preset under `modes:` with a label, a color, its own rules and a short text that goes to the AI. Add a mode by adding a key. Choose the default in `lightmind.md` (`defaults.mode`).

## Settings

| Key | Meaning |
|---|---|
| `audit` | Which kinds of action are written to the audit index in `runs/_audit/` |
| `money_limit` | Spending limit, `<amount> USD/<day, week or month>`; `off` removes it |
| `service_token` | Secret that protects the Lightmind daemons; `off` disables the check |
