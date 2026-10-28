---
type: claude-subscription
config_dir: system/claude/claude-abo-2   # its own login folder; log in once with: lm login claude-abo-2
use: personal
default_model: sonnet
tiers: {strong: opus, medium: sonnet, cheap: haiku}
models:                              # USD per million tokens, API equivalent (flat-rate subscription: used for estimates only)
  opus:   {input: 4,   output: 20,  cache_write: 5,     cache_read: 0.20}
  sonnet: {input: 2,   output: 10,  cache_write: 2.50,  cache_read: 0.20}
  haiku:  {input: 0.10, output: 0.50, cache_write: 0.125, cache_read: 0.01}
fallback: [anthropic-api-1, anthropic-api-2]   # where to continue when this subscription reaches its limit
switch_at: 0.9
---

# claude-abo-2

A second Claude subscription, next to `claude-abo`. This file is only an example (the name starts with `_`), so it is not active.

## Switch it on

1. Copy this file to `claude-abo-2.md` in the same folder (the file name is the account name).
2. Log in once: `lm login claude-abo-2`. The browser opens, you sign in with the OTHER Claude account. The login is stored in `system/claude/claude-abo-2`, separate from `claude-abo`, so each account keeps its own login and its own 5-hour and weekly limits.
3. Use it: `/account claude-abo-2` in a chat, or let `claude-abo` continue on it: put `claude-abo-2` first in the `fallback:` list of `claude-abo.md`.

Check with `lm accounts`: it says `not logged in: lm login claude-abo-2` until step 2 is done.
