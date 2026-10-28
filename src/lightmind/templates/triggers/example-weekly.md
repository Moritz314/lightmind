---
name: example-weekly
when: "cron 0 8 * * 1"      # Mondays at 08:00
agent: example-agent
task: "Count the words of the README.md files in skills/, tools/ and agents/ with the skill text-stats and write the table to $LM_RESULTS/text-stats.md"
enabled: false              # switch on with true, then install it: uv run tools/triggers/apply.py
---

# example-weekly

The example trigger of a new tree: once a week the example agent counts the words of the folder READMEs and leaves a table in its result folder (`runs/ergebnisse/`). It is off until you switch it on, so nothing runs by itself after `lm init`.

Try the same task by hand first:

```text
lm run -a example-agent "Count the words of the README.md files in skills/, tools/ and agents/ with the skill text-stats and write the table to $LM_RESULTS/text-stats.md"
```

Then set `enabled: true` and run `uv run tools/triggers/apply.py` (`--dry-run` shows what it would install, `--list` what is installed). The format of this file is explained in `triggers/README.md`.
