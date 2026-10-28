"""Script of the workflow: fixed control flow around agents.  Delete this file when the steps in
agentMaster.md are enough.

Start it from the tree root:   uv run workflows/my-workflow/workflow.py "topic"
(copy `_example/` to `my-workflow/` first; `lib.py` next to the workflow folders does the calling.)

Plan -> three builds at the same time -> review.  Between the steps only Python runs, so the
control flow costs no tokens; the agents hand each other file paths, not text.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))      # the workflows/ folder, where lib.py lives
import lib

topic = sys.argv[1]
lib.budget(5.0)                                  # stop starting runs after 5 USD
lib.resume(lib.work_dir() / "steps.json")        # run it again after a stop: finished steps are not repeated
out = lib.work_dir()

# 1) Plan: the answer must be JSON of this shape, the core checks it and asks again when it does not fit
PLAN = {"type": "object", "required": ["parts"],
        "properties": {"parts": {"type": "array", "items": {
            "type": "object", "required": ["name", "brief"],
            "properties": {"name": {"type": "string"}, "brief": {"type": "string"}}}}}}
plan = lib.run(f"Split this job into exactly three independent parts: {topic}", agent="my-agent",
               schema=PLAN, max_usd=0.5, step="plan")
if not plan["ok"]:
    sys.exit(f"plan failed: {plan['exit_reason']}")

# 2) Three builds in parallel, each with its own file so they never touch the same one
builds = lib.parallel([
    {"task": f"{p['brief']}\nWrite the result to {out / (p['name'] + '.md')} and answer with the path.",
     "agent": "my-agent", "max_usd": 1.0, "step": "build-" + p["name"]}
    for p in plan["result"]["parts"][:3]])

# 3) Plain Python in between: which builds worked?
good = [out / (p["name"] + ".md") for p, b in zip(plan["result"]["parts"], builds) if b["ok"]]
(out / "report.json").write_text(json.dumps({"built": [str(g) for g in good], "usd": lib.spent()}), encoding="utf-8")

# 4) Review: one agent reads the files and says what is wrong
review = lib.run("Review these files against the topic and list every problem:\n" + "\n".join(map(str, good)),
                 agent="my-agent", step="review")
print(review["text"])
print(f"cost so far: {lib.spent():.2f} USD", file=sys.stderr)
