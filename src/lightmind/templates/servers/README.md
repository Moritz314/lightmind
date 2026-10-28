# servers

Folder for the machines Lightmind runs on or works on: this computer and every remote server.

## Format

One folder per server with an `info.md`: frontmatter for the connection, text for purpose and rules.

```text
servers/<name>/info.md
```

| Key | Meaning |
|---|---|
| `host` | Address of the server. `local` means this machine: no SSH, nothing to set up |
| `machine` | Name of the instance that is this machine (`system/state/machine`, set by `lm sync init\|join`). The card whose `machine` equals the name of this instance is local, on every other instance it is an ssh target. Replaces `host: local`, which is still read for now (`lm check` warns in a sync repo) |
| `ssh` | Alias of the server in `~/.ssh/config` of this machine. If set, it is used instead of `host`, `user`, `port`. May be a list, `ssh: [homeserver, homeserver-vpn]`: the aliases are tried in order with `ssh -o BatchMode=yes -o ConnectTimeout=5 <alias> true` and the first one that answers is used (a single alias is used without a probe) |
| `user` | SSH user |
| `port` | SSH port, default `22` |
| `key` | Folder in `secrets/` that holds the SSH key |
| `role` | List of labels, for example `[gpu, local-models]` |
| `lm_home` | Path of the Lightmind tree on that server (default `~/lightmind`) |
| `remote` | `true` lets the phone app (Remote Control) use this server as a node: its chats are listed there and new ones can be started on it. Missing or `false` = off. Needs `lm_home` (or this machine); `lm node targets --json` shows what the gateway will start |

The text says what the server is for and what is not allowed on it, for example "no renders here". The AI reads it before it works on that server. `laptop/` is the default server (`defaults.server` in `lightmind.md`).

## Permanent session on a server

A server that has its own Lightmind tree (`lm_home`) can hold the permanent sessions. `lm remote <server>`, or choosing a remote server in the terminal UI, runs

```text
ssh -t <ssh> 'tmux new -A -s lm <lm_home>/system/lm'
```

in this terminal: it attaches to the tmux session `lm` on that server, or creates it. Detach with Ctrl+B D; the session keeps running and the next call finds it. The key stays in the ssh configuration of this machine, nothing secret goes through Lightmind. `ssh` (every entry of a list) and `lm_home` must be plain names (no spaces, no shell characters). The terminal says once what it opens, and with an `ssh` list which alias answered; if none answers the call fails. The server that is the default of the machine it runs on has `host: local`.

## Remote Control (phone app)

The gateway of the phone app (`tools/remote`) starts a node on every server with `remote: true`: `<lm_home>/system/lm node --stdio`, over ssh (on the machine of the gateway itself without ssh). Nothing else is opened on the server. The node lists the chats of that server, lets the phone read and steer the ones with Remote Control on, and starts new ones as `lm open <id> --rc <name>` in a window of the tmux session `lm` (`lm remote <server>` finds them). It needs tmux for that. A machine the gateway cannot reach by ssh (the laptop) has no `ssh` or `host`; it connects out by itself with `lm node --uplink`. The defaults for new chats from the phone are the `remote:` keys of `config.md` (`remote.enabled` switches Remote Control on in a tree; the card key `remote` picks the servers).

## Use

An agent or project names the servers it may use under `# Servers` in its `tools.md`; listing a server grants ssh access to it. Commands on a remote server pass the same permission rules as local ones.

## Add a server

1. Copy `_example/` to `<name>/`.
2. Fill in `host`, `user` and the `key` secret.
3. Write purpose and rules in the text.

Files and folders starting with `_` are ignored.
