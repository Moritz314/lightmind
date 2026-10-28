---
name: example-agent
description: Minimal example agent used by the tests
provider: ""
model: medium
effort: medium
context_limit: 200k
budget_tokens: 200k
lifetime: task
mode: auto
may_spawn: []
data: []
permissions:
  allow: []
  ask: []
  deny: []
---
You are a small general helper. Do what the task says with the listed tools, write results to your run's out/ folder, and keep the answer short.
