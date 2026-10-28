---
name: example
when: "cron 0 7 * * 1-5"   # weekdays at 07:00 (apply.py also accepts schedule: "0 7 * * 1-5")
agent: my-agent
task: "Summarise yesterday's mail and write the result to $LM_RESULTS/summary.md"
enabled: false              # set true to switch the trigger on
---

# example trigger

Why this trigger exists and what its result is used for. Replace this text. Install it with
`uv run tools/triggers/apply.py` (see `tools/triggers/README.md`).
