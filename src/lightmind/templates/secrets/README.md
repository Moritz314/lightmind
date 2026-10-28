# secrets

Folder for secrets: API keys, passwords and tokens. The AI learns how a secret is used from `info.md` and never sees the values.

## Format

One folder per service, one `.env` file per account.

```text
secrets/<service>/
├─ info.md          readable: purpose, hosts, accounts, rules, variable names
└─ <account>.env    the values: local only, never committed, never read by the AI
```

`<account>.env` starts with comment lines that describe the account, then the line `# --- END HEADER ---`, then `NAME=value` lines. Every account of a service uses the same variable names, for example `MAIL_USER` and `MAIL_PASS`.

`info.md` frontmatter: `servers` lists the machines that may hold this secret; empty means this machine only.

## Rules

- The AI reads `info.md` only. Reading or writing a `.env` file is denied in every mode by `permissions.md`.
- The core injects a secret as environment variables into a tool process when it is granted: under `# Secrets` in the `tools.md` of the agent or project, or by a rule such as `allow: secret(<service>/<account>)`.
- Files that grant secrets (`tools.md` of agents, workflows and projects, `agentMaster.md`, `lightmind.md`, `permissions.md`, the files in `providers/`) and the scripts the hooks run (`tools/vault/`) ask the user before every write, so an agent cannot grant itself a secret.
- The logins and API keys of `providers/` are read by the core itself and never reach a tool.
- If a value ever shows up in the chat, tell the user at once which secret and where.
- Never write a value into any other file, command line or message.

## Add a secret

1. Copy `_example/` to `<service>/` and fill `info.md`.
2. Create `<account>.env` by hand, or with the `secrets` tool where the agent holds a `secret-write` grant.
3. Grant it to the agent or project that needs it.

Folders starting with `_` are ignored.
