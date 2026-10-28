# system

The technical engine of Lightmind: runtime, Python, caches, the uv program, Claude logins and the state of the core. You never need to touch this folder. To rebuild the engine, delete the `runtimes` folder (and an old `runtime-*` folder, if there is one) and start `lm`. Do not delete `claude/` and `state/`: they hold your Claude logins, the state of the core and the money ledger.

## What is in it

| Folder or file | Holds |
|---|---|
| `state/` | locks, the money ledger, approved tool hashes, tokens, the record of shipped templates, `install-source.txt`, `runtime-stamp.txt` |
| `runtimes/<os>-<arch>/rN/` | Lightmind itself: one folder per build, several side by side; `runtimes/<os>-<arch>.current` names the build that new windows use |
| `python/`, `cache/`, `bin/` | Python, the uv cache with the dependencies of tool scripts, the uv program |
| `wheels/`, `uv-tools-<os>-<arch>/` | the kept copy of the Lightmind wheel, tools installed with `uv tool` |
| `claude/<account>/` | the login of a Claude subscription account |
| `browsers/` | browsers for the browser tools |
| `lm`, `lm.cmd`, `lightmind`, `lightmind.cmd`, `lmq`, `lmq.cmd`, `lmr`, `lmr.cmd` | launchers for Linux or macOS and for Windows (`lightmind` is the same as `lm`, `lmq` the same as `lm q`: a quick chat, `lmr` the same as `lm resume`); `lm path` puts this folder on your PATH |

## How the runtime is built

The launcher takes uv from `bin/` (a copy of the one on your PATH, else downloaded), installs Python 3.12 into `python/`, makes a new folder `runtimes/<os>-<arch>/rN/` and installs Lightmind with its terminal UI there. Where Lightmind comes from stands in `state/install-source.txt`; the wheel is kept in `wheels/`, so a copied or moved tree rebuilds from its own files and cache, offline.
A start where nothing changed does not run uv: the launcher reads the pointer `runtimes/<os>-<arch>.current` (one line, the name of the build), checks the files `lm-root.txt` (the place the build was made for: a moved tree builds again) and `lm-stamp.txt` in it, and starts it.

## Updating while lm windows are open

`lm update` can run at any time. It writes a new stamp to `state/runtime-stamp.txt`, brings the launchers `lm` and `lm.cmd` to the new version, and has the launcher build the new runtime at once (`system/lm build`; `lm.cmd build` on Windows). A new build goes into a folder of its own and the pointer is switched only when it stands complete. Windows that are already open keep running the build they started with until they end; every window opened afterwards uses the new one. If the build fails or is skipped, the next start builds it (a start that finds a new build in progress in another window simply runs the current one).
A build that no window uses any more is removed at the next start. On Windows a folder holding a running `lm.exe`, `python.exe` or `claude.exe` cannot be renamed, which is how the launcher knows it is in use; on Linux and macOS every start holds a shared `flock` on `lm-use.lock` in its build for as long as lm runs (without the `flock` program, old builds are simply kept). `lm check` shows which build runs and how many older ones are still in use; that is normal.
`runtime-<os>-<arch>/` (a single folder, made by earlier launchers) is run as long as it is current and is removed once a new build exists and nobody uses it.

The AI can neither read nor change anything here. Git ignores the folder except the launchers and this file, so a copy of the tree on another machine builds its own engine on the first start. Deleting `claude/` logs you out (`lm login <account>`), deleting `state/` resets the money ledger.
