---
# All permissions, limits and modes live here. The core only reads and applies them.
# Patterns use placeholders resolved from lightmind.md: {secrets}, {state}, {audit}, {agents}, ...
# = any `paths:` key, {file_agent} = a `files:` key, {entry} = lightmind.md itself,
# {run} / {project} = the current run and project folder. Rename a folder in lightmind.md and
# every rule follows. A write rule also governs edit.
fallback: deny
allow:
  - read(**)
  - write({projects}/**)
  - write({vault}/**)
  - write({skills}/**)
  - write({tools}/**)        # agents may build their own tools
  - write({run}/**)
  - skill(*)
  - web(*)
  - tool(*)
ask:
  - new-tool(*)              # first use of a new or changed tool asks once
  - bash(git push*)
deny:
  - bash(rm -rf /*)
locked:
  deny:
    - read({secrets}/**/*.env)
    - bash(*{secrets}/*.env*)   # catches the obvious ways, not every way
    - read({state}/**)
    - write({state}/**)
    # Files that define where things live and what is allowed. Remove a line to hand one to agents.
    - write({entry})
    - write({permissions})
    - write({config})
    - write({context})
    - write({providers})
    - write({prices})
    - write({agents}/*/{file_agent})
    - write({audit}/**)
modes:
  auto:
    label: Auto
    color: green
    locked:
      ask: [category(send), category(money), category(delete), category(deploy)]
    text: Work on the task without asking for routine steps. Rules decide when the user is asked.
  plan:
    label: Plan
    color: blue
    allow: [read(**), web(*), skill(*)]
    ask: [bash(*)]
    locked:
      deny: [write(**), edit(**), category(send), category(money), category(delete), category(deploy), category(account), pc-control(*)]
    text: Read, research and write a plan as your final answer. Change nothing. When the plan is ready, stop and ask the user to switch mode.
  bypass:
    label: Bypass
    color: red
    allow: ["*"]
    text: Act without asking. The locked defaults still apply.
audit: { secret_use: on, secret_write: on, pc: on, browser: on, send: on, money: on, delete: on, deploy: on, screenshots: thumb }
money_limit: 20 USD/day
service_token: lm-services/main   # secret for lmd, pcd, browserd; "off" disables the token check
sandbox: off                       # reserved for a later OS sandbox
---
# Rules
- Never bring a secret value into the chat. If it happens anyway, tell the user at once which secret it was and where it appeared.
- For secrets, only read `secrets/<name>/info.md` to learn how to use them. Use them through tools that get them injected.
- Do not try to get around a deny or an ask by other means. If a rule blocks your task, stop and say which rule.
