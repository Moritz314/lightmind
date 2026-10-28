---
name: my-agent
description: One line saying what this agent is for
provider: ""          # empty = defaults.provider; or a file name from providers/, or an alias
model: ""             # empty = defaults.model; or a model of the provider, or strong | medium | cheap
effort: medium        # low | medium | high | max
context_limit: 200k   # 100k | 200k | 1m
budget:
  tokens: 400k        # per run
  usd: 2              # per run, optional
lifetime: task        # task | resident
mode: ""              # empty = defaults.mode; or auto | plan | bypass
# project: website    # default project: its memory and assets load with the agent, `-p` wins; grants nothing. A folder in projects/
may_spawn: []         # agents this agent may start
data: []              # extra files for its context, e.g. [projects/website/projectMemory.md]
permissions:          # extra rules for this agent; secrets and servers are granted in tools.md
  allow: []
  ask: []
  deny: []
---

# my-agent

Replace this text with the agent's instruction. Say what its job is, how it works, what it must never do and when it is done.
