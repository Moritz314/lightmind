---
servers: []        # machines that may hold this secret; empty = this machine only
---

# example

Purpose of the service and how its secrets are used. The AI reads this file, so keep it free of values.

- Hosts: where the service lives, for example `smtp.example.com:587`
- Accounts: one `<account>.env` per account, for example `main.env`
- Rules: when the secret may be used and what needs the user's approval

## Format of every .env file here

Comment lines describe the account, the line `# --- END HEADER ---` ends them, then come the variables. The names are fixed for this service: `EXAMPLE_KEY`.

```text
# Account: main
# Purpose: what this account is used for
# --- END HEADER ---
EXAMPLE_KEY=<value, typed by hand>
```
