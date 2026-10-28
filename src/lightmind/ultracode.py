"""The effort level `ultracode`: effort max for the model plus the duty to orchestrate substantial work with `workflow`,
and the settings of the `workflow:` section of config.md that tune it.

Pure data and small functions, so the UI, the settings page, the slash command and the loop can use them without importing
the workflow runtime (orchestrate.py).  May import: util.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .util import setting, to_int

ULTRACODE = "ultracode"
MODEL_EFFORT = "max"           # what the provider gets: no backend knows the word `ultracode`
FALLBACK = "max"               # where `/ultracode off` goes when the chat does not remember its earlier effort

# Shown on the settings page and in /effort beside the word.
LABEL = "max + every substantial task as a workflow"

# What a workflow agent may get as its effort (`ultracode` itself is a level of the chat, not of an agent).
AGENT_EFFORTS = ("low", "medium", "high", "xhigh", "max")
NOTIFY_WORDS = ("off", "ntfy", "telegram", "both")

# key of `workflow:` -> (default, lowest, highest).  The one list the runtime clamps to and the settings page checks against
# (tests compare both); a hand-edited value outside it is clamped, one that is no number is the default.
BOUNDS: dict[str, tuple[int, int, int]] = {
    "max_agents": (1000, 1, 10_000),       # agents per run: a guard against endless loops in a script
    "max_items": (4096, 1, 65_536),        # elements per parallel() / pipeline()
    "schema_retries": (3, 0, 10),          # fresh tries when an answer does not fit its schema
    "skeptics": (3, 1, 9),                 # sceptics of an adversarial verify (goes into the system text)
    "dry_rounds": (2, 1, 10),              # empty rounds that end a loop-until-dry (goes into the system text)
    "notify_after": (10, 1, 1440),         # minutes a workflow must have run before its end is reported
    "budget": (0, 10_000, 1_000_000_000),  # tokens per workflow, 0 = none
}

# One block of the system text while the chat is on ultracode (Session._build_ctx).  English like every prompt of
# the core; the patterns are fixed here.  {skeptics} and {dry_rounds} are filled from config.md.
TEXT = """Effort: ultracode
You are in ultracode. Correctness and thoroughness come first; token cost is not a criterion.
- Orchestrate every substantial task through the `workflow` tool: write a workflow script (see the skill `ultracode`) instead of doing the work yourself. Run big tasks as several workflows one after another (understand, design, build, verify); read each result before you decide the next workflow.
- Trivial requests and plain conversation stay solo: answer them yourself.
- Verify adversarially. Compose these patterns as the task needs them:
  * adversarial verify: {skeptics} independent sceptics try to refute a finding; it survives only with a majority.
  * perspectives: reviewers with different angles (correctness, security, performance, reproducibility).
  * judge panel: N drafts from different directions, judged in parallel, then a synthesis of the winner and the best ideas of the others.
  * loop-until-dry: keep starting searchers until {dry_rounds} rounds in a row bring nothing new; compare against everything seen, not only against what was confirmed.
  * multi-modal sweep: search in parallel along different routes.
  * completeness critic: a last agent asks what is missing; that becomes the next round.
  * no silent caps: whatever is left out is logged with log().
- Heavy work belongs on a server, never on the laptop; the runtime throttles agents on the laptop by itself."""

# The word on its own, not part of a path or a longer word (`skills/ultracode/`, `ultracode.py` do not count).
_WORD = re.compile(r"(?<![\w/\\.-])ultracode(?![\w/\\-]|\.\w)", re.I)
_BUDGET = re.compile(r"\+?\s*(\d+(?:[.,]\d+)?)\s*([km]?)", re.I)
_MODEL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/@\[\]+-]{0,79}")


def model_effort(effort: str) -> str:
    """The effort a provider gets: `max` for ultracode, everything else unchanged.  `effort`: the chat's effort word."""
    return MODEL_EFFORT if (effort or "").strip().lower() == ULTRACODE else effort


def is_on(effort: str) -> bool:
    """Does `effort` (the chat's effort word) mean ultracode?"""
    return (effort or "").strip().lower() == ULTRACODE


def mentioned(text: str) -> bool:
    """Does a message name the keyword `ultracode` as a word of its own?  `text`: the message as typed."""
    return bool(_WORD.search(text or ""))


def spans(text: str) -> list[tuple[int, int]]:
    """Where a message names the keyword: (start, end) offsets of every hit, by the same rule as `mentioned`, so the
    chat colours exactly the words that switch ultracode on.  `text`: the message as typed."""
    return [m.span() for m in _WORD.finditer(text or "")]


def parse_budget(text: Any) -> int | None:
    """Tokens of a budget word: `+500k` -> 500000, `2m`, `1.5M`, `800000`; None for nothing or an unreadable word."""
    if isinstance(text, (int, float)) and not isinstance(text, bool):
        return int(text) if text > 0 else None
    m = _BUDGET.fullmatch(str(text or "").strip())
    if not m:
        return None
    n = float(m.group(1).replace(",", "."))
    n *= {"k": 1_000, "m": 1_000_000}.get(m.group(2).lower(), 1)
    return int(n) if n > 0 else None


@dataclass(frozen=True)
class Config:
    """The settings of the `workflow:` section that are not about CPU and RAM (those are orchestrate.max_parallel and
    memory_ok), as the runtime uses them.  `budget`: tokens per workflow, None = none.  `model` empty: as spawn / the chat."""
    max_agents: int
    max_items: int
    budget: int | None
    model: str
    effort: str
    schema_retries: int
    skeptics: int
    dry_rounds: int
    tidy_worktrees: bool
    notify: str
    notify_after: int


def _clamped(home: Any, key: str, default: int) -> int:
    _, low, high = BOUNDS[key]
    return max(low, min(high, to_int(setting(home, "workflow." + key), default)))


def read(home: Any, **defaults: int) -> Config:
    """The `workflow:` settings of config.md, every value checked and clamped.  `defaults` replaces the built-in default of
    a numeric key (orchestrate.py passes its module constants, so a test can move them)."""
    def number(key: str) -> int:
        return _clamped(home, key, defaults.get(key, BOUNDS[key][0]))
    effort = str(setting(home, "workflow.effort") or "").strip().lower()
    effort = MODEL_EFFORT if effort == ULTRACODE else effort
    model = str(setting(home, "workflow.model") or "").strip()
    notify = str(setting(home, "workflow.notify") or "off").strip().lower()
    budget = parse_budget(setting(home, "workflow.budget"))
    return Config(
        max_agents=number("max_agents"), max_items=number("max_items"),
        budget=None if budget is None else max(BOUNDS["budget"][1], min(BOUNDS["budget"][2], budget)),
        model=model if _MODEL.fullmatch(model) else "",
        effort=effort if effort in AGENT_EFFORTS else MODEL_EFFORT,
        schema_retries=number("schema_retries"), skeptics=number("skeptics"), dry_rounds=number("dry_rounds"),
        tidy_worktrees=setting(home, "workflow.tidy_worktrees") is not False,
        notify=notify if notify in NOTIFY_WORDS else "off", notify_after=number("notify_after"))


def system_text(cfg: Config) -> str:
    """The ultracode block of the system text with the sceptics and dry rounds of `cfg`."""
    return TEXT.replace("{skeptics}", str(cfg.skeptics)).replace("{dry_rounds}", str(cfg.dry_rounds))


def limits_line(cfg: Config, parallel: int) -> str:
    """The limits a workflow runs under, in one line (the start note of a workflow).  `parallel`: agents at once."""
    return (f"limits: {parallel} agents at once, {cfg.max_agents} per run, {cfg.max_items} items per parallel/pipeline, "
            f"budget {cfg.budget or 'none'} tokens, agent model {cfg.model or 'as the chat'}, agent effort {cfg.effort}, "
            f"{cfg.schema_retries} schema retries")


def status(effort: str, before: str, cfg: Config, parallel: int, extra: dict[str, Any]) -> str:
    """`/ultracode status`: whether the level is on and the settings that apply, as a short list.  `effort`: the chat's
    effort word; `before`: the effort `/ultracode off` goes back to; `parallel`: agents at once now; `extra`: label ->
    value of the settings the runtime reads on its own (confirm, keyword, RAM)."""
    on = is_on(effort)
    rows = [f"ultracode: {'on' if on else 'off'}  (effort {effort}" + (f", off goes back to {before or FALLBACK})" if on else ")"),
            f"agents: {parallel} at once, at most {cfg.max_agents} per run, {cfg.max_items} items per parallel/pipeline",
            f"budget: {cfg.budget or 'none'} tokens per workflow",
            f"agent model: {cfg.model or 'as spawn / the chat'}, agent effort: {cfg.effort}",
            f"schema retries: {cfg.schema_retries}, sceptics: {cfg.skeptics}, empty rounds: {cfg.dry_rounds}",
            f"unchanged worktrees are removed: {'yes' if cfg.tidy_worktrees else 'no'}",
            "report a finished workflow: " + (f"{cfg.notify} after {cfg.notify_after} min" if cfg.notify != "off" else "no")]
    rows += [f"{k}: {v}" for k, v in extra.items()]
    return "\n".join(rows)
