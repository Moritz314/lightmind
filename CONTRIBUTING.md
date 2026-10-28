# Contributing

Thanks for helping. Never paste API keys, tokens or `.env` contents into an issue, a pull request or a log excerpt.

## Report a bug

Open an issue with the bug report form. Please include:

- Version: `lm --version` (or `uv tool list`, or `pip show lightmind`)
- Operating system and Python version
- Steps to reproduce, what you expected, what happened
- A log excerpt with all secrets removed

Questions go to [Discussions](https://github.com/Moritz314/lightmind/discussions), not Issues.

## Propose a feature

Open an issue with the feature request form. Describe the problem first, then your idea. Small, focused proposals are easier to accept.

## Pull requests

- Keep them small and about one thing.
- Add or update tests.
- `uv sync --extra dev --extra tui` and `uv run pytest -q` must be green on your OS.
- Describe what changes and why.

## Contribute a skill

Skills live in [lightmind-skills](https://github.com/Moritz314/lightmind-skills), not in this repo.

- One folder `skills/<name>/` with a `SKILL.md`; its frontmatter has `name` and `description`.
- Scripts go into `scripts/`, with PEP 723 inline dependencies, run via `uv run`.
- No secrets and no personal paths.
- Write everything in English.

## License of contributions

By contributing you agree that your contribution is licensed under the Apache License 2.0, the same as the project (inbound = outbound). The name "Lightmind" and the logos are not licensed, see NOTICE.
