"""Helpers for workflow scripts: call an agent like a function.  Standard library only.

    import sys, pathlib; sys.path.insert(0, str(pathlib.Path(__file__).parent.parent))   # the workflows/ folder
    import lib

    plan = lib.run("Split the job into parts", agent="planner", schema={"type": "object", "required": ["parts"]})
    results = lib.parallel([{"task": p, "agent": "worker", "step": "build"} for p in plan["result"]["parts"]])
    print(lib.spent(), "USD so far")

Every call is `lm --json run ...` in a process of its own: the script holds results in variables and files,
the control flow costs no tokens.  Finds `lm` through $LM_CMD, else $LM_HOME/system/lm(.cmd), else the PATH.
"""
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

_lock = threading.Lock()
_state = {"spent": 0.0, "budget": None, "cache": None, "data": {}}


class BudgetExceeded(RuntimeError):
    pass


def lm_command() -> list:
    if os.environ.get("LM_CMD"):
        return shlex.split(os.environ["LM_CMD"])
    home = os.environ.get("LM_HOME") or os.environ.get("LIGHTMIND_HOME") or ""
    launcher = Path(home, "system", "lm.cmd" if os.name == "nt" else "lm")
    return [str(launcher)] if home and launcher.is_file() else [shutil.which("lm") or "lm"]


def _lm(*args, stdin=None):
    p = subprocess.run([*lm_command(), "--json", *args], input=stdin, capture_output=True, text=True, encoding="utf-8")
    try:
        return json.loads(p.stdout), p
    except ValueError:
        return None, p


def _failed(p) -> dict:
    return {"run_id": "", "status": "failed", "exit_reason": (p.stderr or "no answer").strip()[-300:], "text": "",
            "cost_usd": 0.0, "ok": False}


def _flags(agent, schema, max_usd, kw) -> list:
    args = []
    if agent:
        args += ["-a", agent]
    if max_usd is not None:
        args += ["--max-usd", str(max_usd)]
    if schema is not None:
        if isinstance(schema, dict):          # a schema given as a dict goes to a file next to the step files
            f = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False, dir=work_dir(), encoding="utf-8")
            json.dump(schema, f)
            f.close()
            schema = f.name
        args += ["--schema", str(schema)]
    for k, v in kw.items():                   # model, project, mode, account, effort, max_turns
        args += ["--" + k.replace("_", "-"), str(v)]
    return args


def _add_cost(res: dict) -> dict:
    with _lock:
        _state["spent"] += float(res.get("cost_usd") or 0)
    res["ok"] = res.get("status") == "done"
    return res


def run(task, agent=None, schema=None, max_usd=None, step=None, **kw) -> dict:
    """One run, waits for it.  Returns {run_id, status, exit_reason, text, cost_usd, ok} and `result` (the parsed
    answer) when a schema was given.  A failed run is tried once more.  `step` names it for the resume cache."""
    if _state["budget"] is not None and spent() >= _state["budget"]:
        raise BudgetExceeded(f"{spent():.2f} of {_state['budget']} USD used")
    key = f"{step or agent or 'run'}:" + hashlib.sha256(json.dumps([task, agent, schema, kw], sort_keys=True, default=str).encode()).hexdigest()[:16]
    if key in _state["data"]:
        return _state["data"][key]
    for _ in range(2):
        res, p = _lm("run", "-", *_flags(agent, schema, max_usd, kw), stdin=task)
        res = _add_cost(res if isinstance(res, dict) else _failed(p))
        if res["status"] != "failed":
            break
    if res["ok"] and _state["cache"]:
        with _lock:
            _state["data"][key] = res
            tmp = Path(_state["cache"] + ".tmp")
            tmp.write_text(json.dumps(_state["data"], ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, _state["cache"])
    return res


def parallel(items, limit=4) -> list:
    """Run several at once, `limit` at a time; results in the order of `items`.  An item is a task text, a dict of
    arguments for run() (with `task`) or a function without arguments."""
    def one(item):
        if callable(item):
            return item()
        return run(item) if isinstance(item, str) else run(**item)
    with ThreadPoolExecutor(max_workers=limit) as pool:
        return list(pool.map(one, items))


def run_detached(task, agent=None, schema=None, max_usd=None, **kw) -> str:
    """Start a run in the background and return its run id at once; collect it with wait()."""
    res, p = _lm("run", "-", "--detach", *_flags(agent, schema, max_usd, kw), stdin=task)
    if not isinstance(res, dict) or "run_id" not in res:
        raise RuntimeError("could not start the run: " + (p.stderr or "").strip()[-300:])
    return res["run_id"]


def wait(ids, timeout=None) -> list:
    """Block until the runs have ended; one result per id (a run still going at `timeout` has status running)."""
    res, p = _lm("wait", *ids, *(["--timeout", str(timeout)] if timeout else []))
    return [_add_cost(r) for r in res] if isinstance(res, list) else [_failed(p) for _ in ids]


def work_dir() -> Path:
    """Where the steps leave files for each other: `$LM_RESULTS` (the result folder of the run that runs the script),
    else out/ of that run, else ./workflow-out."""
    if os.environ.get("LM_RESULTS"):
        d = Path(os.environ["LM_RESULTS"])
    else:
        d = Path(os.environ["LM_RUN"], "out") if os.environ.get("LM_RUN") else Path("workflow-out")
    d.mkdir(parents=True, exist_ok=True)
    return d


def run_out(res) -> Path:
    """The out/ folder of a finished run: what that agent wrote for the next step."""
    info, _ = _lm("runs", "show", res["run_id"])
    return Path(info["path"], "out")


def spent() -> float:
    """USD used by all runs of this script so far (estimates for a subscription)."""
    return _state["spent"]


def budget(max_usd) -> None:
    """Refuse to start another run once `max_usd` is used."""
    _state["budget"] = max_usd


def resume(path) -> None:
    """Keep finished steps in this JSON file; run again after a stop and they are not repeated.  A step is found
    again by its name (`step=`, else the agent) and a hash of its inputs."""
    _state["cache"] = str(path)
    try:
        _state["data"] = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _state["data"] = {}
