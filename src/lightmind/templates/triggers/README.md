# triggers

Folder for triggers: a rule that starts an agent or a workflow when a time comes or something happens.

## Format

One `.md` file per trigger; the file name is the trigger name. Files starting with `_` are ignored.

| Key | Meaning |
|---|---|
| `schedule` | A 5-field cron expression (for example `0 7 * * 1-5`). Installed by `tools/triggers/apply.py` |
| `machine` | Name of the instance that installs the trigger: the name `lm sync init` or `lm sync join` gave it (`system/state/machine`, `$LM_MACHINE` overrides), compared without regard to case. Every other instance skips the file. Without `machine` only the instance with `always_on: true` in `system/state/local.md` installs it. An instance that has no name yet (no sync) installs every trigger except one with `run: sync` |
| `run` | `sync` or `check`: the trigger runs that `lm` command (`system/lm sync`, `system/lm check`) with no agent and no task. It replaces `agent` and `task` (giving both is an error), and the installer refuses any other value because it ends up in crontab or `schtasks`. `run: sync` needs a sync repo, so use it together with `machine` |
| `session` | With `when: every|in`: an EXISTING session (name or run id) that the trigger wakes with its `task` text as a message marked [Timer] (a headless turn when nobody holds the session). Otherwise: a session name (`[A-Za-z0-9._-]`, up to 64 characters): the trigger goes on in that fixed session instead of starting a fresh run. It runs `system/lm run --continue <name> --create -a <agent> "<task>"`: the first run makes the session, every later one continues it (same history, agent, account and model). It needs `agent` and `task` and cannot be combined with `run`. The call is not detached, and a session that is open in another process is waited for up to 10 minutes before the run ends as paused |
| `when` | `every <N>m|h` (repeating) or `in <N>m` (once): fired by `lm triggers tick`, see below. Other kinds for the core's own scheduler: `cron <expression>` (same as `schedule`), `file <glob>`, `webhook <path>`, `telegram`, `mail <account>` or `quota <account> >= 0.9` |
| `until` | With `when: every ...`: the trigger ends at this time (`2026-10-23T18:00`) or when the sub-runs of `owner` are over (`children-done`) |
| `owner` | The session (run id) that made the trigger; with `until: children-done` the sessions whose sub-runs count |
| `name` | Optional; default is the file name |
| `task` | The task text given to the agent |
| `agent` | The agent to start (folder in `agents/`) |
| `workflow` | A workflow to start instead of an agent (folder in `workflows/`) |
| `tools` | Extra tools for this run |
| `data` | Extra files loaded into the context of this run |
| `server` | The server to run on. Empty means `defaults.server` |
| `enabled` | `false` switches the trigger off without deleting it |

The text below the frontmatter is free. Use it to note why the trigger exists.

## Use

Cron triggers are installed into the OS scheduler with `uv run tools/triggers/apply.py` (crontab block on Linux, `Lightmind <name>` tasks on Windows; `--list`, `--dry-run`, `--remove`); each runs `system/lm run --detach -a <agent> "<task>"`, or, with `session:`, `system/lm run --continue <name> --create -a <agent> "<task>"` (not detached), or, with `run:`, just `system/lm sync` or `system/lm check`. The scheduler of the core reads the other `when` kinds and starts the run when they fire. A run started by a trigger has nobody to talk to: questions go to the channel in `run.ask_channel` of `lightmind.md`, and the permission rules decide what the run may do on its own. The run has the grants of the agent the trigger names, not those of whoever wrote the file, so every write to this folder asks the user for a confirmation.

## The tick: `every` and `in`

`when: every 15m` and `when: in 20m` are not cron: the OS calls `lm triggers tick` every minute and the core fires what is due (a missed beat is made up at most once; the state is in `system/state/ticks.json`). Set the beat up once with `lm triggers install` (systemd user timer, else crontab; on Windows a scheduled task); `lm triggers status` and `lm triggers list` show it. A trigger with `session` (or `owner`) wakes that session: the message goes into the session's inbox, a process that holds the session (the screen, `lm serve`) reads it, and otherwise the core continues the session headless for one turn. The message is a system note, not a command from the user. With an agent and a task and no session the tick starts a new detached run.

Chats make their own timers with the `timer` tool; those and the watch triggers live in `system/state/triggers/`, which no agent can write.

## Example

`example-weekly.md` starts the example agent every Monday at 08:00 to count the words of the folder READMEs. It ships with `enabled: false`: nothing runs by itself until you switch it on and run `uv run tools/triggers/apply.py`.

## Create a trigger

1. Copy `_example.md` to `<name>.md`.
2. Set `schedule` (or `when`), `task` and the `agent` (the installer needs an agent and a task, or `run: sync|check` instead of both). With several synced instances add `machine:` so only the one that should run it installs it.
3. Start with `enabled: false`, test the agent by hand, then switch the trigger on and run `apply.py`.
