# vault

Folder for the vault: the knowledge base, a set of Markdown notes that the AI searches before it asks the user for context.

## Where the vault lives

The place is `paths.vault` in `lightmind.md`. The default is this folder, `vault/`, so the tree stays in one piece and can be moved.

To use an existing vault repo, clone it into this folder and point the setting at the clone:

```bash
git clone https://example.com/me/notes.git vault/notes
```

```yaml
paths:
  vault: vault/notes/
```

`lm init --vault-repo <url>` does both when it creates a tree. The clone is a separate git repo: this `README.md` stays beside it and never goes into it, and the `.gitignore` of the tree skips it. A vault outside the tree works as well (`vault: C:/Users/me/notes`), but then the tree is no longer self-contained.

## Session start

`on_start` in `lightmind.md` runs `tools/vault/status.py`. It pulls the vault (`git pull --ff-only`) and prints one line, for example `Vault connected · 368 notes · main · clean`. The same line goes into the state block of the AI.

## Use

- Search the vault before you ask the user for context.
- The vault skill defines which notes to write and when. Without that skill, write nothing into the vault.
- Keep secrets out of the vault.
