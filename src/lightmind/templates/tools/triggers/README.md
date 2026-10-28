# triggers (apply)

Installs the cron triggers of `triggers/*.md` into the scheduler of the OS, so they fire when the core is not running.

## Call

```bash
uv run tools/triggers/apply.py --list          # every trigger, enabled or not, installed or not
uv run tools/triggers/apply.py --dry-run       # show what would be installed
uv run tools/triggers/apply.py                 # install (idempotent)
uv run tools/triggers/apply.py --remove        # remove all; --remove NAME removes one
```

`--tree DIR` selects another tree (default `$LIGHTMIND_HOME`, else the tree this tool sits in).

- **Linux:** the block between `# lightmind triggers` and `# end lightmind triggers` in the user crontab is replaced as a whole; the rest of the crontab stays.
- **Windows:** one scheduled task `Lightmind <name>` per trigger (`schtasks /Create /F`); tasks of triggers that are gone or disabled are deleted. Supported schedules: every N minutes (`*/5 * * * *`), hourly (`30 * * * *`), daily (`0 7 * * *`), weekly (`0 7 * * 1-5`, `0 9 * * 1,4`), monthly (`0 6 1 * *`). Anything else is skipped with a message.

Each trigger runs `<tree>/system/lm run --detach -a <agent> "<task>"`. A trigger with `session: <name>` runs `<tree>/system/lm run --continue <name> --create -a <agent> "<task>"` instead (not detached; first run makes the session, later ones continue it). Only triggers with `enabled` not `false` and a valid cron schedule are installed; the others are reported on stderr. See `triggers/README.md` for the file format.
