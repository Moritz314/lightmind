---
type: claude-subscription
config_dir: system/claude/claude-abo   # the login lives inside the tree; log in once with: lm login claude-abo
use: personal
default_model: sonnet
tiers: {strong: opus, medium: sonnet, cheap: haiku}
models:                              # USD per million tokens. CHECK PRICES before you rely on them
  opus:   {input: 15, output: 75, cache_write: 18.75, cache_read: 1.50}
  sonnet: {input: 3,  output: 15, cache_write: 3.75,  cache_read: 0.30}
  haiku:  {input: 1,  output: 5,  cache_write: 1.25,  cache_read: 0.10}
fallback: []
switch_at: 0.9
---

# claude-abo

A Claude subscription. The AI runs through the Claude Agent SDK on the login stored in `config_dir`. Log in once with `lm login claude-abo`; the login stays in the tree, so a copy of the tree works on another machine.

A subscription is billed flat. The prices above are API equivalents, used only to estimate the cost of a run, which is then marked estimated. Check prices: the figures are placeholders, not verified against the current price list (current API prices: see `_example-anthropic-api.md`).

A second subscription: copy `_example-claude-abo-2.md` to `claude-abo-2.md`, run `lm login claude-abo-2`, and list it under `fallback:` here (see `README.md` in this folder).
