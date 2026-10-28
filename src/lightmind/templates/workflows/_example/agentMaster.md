---
name: my-workflow
description: One line saying what job this workflow does
provider: ""
model: ""
effort: medium
context_limit: 200k
budget:
  tokens: 600k
lifetime: task
may_spawn: [my-agent]   # agents the lead may start for single steps
permissions:
  allow: []
---

# my-workflow

Replace this text with the steps of the job, in order. Say which agent does what, what each step produces and where the result is written. End with what counts as done.

1. Read the input.
2. Start `my-agent` for each part and collect the results.
3. Write the combined result to the `out/` folder of the run and report its path.
