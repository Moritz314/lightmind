# workflows

Folder for workflows: a repeatable job. Two ways to run one, and a rule for choosing.

- **A script** (`workflow.py`) that calls agents like functions. The order of the steps is fixed in code; the code costs no tokens, the agents only see their own step. Use it when you can write down the steps beforehand: plan, then several builds at the same time, then a review.
- **A lead agent** (`agentMaster.md`) that reads the job and decides the steps itself. Use it when the parts cannot be known until the work starts. See `agents/_example-lead/`.

Fixed steps and fan-out belong in a script. A lead agent only where the next step depends on what was found.

## Format

```text
workflows/
├─ lib.py             the helpers a script imports (below)
└─ <name>/
   ├─ agentMaster.md  optional: the lead agent for the open-ended parts, same keys as in agents/
   ├─ tools.md        what the lead agent may use
   └─ workflow.py     optional: the script
```

`agentMaster.md` and `tools.md` are explained in `agents/README.md` and hold grants, so writing them asks the user for a confirmation.

## The script and lib.py

A script is plain Python. `lib.py` (standard library only) starts every agent as its own `lm run` process and hands back the result as a dictionary `{run_id, status, exit_reason, text, cost_usd, ok}`; with a schema also `result`, the parsed answer.

| Call | Does |
|---|---|
| `lib.run(task, agent=None, schema=None, max_usd=None, step=None, **flags)` | One run, waits for it. A failed run is tried once more. `schema` is a dict or a file: the answer must be JSON that fits, the core asks again up to three times. `flags`: `model`, `project`, `mode`, `account`, `effort`, `max_turns` |
| `lib.parallel(items, limit=4)` | Several at once, `limit` at a time, results in order. An item is a task text, a dict of `run` arguments or a function |
| `lib.run_detached(task, ...)` and `lib.wait(ids, timeout=None)` | Start in the background and collect later: many runs in flight, other work in between |
| `lib.work_dir()` | The folder where the steps leave files for each other (`$LM_RESULTS`, the result folder of the run that runs the script). Give each parallel step its own file |
| `lib.run_out(result)` | The `out/` folder of a finished run: what that agent wrote |
| `lib.budget(usd)`, `lib.spent()` | Stop starting runs after that many USD; the sum so far |
| `lib.resume(file)` | Remember finished steps in a JSON file, keyed by the step name (`step=`) and a hash of its inputs. Start the script again after a stop and only the missing steps run |

Steps pass file paths, not text: write the long result to a file and give the next agent the path. That keeps every agent's context small.

## Limits the core keeps for you

- `--max-usd` and `--max-turns` of `lm run` end a run between two turns when it has spent or used that much.
- `run.max_depth` in `lightmind.md` stops runs that start runs from going deeper than that; `run.max_parallel` caps how many run at the same time (the rest wait for a slot).
- A run without a screen asks the user through `run.ask_channel` when a rule says ask. No answer counts as no.

## Use

```text
uv run workflows/<name>/workflow.py "input"
```

Every run logs to `runs/`; `lm runs show <id>` and `lm runs cost` show what happened and what it cost. A script stops for a human with a separate step: end the script, show the result, start the next script after the user agrees.

## Create a workflow

1. Copy `_example/` to `<name>/`.
2. Edit `workflow.py`: the agents, the briefs, the schemas. Delete `agentMaster.md` and `tools.md` when no lead agent is needed.
3. Run it with a small input first and look at `lm runs cost`.

Files and folders starting with `_` are ignored.
