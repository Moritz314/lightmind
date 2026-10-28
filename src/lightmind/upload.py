"""`/upload` and `/datei`: the native file dialog of the machine the chat runs on, and the copy of the chosen files into
the chat's run folder as `[Anhang: <path>]` lines for the input.

Both chat surfaces (Orbit and `lm chat`) use it.  The dialog is a subprocess, so a caller runs `pick` in a worker thread;
nothing here touches a screen.  May import: nothing of the core.
"""
from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

MAX_COPY = 500 * 1024 * 1024                    # a bigger file is not copied, its own path is used
DIALOG_TIMEOUT = 900                            # seconds; a dialog left open must not keep the chat's exit waiting forever
TITLE = "Dateien anhängen"
NO_PICKER = "Kein Datei-Dialog hier möglich – Pfad eintippen oder per Telegram schicken."
IMAGE_TYPES = ("png", "jpg", "jpeg", "gif", "webp", "bmp", "heic", "tif", "tiff")


@dataclass
class Picked:
    """What the dialog gave: the `files` chosen (empty: cancelled), or a `notice` saying why no dialog was possible."""
    files: list[Path] = field(default_factory=list)
    notice: str = ""


class Unavailable(Exception):
    """No dialog can be shown here; the message is the German notice for the user."""


# ------------------------------------------------------------------ the dialog
# Windows: the dialog is owned by an invisible top-most form, else it opens behind the terminal.  The names are written
# through a UTF-8 stream because the console code page of a piped PowerShell mangles umlauts.
WINDOWS_SCRIPT = r"""
Add-Type -AssemblyName System.Windows.Forms
[System.Windows.Forms.Application]::EnableVisualStyles()
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
$owner.ShowInTaskbar = $false
$owner.Opacity = 0
$owner.Show()
$owner.Activate()
$dialog = New-Object System.Windows.Forms.OpenFileDialog
$dialog.Title = $env:LM_UPLOAD_TITLE
$dialog.Multiselect = $true
$dialog.CheckFileExists = $true
$dialog.Filter = $env:LM_UPLOAD_FILTER
if ($env:LM_UPLOAD_DIR -and (Test-Path -LiteralPath $env:LM_UPLOAD_DIR)) { $dialog.InitialDirectory = $env:LM_UPLOAD_DIR }
$result = $dialog.ShowDialog($owner)
$out = New-Object System.IO.StreamWriter([Console]::OpenStandardOutput(), (New-Object System.Text.UTF8Encoding($false)))
if ($result -eq [System.Windows.Forms.DialogResult]::OK) { foreach ($name in $dialog.FileNames) { $out.WriteLine($name) } }
$out.Flush()
$owner.Dispose()
"""

# macOS: cancelling raises error -128, which is answered with an empty list.
MAC_SCRIPT = """on run argv
    set picked to {}
    try
        if (count of argv) > 1 and (item 2 of argv) is not "" then
            set picked to choose file with prompt (item 1 of argv) default location (POSIX file (item 2 of argv)) with multiple selections allowed
        else
            set picked to choose file with prompt (item 1 of argv) with multiple selections allowed
        end if
    on error number -128
        return ""
    end try
    set out to ""
    repeat with f in picked
        set out to out & POSIX path of f & linefeed
    end repeat
    return out
end run
"""


def windows_filter() -> str:
    """The `Filter` of the Windows dialog: everything first (the default), then pictures."""
    pictures = ";".join(f"*.{t}" for t in IMAGE_TYPES)
    return f"Alle Dateien (*.*)|*.*|Bilder|{pictures}"


def powershell_command(script: str = WINDOWS_SCRIPT) -> list[str]:
    """The PowerShell call that runs `script` in a single-threaded apartment (a Windows dialog needs one).  The script
    travels base64-encoded (`-EncodedCommand`), so no quoting can break it."""
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if exe is None:
        raise Unavailable(NO_PICKER)
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return [exe, "-NoProfile", "-NonInteractive", "-STA", "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded]


def remote_session(env: dict[str, str]) -> bool:
    """True when this process runs in an ssh login: a dialog would open on another machine, if at all."""
    return bool(env.get("SSH_CONNECTION") or env.get("SSH_TTY") or env.get("SSH_CLIENT"))


def command(start: Path | None, platform: str | None = None, env: dict[str, str] | None = None) -> tuple[list[str], dict[str, str]]:
    """The dialog command for this machine and the environment to run it in.  `start` is the folder the dialog opens in
    (None: the dialog's own choice); `platform` and `env` default to the real ones (tests pass their own).  Raises
    `Unavailable` when no dialog is possible (ssh, no display, no tool)."""
    platform = platform or sys.platform
    env = dict(os.environ if env is None else env)
    where = str(start) if start else ""
    if remote_session(env):
        raise Unavailable(NO_PICKER)
    if platform == "win32":
        env.update(LM_UPLOAD_DIR=where, LM_UPLOAD_TITLE=TITLE, LM_UPLOAD_FILTER=windows_filter())
        return powershell_command(), env
    if platform == "darwin":
        if shutil.which("osascript") is None:
            raise Unavailable(NO_PICKER)
        return ["osascript", "-e", MAC_SCRIPT, TITLE, where], env
    if not (env.get("DISPLAY") or env.get("WAYLAND_DISPLAY")):
        raise Unavailable(NO_PICKER)
    suffix = where.rstrip("/") + "/" if where else ""
    if shutil.which("zenity"):
        return ["zenity", "--file-selection", "--multiple", "--separator=\n", f"--title={TITLE}",
                *([f"--filename={suffix}"] if suffix else [])], env
    if shutil.which("kdialog"):
        return ["kdialog", "--title", TITLE, "--getopenfilename", suffix or os.path.expanduser("~") + "/", "--multiple",
                "--separate-output"], env
    raise Unavailable(NO_PICKER)


def _run(argv: list[str], env: dict[str, str]) -> tuple[int, str, str]:
    """Run the dialog process and return (exit code, stdout, stderr).  The one place that starts a process; the tests
    replace it, so no test opens a real dialog."""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)       # no console window flashes up next to the dialog
    r = subprocess.run(argv, env=env, capture_output=True, timeout=DIALOG_TIMEOUT, creationflags=flags)
    return r.returncode, r.stdout.decode("utf-8", "replace"), r.stderr.decode("utf-8", "replace")


def parse_names(out: str) -> list[Path]:
    """The paths the dialog printed, one per line; blank lines and names of files that are gone are left out."""
    names = [line.strip("\r") for line in out.splitlines()]
    return [Path(n) for n in names if n.strip() and Path(n).is_file()]


def pick(start: Path | None = None, platform: str | None = None) -> Picked:
    """Show the file dialog and wait for it (call it from a worker thread).  A cancelled dialog gives no files and no
    notice; a machine without a dialog gives the German `notice` and no files.  `start` is the folder it opens in,
    `platform` defaults to the real one."""
    platform = platform or sys.platform
    try:
        argv, env = command(start, platform)
        code, out, err = _run(argv, env)
    except Unavailable as e:
        return Picked(notice=str(e))
    except subprocess.TimeoutExpired:
        return Picked()                                      # the dialog was left open for too long: it was closed
    except (OSError, subprocess.SubprocessError):
        return Picked(notice=NO_PICKER)
    files = parse_names(out)
    if files or code == 0:
        return Picked(files)
    broken = platform == "win32" or code != 1 or "display" in err.lower()    # zenity and kdialog answer a cancel with 1
    return Picked(notice=NO_PICKER) if broken else Picked()


def start_folder(last: Path | None) -> Path:
    """Where the dialog opens: the folder used last in this chat, else the Downloads folder, else the home folder."""
    if last is not None and last.is_dir():
        return last
    downloads = Path.home() / "Downloads"
    return downloads if downloads.is_dir() else Path.home()


# ------------------------------------------------------------------ the copy and the marker
def unique_target(folder: Path, name: str) -> Path:
    """`folder/name`, or `name-1.ext`, `name-2.ext` ... when the name is taken."""
    target = folder / name
    stem, ext = Path(name).stem, Path(name).suffix
    n = 0
    while target.exists():
        n += 1
        target = folder / f"{stem}-{n}{ext}"
    return target


def marker_path(path: Path, root: Path) -> str:
    """The path as the marker shows it: relative to the tree root when inside it, else absolute; forward slashes."""
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except (ValueError, OSError):
        return path.as_posix()


def marker(path: Path, root: Path) -> str:
    """The input line of one attached file.  `Anhang` is the contract with the model: a file the user attached, to be
    opened with the read tool (the description of that tool in tools.py says so)."""
    return f"[Anhang: {marker_path(path, root)}]"


@dataclass
class Attached:
    """The result of `attach`: the marker `lines` for the input and `notes` about files that were not copied."""
    lines: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def attach(files: list[Path], run_folder: Path, root: Path) -> Attached:
    """Copy each file into `run_folder/uploads` (a taken name gets `-1`, `-2`) and make its marker line.  A file over
    500 MB, or one that cannot be copied, keeps its own path; a file that is gone is left out with a note.  `root` is
    the tree root the marker paths are relative to."""
    out = Attached()
    folder = run_folder / "uploads"
    for source in files:
        if not source.is_file():
            out.notes.append(f"{source.name}: nicht gefunden")
            continue
        try:
            if source.stat().st_size > MAX_COPY:
                out.notes.append(f"{source.name}: über 500 MB, nicht kopiert (Originalpfad)")
                out.lines.append(marker(source, root))
                continue
            folder.mkdir(parents=True, exist_ok=True)
            target = unique_target(folder, source.name.replace("[", "(").replace("]", ")"))   # `]` would end the marker
            shutil.copy2(source, target)
        except OSError as e:
            out.notes.append(f"{source.name}: nicht kopiert ({e.strerror or e}), Originalpfad")
            out.lines.append(marker(source, root))
            continue
        out.lines.append(marker(target, root))
    return out


def insert_lines(text: str, pos: int, lines: list[str]) -> tuple[str, int]:
    """`text` with the `lines` put at `pos` as lines of their own (a newline before them unless `pos` is at a line start,
    one after them so the user types on the next line).  -> (the new text, the cursor behind the block)."""
    pos = max(0, min(pos, len(text)))
    head, tail = text[:pos], text[pos:]
    block = "\n".join(lines)
    if head and not head.endswith("\n"):
        block = "\n" + block
    block += "\n"
    if tail.startswith("\n"):
        tail = tail[1:]                                      # the block already ends the line
    return head + block + tail, len(head) + len(block)


def confirmation(count: int) -> str:
    """The note after attaching: `1 Datei angehängt` / `2 Dateien angehängt`."""
    return f"{count} Datei angehängt" if count == 1 else f"{count} Dateien angehängt"
