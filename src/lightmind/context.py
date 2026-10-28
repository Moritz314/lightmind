"""Context builder: Tools + Instructions + Data -> one byte-stable prefix + volatile state.

The prefix (cache-stable) is, in the order of `context.order` in lightmind.md: the master prompt
(lightmind.md body), the instruction of the active mode, the agent body, the project data, the skill
index (name, description, path), skills that are always inlined.  Permission rules are NOT part of
it: the core enforces them, the AI only gets the mode instruction.  Everything volatile (time,
server, granted secrets, the outputs of `on_start` commands marked `context: true`) goes into the
state block after the cache line.

May import: model, files.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from .files import Home
from .model import Agent, Message, SessionState, Skill, Task, Tool

BREAKPOINT = "--breakpoint--"
DEFAULT_ORDER = ["lightmind", "mode", "agent", "project", "skill_index", "required_skills",
                 BREAKPOINT, "state", "task"]
_DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


@dataclass
class Context:
    blocks: list[tuple[str, str]] = field(default_factory=list)   # (name, text) above breakpoint
    state: str = ""                                               # volatile block
    tool_schemas: list[dict[str, Any]] = field(default_factory=list)
    skills: list[Skill] = field(default_factory=list)             # loadout (index + skill_load)

    @property
    def prefix(self) -> str:
        """'\\n\\n'.join of block texts; byte-identical for identical inputs."""
        return "\n\n".join(t for _, t in self.blocks)


def context_settings(home: Home) -> dict[str, Any]:
    """The `context:` section of lightmind.md (order, handover_at, tool_output_inline, ...)."""
    return dict(home.settings.get("context") or {})


def handover_at(home: Home) -> float:
    """Fraction of context_limit at which a handover starts (`context.handover_at`)."""
    return float(context_settings(home).get("handover_at", 0.7))


def _where(s: Skill, root: Path | None) -> str:
    if not s.path:
        return ""
    p = Path(s.path)
    if root is not None:
        try:
            p = p.relative_to(root)
        except ValueError:
            pass
    return f" ({p.as_posix()})"


def skill_index(skills: list[Skill], mode: str = "oneline", root: Path | None = None) -> str:
    """'- name: description (path)' per skill, sorted by name; the path is relative to `root`
    when given and the skill lies inside it, and left out when the skill has none.  '' when
    mode == 'off'."""
    if mode == "off":
        return ""
    return "\n".join(f"- {s.name}: {' '.join(s.description.split())}{_where(s, root)}"
                     for s in sorted(skills, key=lambda s: s.name))


def preload_skills(skills: list[Skill], task_text: str, max_tokens: int) -> list[Skill]:
    """No-tool models: skills whose preload_when regex matches task_text (case-insens.),
    sorted by name, up to max_tokens (4 chars ~ 1 token)."""
    out, used = [], 0
    for s in sorted(skills, key=lambda s: s.name):
        if s.preload_when and re.search(s.preload_when, task_text, re.I):
            cost = len(s.body) // 4 + 1
            if used + cost > max_tokens:
                break
            out.append(s)
            used += cost
    return out


def _read(home: Home, rel: Any) -> str:
    try:
        return home.doc(rel).body.strip()
    except OSError:
        return ""


def _task_text(task: Task) -> str:
    parts = [f"# Task\n{task.goal}"]
    for label, v in (("Limits", ", ".join(f"{k}: {x}" for k, x in task.limits.items())),
                     ("Sources", ", ".join(task.sources)), ("Check", task.check),
                     ("Return", task.return_)):
        if v:
            parts.append(f"{label}: {v}")
    return "\n".join(parts)


def state_block(home: Home, *, server: str = "", now: datetime | None = None,
                granted_secrets: list[str] | None = None,
                state_info: dict[str, Any] | None = None) -> str:
    """The volatile block: time, server, granted secrets, then the facts of `state_info` (a key of
    the same name replaces ours; an empty value is left out).  The session passes the `on_start`
    outputs marked `context: true` as `start`."""
    now = now or datetime.now()
    facts: dict[str, Any] = {"time": f"{now:%Y-%m-%d %H:%M} {_DAYS[now.weekday()]}",
                             "server": server or str(home.defaults.get("server", ""))}
    if granted_secrets is not None:
        facts["secrets granted"] = ", ".join(sorted(granted_secrets)) or "none"
    facts.update(state_info or {})
    return "State\n" + "\n".join(f"{k}: {v}" for k, v in facts.items() if v != "")


def build_context(home: Home, *, agent: Agent | None, mode: str, task: Task | None,
                  session: SessionState, project: str = "", rules_text: str = "",
                  tools_available: list[Tool] | None = None,
                  builtin_schemas: list[dict[str, Any]] | None = None,
                  state_info: dict[str, Any] | None = None,
                  granted_secrets: list[str] | None = None,
                  server: str = "", now: datetime | None = None) -> Context:
    """Assemble blocks in the order of `context.order` (lightmind.md): master prompt | mode
    instruction | agent body | project data | skill index | required skills, then the breakpoint and
    the state block (time, server, granted secrets and the facts of `state_info`).

    Permission rules are not sent; `rules_text` only appears if "rules" is put into `order`.
    `builtin_schemas` = the complete schema list the dispatcher offers; it travels natively in
    `Context.tool_schemas`, so the `tools` block adds no prefix text.  `[]` marks a no-tool model:
    matching skills are then preloaded into the required_skills block.  `tools_available` is
    accepted for symmetry only."""
    cfg = context_settings(home)
    order = list(cfg.get("order") or DEFAULT_ORDER)
    # skills are always on: the index lists every skill of the tree; neither an agent's `tools.md` nor
    # `tools_off` narrows it (those may still preload or require some)
    found = home.list_skills()
    skills = sorted(found, key=lambda s: s.name)
    required = [s for s in found if s.name in (cfg.get("required_skills") or [])]

    def mode_text() -> str:
        return str((home.modes().get(mode) or {}).get("text", "")).strip()

    def data_files() -> list[str]:
        out: list[str] = []
        if project:
            pd = home.path("projects") / project
            out += [str(pd / home.filename(k)) for k in ("project_memory", "project_assets")]
        return out + (agent.data if agent else []) + session.data_added

    def req_skills() -> str:
        text = "\n\n".join(f"## Skill: {s.name}\n{s.body.strip()}" for s in required)
        if builtin_schemas == [] and task:
            pre = preload_skills([s for s in skills if s not in required], task.goal,
                                 int(cfg.get("preload_max_tokens", 8000)))
            text += "".join(f"\n\n## Skill: {s.name}\n{s.body.strip()}" for s in pre)
        return text.strip()

    project_blocks = [f"## Data: {f}\n{t}" for f in data_files() if (t := _read(home, f))]
    mode_idx = cfg.get("skill_index", "oneline")        # YAML reads a bare `off` as false
    idx = skill_index(skills, "off" if mode_idx is False or str(mode_idx).lower() in ("off", "false", "no", "none")
                      else "oneline", home.root)
    texts = {
        "lightmind": home.master_prompt(),
        "rules": rules_text.strip(),
        "mode": f"Mode: {mode}\n{mode_text()}".strip() if mode else "",
        "agent": agent.body.strip() if agent else "",
        "tools": "",
        "skill_index": f"Skills (read the SKILL.md at the path before you use one):\n{idx}" if idx else "",
        "required_skills": req_skills(),
        "project": "\n\n".join(project_blocks),
    }
    ctx = Context(skills=skills)
    ctx.tool_schemas = sorted(builtin_schemas or [], key=lambda s: s["name"])
    pre_bp = True
    for name in order:
        if name == BREAKPOINT:
            pre_bp = False
        elif pre_bp and name in texts and texts[name]:
            ctx.blocks.append((name, texts[name]))
        elif not pre_bp and name == "state":
            ctx.state = state_block(home, server=server, now=now, granted_secrets=granted_secrets,
                                    state_info=state_info)
    return ctx


def handover_needed(input_tokens: int, context_limit: int, handover_at: float) -> bool:
    """True when input_tokens >= handover_at * context_limit (checked between turns)."""
    return input_tokens >= handover_at * context_limit


def handover_instruction() -> str:
    """The user message asking for '## Handover' in the run log."""
    return ("The context is nearly full. Write a `## Handover` section at the end of log.md "
            "(done / open / next, with file paths), then stop. Do not start new work.")


def handover_task(previous_log: str, original: Task) -> Task:
    """Task for the restarted run: same goal + the handover text."""
    i = previous_log.rfind("## Handover")
    text = re.sub(r"<!--\s*lm:end[^>]*-->", "", previous_log[i:] if i >= 0 else previous_log).strip()
    return Task(goal=f"{original.goal}\n\nContinue a previous run. Its handover:\n{text}",
                limits=dict(original.limits), sources=list(original.sources),
                check=original.check, return_=original.return_, parent=original.parent)


def initial_messages(ctx: Context, task: Task | None, user_text: str) -> list[Message]:
    parts = [p for p in (_task_text(task) if task else "", user_text.strip()) if p]
    return [Message(role="user", text="\n\n".join(parts))] if parts else []


# ----------------------------------------------------------------------------- skills as slash commands
def skill_commands(home: Home, reserved=()) -> dict[str, Skill]:
    """Slash command name -> skill: `name` (lower case), or `skill:<name>` when it collides with a built-in
    command (or a `ui.slash` row) in `reserved` or is not the first skill of that name."""
    taken = {str(r).lstrip("/").lower() for r in reserved} | {"skill"}
    out: dict[str, Skill] = {}
    for s in sorted(home.list_skills(), key=lambda s: s.name):
        n = s.name.lower().replace(" ", "-")
        out[f"skill:{n}" if n in taken or n in out else n] = s
    return out


def skill_message(home: Home, skill: Skill, request: str = "") -> str:
    """The user message of `/<skill> <request>`: an instruction to use the skill, then the request.  A skill
    small enough for `context.preload_max_tokens` is included as its body, any other by the path of its
    SKILL.md (read first)."""
    request = request.strip()
    if len(skill.body) // 4 + 1 <= int(context_settings(home).get("preload_max_tokens", 8000)):
        head = (f'Use the skill "{skill.name}" for this request; its text follows.\n\n'
                f'<skill name="{skill.name}">\n{skill.body.strip()}\n</skill>')
    else:
        head = f'Use the skill "{skill.name}" (read {skill.path} first).'
    return f"{head}\n\n{request}" if request else head
