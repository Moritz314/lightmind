---
# config.md: the settings you want to change yourself. The core reads only the keys listed here;
# everything else in this file is ignored, and `lm check` reports it.
# Layers, later wins: template values in the program, lightmind.md, this file, system/state/local.md (per machine).

# Defaults for new chats, the start menu and agents without their own setting.
defaults:
  provider: claude-abo               # account from providers/<name>.md
  model: sonnet                      # a model of this account or a tier: strong | medium | cheap
  effort: medium                     # low | medium | high | xhigh | max | ultracode (max + every substantial task as a workflow)
  context: 200k                      # 100k | 200k | 500k | 800k | 1m (above 200k needs a model with a 1m window)
  mode: auto                         # auto | plan | bypass (bypass asks nothing anymore, use with care)

# Where a new session opens: a new chat and a continued chat from the start menu.
sessions:
  open: tab                          # tab | window | replace
  auto_title: true                   # true | false: a chat gets a title and a description by itself, from its newest work
  title_model: haiku                 # the cheap model that names chats (a model of the account or a tier)
  title_every: 1                     # named after the first answer, then every this many answers (1 = after each) and when the chat ends

# The start checklist before the start menu.
ui:
  boot: true                         # true | false | brief (warnings only); LM_NO_BOOT=1 turns it off
  boot_wait: true                    # true: stays until you press Enter | false: closes by itself
  notes_layout: split                # split: Notes & To-dos (Ctrl+N) next to the chat, from 120 columns | switch: Ctrl+N switches between chat and Notes

# Runs (lm run) and agents that start agents.
run:
  max_parallel: 6                    # this many runs at the same time, more wait
  max_depth: 3                       # runs that start runs: no deeper than this (0 = top level only)

# The automatic watch over runs a chat started with `lm run --detach` (also for the Telegram bot). More keys: lightmind.md `watch:`.
watch:
  every: 15m                         # 5m | 10m | 15m | 30m | 1h | 2h: how often every open sub-run is checked and the chat is woken; a changed state is acted on within a minute
  max_restarts: 3                    # restarts per sub-run (died, turn limit, rate limit, full context), with a growing wait; 0 = only report
  notify: both                       # telegram | ntfy | both | off: where the user hears about a dead or failed run, a restart and the end of all of them (only channels whose tool exists in tools/: telegram/, ntfy/)

# Sub-agents that a chat starts with the `spawn` tool.
spawn:
  model:                             # a model of the account or a tier (e.g. cheap); empty: the same model as the chat
  auto_turns: 3                      # this many turns a chat may start by itself when background sub-agents report; then results wait until you write

# Workflows (the `workflow` tool, effort ultracode; /ultracode on | off | status): many sub-agents run by a script.
workflow:
  max_parallel:                      # agents of one workflow at once; empty = min(16, CPUs-2), never more
  laptop_parallel: 3                 # at most this many on a laptop (Windows, macOS, server laptop)
  agent_memory_mb: 1024              # RAM one more agent may take ...
  free_memory_mb: 2048               # ... and RAM that must stay free; otherwise the next agent waits
  max_agents: 1000                   # most agents one workflow run starts (1-10000); the rest of the calls give None
  max_items: 4096                    # most elements one parallel() / pipeline() runs (1-65536); the rest give None
  budget:                            # tokens per workflow (10000-1000000000, e.g. 2000000); empty = none; a call or script budget wins
  model:                             # model of the workflow agents (a model or tier); empty = as spawn.model / the chat
  effort: max                        # effort of the workflow agents: low | medium | high | xhigh | max
  schema_retries: 3                  # fresh tries when an agent's answer does not fit its schema (0-10)
  skeptics: 3                        # sceptics of an adversarial verify, told to the model in ultracode (1-9)
  dry_rounds: 2                      # empty rounds that end a loop-until-dry, told to the model in ultracode (1-10)
  tidy_worktrees: true               # true: remove an isolated worktree without changes at the end | false: keep all
  notify: off                        # off | ntfy | telegram | both: tell the user when a long workflow has ended
  notify_after: 10                   # ... if it ran at least this many minutes (1-1440)
  confirm: false                     # true: ask before a workflow starts (never in bypass) | false: show its plan and start
  keyword: true                      # true: the word ultracode in your message turns effort ultracode on for that chat

# Memory limit for tools and shell commands: a runaway is ended before it freezes the machine.
tools:
  memory_mb:                         # MB per process tree; empty = 40 % of RAM (at least 2048) | 0 = off | never above 80 % of RAM
                                     # A tool may set its own limit `memory_mb:` in its tool.md.

# Defaults for chats from the VS Code extension (`session.new` without its own setting). Empty = as in the terminal (defaults / agent).
vscode:
  agent:                             # an agent from agents/; empty: no agent
  model:                             # a model of the account or a tier; empty: as defaults.model
  effort:                            # low | medium | high | xhigh | max; empty: as defaults.effort
  mode:                              # auto | plan | bypass (bypass asks nothing anymore, use with care); empty: as defaults.mode

# The quick chat (`lm q`, `lmq`): a chat at once on this device in the current folder, without the start menu. A missing or empty key = the value shown here.
quick:
  model: opus                        # a model of the account or a tier
  effort: high                       # low | medium | high | xhigh | max
  context: 500k                      # 100k | 200k | 500k | 800k | 1m
  mode: bypass                       # auto | plan | bypass (bypass asks nothing anymore, use with care)

# Skills: where `lm skill add <name>` and `lm skill list --available` fetch from.
skills:
  source:                            # a git URL or a folder that holds skills/<name>/SKILL.md; empty = https://github.com/Moritz314/lightmind-skills

# Remote Control (the phone app). Off until you remove the `#` before `remote:`, `enabled` and the keys you want to set.
# remote:
#   enabled: false                   # true | false: /rc and the node of this tree work only when true
#   auto: true                       # true | false: every new chat opens its Remote Control link by itself (/rc off ends it); no push. Default: true
#   gateway:                         # the server the gateway runs on (a folder in servers/); no default
#   default_node:                    # the machine a new phone session starts on, unless the app picks another
#   agent:                           # an agent from agents/ for chats started from the phone; empty: no agent
#   model:                           # a model of the account or a tier; empty: as defaults.model
#   effort:                          # low | medium | high | xhigh | max; empty: as defaults.effort
#   mode:                            # auto | plan | bypass (bypass asks nothing anymore, use with care); empty: as defaults.mode
#   push: webpush                    # webpush | ntfy | both | off: how the phone is told that Remote Control is on
#   push_questions: true             # true | false: push when a chat asks and no device has it open
#   upload_mb: 200                   # the largest file the phone app may upload
---

# Settings

This is your settings page. The values are at the top, between the two `---`. This text is never sent to the model.

## Where you change something

- By hand in this file. The core re-reads it on every call.
- In the terminal with `/settings` (also `/config`). There the `e` key opens this file in the editor.
- Later also on a settings page in the browser.

## Order of the layers

Later wins:

1. the template values in the program
2. `lightmind.md` (paths, shell, tools, hooks, master prompt)
3. this file
4. `system/state/local.md` (applies to this machine only and is not synced)

If a key is in `lightmind.md` and here, the value from here wins. `lm check` shows this as a note.

## What is read

Only the keys listed above. Everything else is ignored, and `lm check` warns about it. If the file is broken (an error in the header), `lm check` reports an error, and the values from `lightmind.md` still apply.

The default server (`defaults.server`) does not belong here but per machine in `system/state/local.md`. Permissions are in `permissions.md`. One exception is `mode`: `bypass` asks nothing anymore, which is a permission value.

This file is protected like `lightmind.md`: an agent may change it only after asking you.
