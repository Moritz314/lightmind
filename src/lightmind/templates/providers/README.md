# providers

Folder for the model providers: one file per account the AI can run on, such as a Claude subscription, an Anthropic API key or an OpenRouter key.

## Format

One `<account>.md` per account; the file name is the account name. Files starting with `_` are examples and ignored. Copy an example, rename it, fill it in.

Frontmatter:

| Key | Meaning |
|---|---|
| `type` | `claude-subscription`, `anthropic-api`, `openrouter`, `openai` or `openai-compatible` |
| `config_dir` | Claude subscription only: the folder with the login, relative to the tree (`system/claude/<account>`). Log in once with `lm login <account>`; the login travels with the tree |
| `secret` | API accounts: `service/account` in `secrets/` that holds the API key. Only the core reads it; it never reaches a tool |
| `admin_secret` | Optional: key for usage and balance queries |
| `base_url` | `openai-compatible`: the endpoint, for example of a local model server |
| `server` | The server the model runs on (local models) |
| `use` | Free label, for example `personal` or `customer` |
| `models` | The models of this account with their price in USD per million tokens: `input`, `output`, `cache_write`, `cache_read` |
| `default_model` | The model used when nothing else is chosen |
| `tiers` | Maps `strong`, `medium` and `cheap` to a model of this account |
| `fallback` | Accounts to continue on, in order, when this one reaches its limit |
| `alias` | Extra names for this account, so an agent can say `provider: main` |
| `switch_at` | Utilization from 0 to 1 at which the fallback starts. Default `0.9` |

The text below the frontmatter is for notes: what the account is for, how to renew it. A provider file ties a secret to an endpoint, so writing one asks the user for a confirmation.

## Use

Agents without a provider follow `defaults.provider` in `lightmind.md`: change that one line to move them all. The account is chosen in this order: switch in the chat, `provider` of the agent, `provider` of the project, `defaults.provider`. An agent with a fixed provider or model keeps it when the default changes.

Prices drive the cost figures of `lm runs`. A subscription is billed flat; its runs are priced as API equivalent and marked estimated. Check the prices at the provider before you rely on them.

## Add an account

1. Copy the example that matches the type to `<account>.md`.
2. Fill in the keys. For an API account create the key under `secrets/` (see `secrets/README.md`).
3. For a Claude subscription run `lm login <account>` once.
4. Set it as `defaults.provider`, or name it in an agent or project.

## Several accounts: a second subscription, more keys

You can have as many accounts as you like, also several of the same type. Each is its own file, with its own login or key and its own limits. `lm accounts` lists them all and says what is still missing.

**A second Claude subscription**

1. Copy `_example-claude-abo-2.md` to `claude-abo-2.md`.
2. Run `lm login claude-abo-2` and sign in with the other Claude account. Its login lands in its own folder (`config_dir: system/claude/claude-abo-2`), so it never mixes with `claude-abo`.

**Another API key**

1. Copy `_example-anthropic-api.md` to `anthropic-api-2.md` and change `secret:` to `anthropic/api-2`.
2. Run `lm secret set anthropic/api-2` and paste the key (hidden input; the key never shows in a chat). `lm secret list` shows which keys are stored (names only, never values).

**Chain them with `fallback:`**

When an account reaches its limit (`switch_at`, default 90 percent, or a rate limit or empty balance), the chat continues on the next account of its `fallback:` list. An account that is not logged in or has no key yet is skipped. Each account has its own list, so a chain is built link by link:

| File | `fallback:` |
|---|---|
| `claude-abo.md` | `[claude-abo-2, anthropic-api-1, anthropic-api-2]` |
| `claude-abo-2.md` | `[anthropic-api-1, anthropic-api-2]` |
| `anthropic-api-1.md` | `[anthropic-api-2]` |

So the order is claude-abo, then claude-abo-2, then anthropic-api-1, then anthropic-api-2. Switch by hand in a chat with `/account <name>`.
