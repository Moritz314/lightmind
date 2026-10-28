"""Dispatcher tests with a fake rule/secret api injected (no import of rules internals)."""
import sys
from types import SimpleNamespace

from lightmind import tools as T
from lightmind.model import Check, Decision, Run, RuleContext, SessionState
from lightmind.runs import RunDir

PY = "python"


class FakeMasker:
    def __init__(self):
        self.m = {}

    def add(self, ref, var, value):
        self.m[value] = f"<secret:{ref}:{var}>"

    def mask(self, text):
        for v, p in self.m.items():
            text = text.replace(v, p)
        return text


class FakeApi:
    """Deny set of (tool, arg); `ask` set of check tools routed to the asker."""

    def __init__(self, deny=(), ask=("new-tool",)):
        self.deny, self.ask, self.seen, self.audits = set(deny), set(ask), [], []
        self.Masker = FakeMasker
        self.Ledger = None
        self.secrets_tool = lambda home, action, ref, **kw: f"<secret:{ref}:X>"

    def build_layers(self, *a, **k):
        return ["layer"]

    def checks_for_call(self, name, args, *, tool_meta=None, hash_approved=True):
        cs = [Check("tool" if tool_meta else name, name if tool_meta else args.get("path"))]
        if tool_meta and not hash_approved:
            cs.append(Check("new-tool", name))
        return cs

    def decide(self, layers, check, ctx):
        bad = (check.tool, check.arg) in self.deny
        return Decision("deny" if bad else "allow", reason="fake")

    async def enforce(self, layers, checks, ctx, asker, session):
        self.seen.append(list(checks))
        for c in checks:
            if (c.tool, c.arg) in self.deny:
                return Decision("deny", reason=f"{c.tool}({c.arg})")
        for c in checks:
            if c.tool in self.ask:
                if asker is None or await asker("ok?", c) == "no":
                    return Decision("deny", reason="ask refused")
        return Decision("allow")

    def build_tool_env(self, home, wanted, allowed, base_env=None):
        env, uses = dict(base_env or {}), []
        for ref in wanted:
            if allowed(ref):
                env["TOK"] = "supersecret-value"
                uses.append({"secret": ref, "vars": ["TOK"], "tool": "t"})
        return SimpleNamespace(env=env, uses=uses, values=[])

    def audit(self, home, kind, record):
        self.audits.append(kind)


def add_tool(home, name, script, meta=""):
    d = home.path("tools") / name
    d.mkdir(parents=True)
    (d / "tool.md").write_text(f"---\nname: {name}\ndescription: test {name}\n"
                               f"run: {PY} run.py\n{meta}---\nbody\n", "utf-8")
    (d / "run.py").write_text(script, "utf-8")
    return d


def make(home, api=None, asker=None, agent=None, session=None, functions=True):
    """A Dispatcher.  `functions`: drop-in tools only exist as functions with `tools.as_functions: true`
    (the shipped lightmind.md says false), and most of these tests are about such tools."""
    if functions:
        home.settings = {**home.settings, "tools": {"as_functions": True}}
    run = RunDir(home, Run(id="r1", agent="a", model="sonnet"))
    api = api or FakeApi()
    ctx = RuleContext(home=str(home.root), run_dir=str(run.path))
    d = T.Dispatcher(home, agent=agent, mode="auto", session=session or SessionState(),
                     rule_ctx=ctx, run=run, asker=asker, api=api)
    return d, api, run


async def yes(q, c):
    return "yes"


ECHO = "import sys,json;print('got',json.load(sys.stdin))\n"


def test_schemas_sorted_and_dropin(home):
    d, *_ = make(home)
    names = [s["name"] for s in d.schemas()]
    assert names == sorted(names) and "bash" in names and "pdf" in names
    add_tool(home, "zed", ECHO, "params:\n  a: {type: string, required: true}\n")
    d2, *_ = make(home)
    z = next(s for s in d2.schemas() if s["name"] == "zed")
    assert z["input_schema"]["required"] == ["a"]


def test_pdf_schema_hides_kind(home):
    s = T.external_schemas([home.load_tool("pdf")])[0]
    assert "kind" not in s["input_schema"]["properties"]["file"]
    assert s["input_schema"]["required"] == ["command", "file"]


def test_hash_changes_on_edit(home):
    d = add_tool(home, "h", ECHO)
    t = home.load_tool("h")
    h1 = T.tool_hash(t)
    assert not T.hash_approved(home, t)
    T.approve_hash(home, t)
    assert T.hash_approved(home, t)
    (d / "run.py").write_text(ECHO + "# edit\n", "utf-8")
    assert T.tool_hash(t) != h1 and not T.hash_approved(home, t)


async def test_external_tool_new_tool_asks_once(home):
    add_tool(home, "echo", ECHO, "params:\n  x: {type: integer}\n")      # undeclared arguments are dropped
    calls = []

    async def asker(q, c):
        calls.append(c)
        return "yes"
    d, api, _ = make(home, asker=asker)
    r = await d.call("echo", {"x": 1})
    assert r.ok and "got {'x': 1}" in r.text
    await d.call("echo", {"x": 2})
    assert len(calls) == 1                       # approved after the first yes
    (home.path("tools") / "echo" / "run.py").write_text(ECHO + "#\n", "utf-8")
    await d.call("echo", {})
    assert len(calls) == 2                       # edited -> asks again


async def test_new_tool_refused_without_asker(home):
    add_tool(home, "echo", ECHO)
    d, *_ = make(home)
    r = await d.call("echo", {})
    assert not r.ok and r.denied_by.effect == "deny"


async def test_missing_param_and_unknown(home):
    add_tool(home, "p", ECHO, "params:\n  a: {type: string, required: true}\n")
    d, *_ = make(home, asker=yes)
    assert "missing" in (await d.call("p", {})).text
    assert "unknown" in (await d.call("nope", {})).text


async def test_path_kinds_become_read_write_checks(home, tmp_path):
    marker = tmp_path / "ran.txt"
    add_tool(home, "cp", f"open(r'{marker}','w').write('x')\nprint('ok')\n",
             "params:\n  file: {type: string, kind: read-path}\n"
             "  out: {type: string, kind: write-path}\n")
    api = FakeApi(deny={("read", "secrets/a.env")})
    d, *_ = make(home, api, asker=yes)
    r = await d.call("cp", {"file": "secrets/a.env", "out": "o.pdf"})
    assert not r.ok and r.denied_by and not marker.exists()
    r = await d.call("cp", {"file": "in.pdf", "out": "o.pdf"})
    assert r.ok and marker.exists()
    assert Check("read", "in.pdf") in api.seen[-1] and Check("write", "o.pdf") in api.seen[-1]


async def test_pdf_template_declares_kinds(home):
    t = home.load_tool("pdf")
    assert T._kind(t.params["file"]) == "read-path" and T._kind(t.params["out"]) == "write-path"
    api = FakeApi(deny={("write", "evil.pdf")})
    d, *_ = make(home, api, asker=yes)
    r = await d.call("pdf", {"command": "split", "file": "a.pdf", "out": "evil.pdf"})
    assert not r.ok and r.denied_by


async def test_long_output_goes_to_file(home):
    add_tool(home, "big", "print('\\n'.join(f'line{i}' for i in range(500)))\n")
    d, _, run = make(home, asker=yes)
    r = await d.call("big", {})
    assert r.ok and r.file and "line499" in open(r.file).read()
    assert "line39" in r.text and "line40" not in r.text and r.file in r.text


async def test_secret_injected_when_allowed_and_masked(home):
    add_tool(home, "sec", "import os;print('tok=' + os.environ.get('TOK','none'))\n",
             "secrets: [svc/main]\n")
    d, api, _ = make(home, asker=yes)
    r = await d.call("sec", {})
    assert r.text.strip() == "tok=<secret:svc/main:TOK>" and "secret_use" in api.audits
    d2, *_ = make(home, FakeApi(deny={("secret", "svc/main")}), asker=yes)
    assert "tok=none" in (await d2.call("sec", {})).text   # skipped, call still runs


async def test_required_secret_denied(home):
    add_tool(home, "sec2", "print(1)\n", "secrets: [svc/main]\nrequired_secrets: [svc/main]\n")
    d, *_ = make(home, FakeApi(deny={("secret", "svc/main")}), asker=yes)
    assert not (await d.call("sec2", {})).ok


async def test_provider_credentials_not_in_tool_env(home, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-leak")
    add_tool(home, "envt", "import os;print('ANTHROPIC_API_KEY' in os.environ)\n")
    d, *_ = make(home, asker=yes)
    assert (await d.call("envt", {})).text.strip() == "False"


async def test_builtins(home):
    d, _, run = make(home)
    assert (await d.call("write", {"path": "x/a.txt", "content": "one\ntwo\n"})).ok
    r = await d.call("read", {"path": "x/a.txt"})
    assert "2\ttwo" in r.text
    assert (await d.call("edit", {"path": "x/a.txt", "old": "two", "new": "2"})).ok
    assert not (await d.call("edit", {"path": "x/a.txt", "old": "zzz", "new": "2"})).ok
    b = await d.call("bash", {"command": f'"{sys.executable}" -c "print(6*7)"'})
    assert b.ok and "42" in b.text
    assert not (await d.call("bash", {"command": f'"{sys.executable}" -c "raise SystemExit(3)"'})).ok
    assert len((run.path / "events.jsonl").read_text().splitlines()) == 6


async def test_skill_load_claude_code_folder_and_tool(home):
    s = home.path("skills") / "cc"
    s.mkdir()
    (s / "SKILL.md").write_text("---\nname: cc\ndescription: d\n---\nFULL TEXT\n", "utf-8")
    d, *_ = make(home)
    assert "FULL TEXT" in (await d.call("skill_load", {"name": "cc"})).text
    assert "name: pdf" in (await d.call("skill_load", {"name": "pdf"})).text
    assert not (await d.call("skill_load", {"name": "nothing"})).ok


async def test_denied_builtin_returns_result_not_exception(home):
    d, *_ = make(home, FakeApi(deny={("read", "secrets/x.env")}))
    r = await d.call("read", {"path": "secrets/x.env"})
    assert not r.ok and r.denied_by.effect == "deny"


def test_loadout_follows_agent_and_session(home):
    from lightmind.model import Agent, ToolRef
    ag = Agent(name="a", tools=[ToolRef("tool", "bash"), ToolRef("tool", "pdf")])
    d, *_ = make(home, agent=ag, session=SessionState(tools_removed=["pdf"]))
    assert {s["name"] for s in d.schemas()} == {"bash", "skill_load", "ask", "todo", "spawn", "workflow", "job", "relay", "timer"}


async def test_the_dispatcher_masker_knows_every_secret_of_the_tree_from_the_start(home, monkeypatch):
    """A resumed session starts a fresh Dispatcher: a secret written to a file before must still be masked."""
    sec = home.path("secrets") / "svc"
    sec.mkdir(parents=True, exist_ok=True)
    (sec / "main.env").write_text("TOKEN=supersecretvalue123\n", "utf-8")
    monkeypatch.setenv("MY_API_KEY", "envsecretvalue456")
    d, _, _ = make(home, api=SimpleNamespace(**{**vars(FakeApi()), "Masker": __import__("lightmind.secrets", fromlist=["x"]).Masker}))
    text = d.masker.mask("a supersecretvalue123 b envsecretvalue456")
    assert "supersecretvalue123" not in text and "envsecretvalue456" not in text
    d.masker.add("later", "X", "injected-later-77")
    assert "injected-later-77" not in d.masker.mask("injected-later-77")
