# runs

Folder for the logs of runs. The core creates one folder per run and writes to it; nothing here is edited by hand.

## Format

```text
runs/<YYYYMMDD-HHMMSS>-<agent>-<4 hex>/
├─ task.md          the task: goal, limits, sources, check, parent
├─ meta.json        agent, model, account, start, end, tokens, cost, status, results_dir
├─ session.md       mode, tool changes and rule overrides of this run
├─ events.jsonl     one line per model turn, tool call, question and rule decision
├─ log.md           progress notes of the agent; ends with a handover and an end marker
├─ tool-out/        the full text of long tool results
└─ out/             link (Windows junction, POSIX symlink) to the result folder; older runs have a real folder here

runs/ergebnisse/<YYYY-MM-DD> <title>/    the result folder of a run: reports and files for a person
```

The result folder is made with the first result (a chat that produces nothing has none; an empty one is removed when the run ends), named after the start date and the chat title, and renamed when the title changes as long as it is empty; once it holds a file its name stays, so file links sent in the chat keep working (`meta.json` `results_dir`, relative to the tree root, always names the current one; `out/` follows). A name taken by another run gets ` (2)`, ` (3)`. `$LM_RESULTS` (= `runs/<id>/out`) is the path to write results to: reports, summaries and files for a person go there, in files and subfolders with speaking names. `ergebnis.txt` in the run folder holds the path when a link could not be made.

`runs/_audit/` holds the audit index: one line per secret use, send, delete, deploy, money and account action.

The agent writes `log.md` and the files in `$LM_RESULTS`; the core writes everything else. The folder is git-ignored. Old runs can be deleted; `meta.json` is the only record of what a run cost.

## Log and handover

Your run folder is named as `run:` in the state block of your context. Append short progress notes to `log.md` while you work. When the work ends, or when the context is nearly full, end the file with:

```text
## Handover
done: what is finished
open: what is not
next: the first step for whoever continues, with file paths
<!-- lm:end status=handover -->
```

Use `status=done` or `status=failed` when the work is over. A run that continues starts from this handover.

## Use

`lm runs` lists runs with their cost, `lm runs show <id>` prints one.
