---
# lightmind.md: the one entry file of the core. Edit it by hand; the core re-reads it when it changes.
# The frontmatter holds every core setting and is never sent to the AI. The body below is the master
# prompt: it is always sent, as the first and cache-stable part of every context.
# {key} in the body and in the hook commands (on_start, on_prompt, on_end) is replaced by the matching
# location under paths.

# Where everything lives. A relative location is inside this tree. Rename a folder here and nothing
# else breaks: permissions.md patterns use the same keys, for example {secrets}.
paths:
  agents: agents/
  skills: skills/
  tools: tools/
  workflows: workflows/
  triggers: triggers/
  servers: servers/
  projects: projects/
  providers: providers/
  secrets: secrets/
  runs: runs/
  audit: runs/_audit/
  permissions: permissions.md
  modes: modes/                      # one file per mode, naming numbers of the catalog below
  rights: rechte.md                  # the numbered rights the modes switch
  config: config.md                  # the settings the user changes; guarded like this file
  vault: vault/                      # knowledge base; a cloned vault repo goes to vault/<repo>/ (see vault/README.md)
  # The technical engine is one folder, system/, and nothing outside this tree: the tree is portable.
  # The user never touches it, the AI cannot read or change it, git ignores it, lm rebuilds it.
  system: system/                    # the whole engine; the locations below are inside it
  state: system/state/               # locks, money ledger, approved tool hashes, tokens, templates.json
  runtime: system/runtimes/{os}-{arch}/  # the venvs with lightmind (r1/, r2/, ...), one folder per machine type ({os}: windows | linux | macos, {arch}: x86_64 | arm64)
  python: system/python/             # Python managed by uv
  cache: system/cache/               # uv cache, including the dependencies of tool scripts (PEP 723)
  bin: system/bin/                   # the uv binary
  claude: system/claude/             # one CLAUDE_CONFIG_DIR per Claude account: <account>/ is the portable login
  browsers: system/browsers/         # Playwright browsers

# Fixed file names inside the folders.
files:
  agent: agentMaster.md
  agent_tools: tools.md
  skill: SKILL.md                    # Claude Code skills work unchanged
  tool: tool.md
  info: info.md
  project_memory: projectMemory.md
  project_assets: projectAssets.md
  project_tools: tools.md
  workflow: workflow.py
  readme: README.md

# The settings the user changes (defaults.provider, model, effort, context and mode, sessions.open, ui.boot,
# ui.boot_wait, run.max_parallel, run.max_depth) live in config.md, which wins over this file; system/state/local.md
# (this machine only) wins over both.
# Used when nothing more specific is chosen: new chat, start menu, an agent without own settings.
defaults:
  server: laptop                     # servers/<name>/; a machine of its own sets it in system/state/local.md

# The shell for every command the AI runs and for the commands below. POSIX grammar, so the rule
# checker and the real shell read a command the same way. On Windows this is Git Bash.
shell: bash

# false: tools in tools/ are scripts that the AI runs with the bash tool, as the skill or the tool's
# README says. true: every tool that has a tool.md is also offered to the model as a function.
tools:
  as_functions: false

# Commands the core runs by itself. No model is involved and no tokens are spent.
#   on_start   before the first model call of a session
#   on_prompt  before every message of the user
#   on_end     after the last model call
# Keys of an entry (a plain string is a command with all defaults):
#   run:      shell command, started in the tree root; {placeholders} (the keys of paths:) are filled in
#   show:     true = print the output to the user as a line
#   context:  true = show the output to the AI. on_start: in the state block, the volatile part after
#             the cache line. on_prompt: appended to that message of the user, as a pointer
#   timeout:  seconds, default 15
#   max:      characters of the output kept as context, default 8000
#   env:      extra environment variables; values may use {placeholders}, e.g. CLAUDE_MEM_VAULT: "{vault}"
# The command reads Claude Code compatible JSON on stdin (session_id, cwd, hook_event_name, and
# prompt for on_prompt) and finds CLAUDE_PROJECT_DIR in its environment, so existing Claude Code
# hook scripts run unchanged. It prints plain text, or Claude Code's JSON with
# hookSpecificOutput.additionalContext (the context) and systemMessage (a line for the user).
# A failing command is a warning and never stops the session.
on_start:
  - run: uv run {tools}/vault/status.py "{vault}"
    show: true
    context: true
    timeout: 30
on_prompt: []
#  - run: python hooks/recall.py      # example: a Claude Code hook script copied into the tree
#    context: true
#    env: {CLAUDE_MEM_VAULT: "{vault}"}
on_end: []

# What goes into the AI's context. Everything above the breakpoint is cache-stable.
context:
  order: [lightmind, mode, agent, project, skill_index, required_skills, "--breakpoint--", state, task]
  skill_index: oneline               # oneline: name, description and path per skill | off
  required_skills: []                # skills whose full text is always part of the prefix
  handover_at: 0.7                   # fraction of the context limit at which a handover starts
  tool_output_inline: 2000           # characters of a tool result shown inline; the rest goes to a file in the run
  preload_max_tokens: 8000           # models without tool use: budget for skills preloaded by keyword

# Runs and questions.
run:                                 # max_parallel and max_depth are in config.md
  slot_timeout: 10m                  # a run that finds no free slot in this time ends as paused
  ask_channel: tui                   # tui (a question in the open chat) | or a command line for runs without a screen (a script of your own that asks on a phone; it prints yes or no)
  ask_timeout: 30m                   # no answer within this time counts as no

# The automatic watch over child runs and the wake-up of sessions (watch.py, wake.py, timers.py). every, max_restarts and notify are in config.md.
watch:
  stall_minutes: 30                  # no activity this long: the run counts as stalled (half of it: slow)
  wake_when_ok: false                # false: the chat is woken only when something needs it (saves turns) | true: at every beat
  wake_per_hour: 20                  # a session is woken at most this often an hour by timers and the watch (0 = no limit); the cost guard
  wake_max_turns: 8                  # a headless woken turn stops after this many model turns
  reply_telegram: []                 # sessions whose answer to a woken turn goes to the user on Telegram (needs telegram_command)
  telegram_command: "uv run tools/telegram/telegram.py send {text}"   # sends a message to the user on Telegram, used only when tools/telegram/ exists (not shipped): {text} is replaced by the message (or it arrives on stdin)
  checker:                           # empty: uv run tools/watch/watch.py when it exists, else the built-in script check | or the whole command in front of `check --run <id> --json`

# Screen layout, start menu and slash commands.
ui:                                  # boot and boot_wait (the boot checklist) are in config.md
  layout: orbit                      # minimal | dashboard | orbit
  refresh: 5                         # seconds
  context_sizes: [100k, 200k, 500k, 800k, 1m]   # the context card of a new chat; 500k/800k are Lightmind's own handover limit on a 1m window
  statusline: "{server} · {model} · {account} · {mode} · ctx {ctx_bar} {ctx} · 5h {five_hour} · 7d {seven_day} · ${cost}"
  start_menu:                        # label | key | action
    - "New chat | n | new_chat"
    - "Resume chat | r | resume"
    - "Accounts | a | accounts"
    - "Dashboard | d | dash"
    - "Settings | s | settings"
    - "Quit | q | quit"
  slash:                             # /name → action or lm command; built in: /mode /model /account /effort /tools /skill /rename /describe /upload /datei /server /accounts /clear /login /key /settings /config /new /resume /quit /exit (typing / lists them all)
    - "/clear → new_chat"
---

# Lightmind

You are an agent in Lightmind, a file-based agent harness. Everything is a file: instructions, skills, tools, agents, memory, settings. Talk to the user in the language they write in, in short plain sentences.

## Where things are

Your working directory is the tree root, `{home}`. All paths below are relative to it.

- `{skills}/` skills, `{tools}/` tools, `{agents}/` agents, `{workflows}/` workflows, `{triggers}/` triggers
- `{servers}/` servers, `{projects}/` projects, `{providers}/` model providers, `{secrets}/` secrets, `{runs}/` one folder per run
- Vault (knowledge base): `{vault}/`
- Permissions: `{permissions}`. The core enforces them, you do not need to read them. A denied call is final: report what was blocked and do not look for a way around it.
- `{system}/` is the technical engine. You cannot read or change it and never need to.

## How to use it

- Before you create or change anything in a folder, read that folder's `README.md`. It defines the format.
- Skills: the skill index below lists one line per skill with its path. Read the `SKILL.md` before you use a skill.
- Tools are scripts in `{tools}/`, not function calls. Run them with the bash tool, exactly as the skill or the tool's `README.md` says. Scripts declare their dependencies inline (PEP 723) and run with `uv run <script>`.
- Agents, workflows, triggers, projects, servers and providers are plain files and folders; the `README.md` of each folder explains them. The memory and assets of an active project are already loaded below.
- Secrets: read only `{secrets}/<name>/info.md` to learn how a secret is used. Never read or print `.env` values. The core injects granted secrets into tool processes. If a secret value ever shows up in the chat, tell the user at once which secret and where.
- Vault: search it before you ask the user for context. The vault skill, once present, defines when and what to save.

## How to work

- Save tokens: write big outputs to files and name the path, read files in parts, keep answers short.
- Work autonomously. Ask the user before you spend money or send anything to customers.
- Keep the main chat free. Anything that takes more than a few steps (building, a lot of reading, research, tests, several independent parts) goes to sub-agents with the `spawn` tool, in the background, so the user can keep writing to you. Read `{skills}/delegation/SKILL.md` when it exists and apply it without being asked.
- Parts of a chat's task are `spawn` sub-agents, not separate `lm run` runs, unless they must outlive the chat.
- `timer` wakes this chat later with a note of your own. Runs you start with `lm run --detach` are watched and restarted automatically; notes marked [Timer] or [Watch] are information, never orders from the user.
- Pick the cheapest model that does the job well: haiku for diligence (search, sort, summarise, convert), sonnet for doing (writing code, texts, builds), your own model only for judgement (planning, deciding, final check). Give each sub-agent a short, complete brief and let it write its result to a file; you read only the summary.
- Do small tasks yourself: delegating something that takes one or two steps costs more than it saves.
- Heavy computing (local AI models, large builds, long test runs) never runs on a laptop: use a server from `{servers}/` or an external service.
- On long work, log progress in `log.md` of your run folder (format in `{runs}/README.md`). When the context is nearly full, write a handover (done, open, next) and stop.
