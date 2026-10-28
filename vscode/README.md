# Lightmind for VS Code

Side bar chat for Lightmind. Starts `lm serve --stdio` from your Lightmind tree and talks protocol v1 to it.

## Install

```
code --install-extension lightmind.vsix
```

Then open the Lightmind icon in the activity bar. Needs a trusted workspace folder.

- Setting `lightmind.home`: Lightmind tree, default `~/lightmind` (with Remote-SSH: the path on the remote machine).
- Setting `lightmind.follow`: open the file the agent works on and flash changed lines.
- Commands: New session, Open session, Settings (opens `config.md`), Clear marks.

## Build

```
npm ci
npm test        # typecheck, build, unit tests, headless Chrome test (CHROME=/path if needed)
npm run package # lightmind.vsix
```
