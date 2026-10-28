---
type: openrouter
secret: openrouter/main        # secrets/openrouter/main.env with OPENROUTER_API_KEY; only the core reads it
admin_secret: ""               # optional: management key for balance queries
base_url: https://openrouter.ai/api/v1
referer: ""                    # optional: shown on openrouter.ai (HTTP-Referer)
title: Lightmind               # optional: shown on openrouter.ai (X-Title)
use: personal
default_model: anthropic/claude-sonnet-5.5
tiers: {strong: anthropic/claude-opus-5.5, medium: anthropic/claude-sonnet-5.5, cheap: google/gemini-2.5-flash}
models:                        # USD per million tokens. Copy the prices from openrouter.ai/models; CHECK PRICES
  anthropic/claude-opus-5.5:   {input: 4, output: 20, cache_write: 5,    cache_read: 0.20}
  anthropic/claude-sonnet-5.5: {input: 2, output: 10, cache_write: 2.50, cache_read: 0.20}
  google/gemini-2.5-flash:     {input: 0.30, output: 2.50, cache_write: 0.30, cache_read: 0.075}
fallback: []
switch_at: 0.9
---

# openrouter

OpenRouter gives access to many models with one key and a prepaid balance. This file is only an example (the name starts with `_`), so it is not active. The same type also serves local model servers: see the end.

## Switch it on

1. Copy this file to `openrouter.md` in the same folder (the file name is the account name).
2. Put the key in: the `secrets` tool of `lm` or by hand in `secrets/openrouter/main.env`, as described in `secrets/README.md`: a short header, the line `# --- END HEADER ---` and the line `OPENROUTER_API_KEY=<your key>`. The key is read by the core only and never handed to a tool or shown to the AI.
3. Use it: `/account openrouter` in a chat, or `provider: openrouter` in a project or agent, or `defaults.provider` in `lightmind.md`.

If the key is missing, the chat says which secret to create.

## Notes

Model names are OpenRouter's (`vendor/model`). Add every model you use under `models:` with its price, otherwise runs on it are not costed. The three tiers point at one model each; change them here. Models without tool use are supported: the core then preloads the skills whose `preload_when` matches the task.

## A local model (Ollama, LM Studio)

Same type works without a key: copy this file to `local.md`, set `type: openai-compatible`, `base_url: http://localhost:11434/v1` (Ollama) or `http://localhost:1234/v1` (LM Studio), remove `secret:`, and list the local model under `models:` with prices of 0 and as `default_model`.
