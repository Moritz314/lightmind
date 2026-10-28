---
type: anthropic-api
secret: anthropic/api-1        # secrets/anthropic/api-1.env with ANTHROPIC_API_KEY; only the core reads it
admin_secret: ""               # optional: admin key for usage queries, same form
base_url: ""                   # empty = https://api.anthropic.com; set it only for a proxy
max_tokens: 32000              # upper limit of one answer
use: personal                  # personal | customer
default_model: claude-sonnet-5-5
tiers: {strong: claude-opus-5-5, medium: claude-sonnet-5-5, cheap: claude-haiku-5-5}
models:                        # USD per million tokens. CHECK PRICES before you rely on them
  claude-opus-5-5:   {input: 4,   output: 20,  cache_write: 5,     cache_read: 0.20}
  claude-sonnet-5-5: {input: 2,   output: 10,  cache_write: 2.50,  cache_read: 0.20}
  claude-haiku-5-5:  {input: 0.10, output: 0.50, cache_write: 0.125, cache_read: 0.01}
  claude-fable-5-1:  {input: 10,  output: 50,  cache_write: 12.50, cache_read: 0.25}
fallback: []                   # e.g. [anthropic-api-2]: continue on another key when this one is limited
switch_at: 0.9
---

# anthropic-api

An Anthropic API key, billed by use. This file is only an example (the name starts with `_`), so it is not active.

## Several keys

One key = one provider file + one secret. The pattern, for as many keys as you like:

| Account file | `secret:` | Key file (made by `lm secret set`) |
|---|---|---|
| `anthropic-api-1.md` | `anthropic/api-1` | `secrets/anthropic/api-1.env` |
| `anthropic-api-2.md` | `anthropic/api-2` | `secrets/anthropic/api-2.env` |

Copy this file once per key, change `secret:` in each copy, and chain them with `fallback:` (`anthropic-api-1` has `fallback: [anthropic-api-2]`). Each account uses only its own key.

## Switch it on

1. Copy this file to `anthropic-api-1.md` in the same folder (the file name is the account name).
2. Store the key without it ever showing up in a chat: run `lm secret set anthropic/api-1` and paste the key. The input is hidden and the value is never printed. This writes `secrets/anthropic/api-1.env` (a short header, the line `# --- END HEADER ---` and the line `ANTHROPIC_API_KEY=<your key>`). The key is read by the core only and never handed to a tool or shown to the AI.
3. Use it: `/account anthropic-api-1` in a chat, or `provider: anthropic-api-1` in a project or agent, or `defaults.provider` in `lightmind.md`.

`lm accounts` shows `key missing: lm secret set anthropic/api-1` until step 2 is done.

## Notes

Model names are the full API names. The tiers (`strong`, `medium`, `cheap`) point at the three models above; change them here when a new model comes out. Prices drive the cost figures of `lm runs`; the numbers above are a snapshot of the price list, check them.

Effort is passed on to the models that support it (Claude 4.6 and newer). Prompt caching is on: the master prompt and the tools are read from the cache on every later turn, which shows as cheaper `cache_read` tokens.

Use this account for customer work that a subscription does not cover.
