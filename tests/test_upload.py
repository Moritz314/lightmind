"""`/upload` and `/datei` (upload.py): parsing of the two words, the dialog command per machine, the copy into the run
folder with its markers, the "no dialog here" path, and `lm chat`.  The Orbit side is in test_tui_upload.py.
No test opens a real dialog: `upload._run` is the one place that starts a process, and conftest makes it fail."""
import base64
import subprocess
from pathlib import Path

import pytest

from lightmind import cli, tools, upload
from lightmind import slash as S
from lightmind.model import Text, Usage
from lightmind.slash import Catalog, parse, take_out
from lightmind.upload import Picked

from test_cli import lm as lm_cli, script  # noqa: F401 - `lm_cli` is the fixture of test_cli.py: `lm chat` on a scripted provider
from test_slash import sent


@pytest.fixture
def lm(lm_cli):  # noqa: F811
    cli._ATTACHED.clear()
    cli._UPLOAD_DIR.clear()
    return lm_cli


def cat() -> Catalog:
    return Catalog(builtin=("mode", "effort", "clear", "upload", "datei"), skills={"arsenal"}, rows=("exit",))


def files_in(folder, **named):
    """Real small files `name=content` in `folder`."""
    out = []
    for name, content in named.items():
        p = folder / name.replace("__", ".")
        p.write_text(content, "utf-8")
        out.append(p)
    return out


# ---------------------------------------------------------------------------- the words
@pytest.mark.parametrize("word", ["/upload", "/datei", "/UPLOAD"])
def test_the_word_is_a_command_of_its_own_kind_that_sends_nothing(word):
    p = parse(word, cat())
    assert [(a.name, a.arg, a.kind) for a in p.acts] == [(word[1:].lower(), "", "insert")]
    assert p.text == "" and p.error == "" and p.then is None


def test_inside_a_sentence_the_word_is_cut_out_and_takes_no_word():
    p = parse("schau dir /upload jetzt an", cat())
    assert p.text == "schau dir jetzt an" and [a.name for a in p.acts] == ["upload"]
    p = parse("/datei foto", cat())                                       # `foto` is text, not an argument
    assert p.text == "foto" and p.acts[0].arg == ""


def test_double_slash_and_code_keep_the_word_as_text():
    assert parse("//upload", cat()).text == "/upload" and parse("//upload", cat()).acts == []
    assert parse("use `/upload` here", cat()).acts == []


def test_a_surface_without_the_command_does_not_know_it():
    p = parse("/upload", Catalog(builtin=("mode",)))
    assert p.unknown and p.acts == []


def test_it_may_come_with_other_commands_and_never_leaves_the_chat():
    p = parse("/effort high /upload", cat())
    assert [a.kind for a in p.acts] == ["command", "insert"] and p.then is None


def test_take_out_removes_the_words_and_says_where_the_first_stood():
    assert take_out("/upload") == ("", 0)
    assert take_out("schau /upload an") == ("schau an", 6)
    assert take_out("schau an /upload") == ("schau an", 8)                # the blank before a word that ends the line goes too
    assert take_out("a /upload b /datei c") == ("a b c", 2)
    assert take_out("a //upload `/upload`") == ("a //upload `/upload`", 20)           # nothing to take out: the end
    assert take_out("line one\n/upload\nline two") == ("line one\nline two", 9)


# ---------------------------------------------------------------------------- the dialog command
@pytest.fixture
def tools_present(monkeypatch):
    monkeypatch.setattr(upload.shutil, "which", lambda name: f"/bin/{name}")


def test_windows_runs_a_sta_powershell_with_a_top_most_owner_and_multiselect(tools_present, tmp_path):
    argv, env = upload.command(tmp_path, "win32", {})
    assert "-STA" in argv and "-NoProfile" in argv
    script_text = base64.b64decode(argv[argv.index("-EncodedCommand") + 1]).decode("utf-16-le")
    assert "OpenFileDialog" in script_text and "Multiselect = $true" in script_text
    assert "TopMost = $true" in script_text and "ShowDialog($owner)" in script_text and "UTF8Encoding($false)" in script_text
    assert env["LM_UPLOAD_DIR"] == str(tmp_path)
    assert env["LM_UPLOAD_FILTER"].startswith("Alle Dateien (*.*)|*.*|Bilder|*.png;*.jpg")


def test_windows_without_powershell_has_no_dialog(monkeypatch):
    monkeypatch.setattr(upload.shutil, "which", lambda name: None)
    with pytest.raises(upload.Unavailable):
        upload.command(None, "win32", {})


def test_mac_uses_osascript_with_the_start_folder(tools_present, tmp_path):
    argv, _ = upload.command(tmp_path, "darwin", {})
    assert argv[0] == "osascript" and "multiple selections allowed" in argv[2] and argv[-1] == str(tmp_path)


def test_linux_needs_a_display_and_zenity_or_kdialog(monkeypatch, tmp_path):
    monkeypatch.setattr(upload.shutil, "which", lambda name: f"/bin/{name}" if name == "zenity" else None)
    with pytest.raises(upload.Unavailable):
        upload.command(tmp_path, "linux", {})                              # no display
    argv, _ = upload.command(tmp_path, "linux", {"DISPLAY": ":0"})
    assert argv[:3] == ["zenity", "--file-selection", "--multiple"] and f"--filename={tmp_path}/" in argv
    monkeypatch.setattr(upload.shutil, "which", lambda name: f"/bin/{name}" if name == "kdialog" else None)
    assert upload.command(tmp_path, "linux", {"WAYLAND_DISPLAY": "w0"})[0][0] == "kdialog"
    monkeypatch.setattr(upload.shutil, "which", lambda name: None)
    with pytest.raises(upload.Unavailable):
        upload.command(tmp_path, "linux", {"DISPLAY": ":0"})                # no tool


@pytest.mark.parametrize("platform, env", [("linux", {"DISPLAY": ":0", "SSH_CONNECTION": "1 2 3 4"}),
                                           ("win32", {"SSH_TTY": "/dev/pts/0"}), ("darwin", {"SSH_CLIENT": "1 2 3"})])
def test_an_ssh_session_has_no_dialog(tools_present, platform, env):
    with pytest.raises(upload.Unavailable):
        upload.command(None, platform, env)


def test_the_shipped_script_has_no_syntax_error_for_powershell_if_it_is_here():
    ps = upload.shutil.which("powershell") or upload.shutil.which("pwsh")
    if ps is None:
        pytest.skip("no PowerShell on this machine")
    check = "$e = $null; [System.Management.Automation.Language.Parser]::ParseInput([Console]::In.ReadToEnd(), [ref]$null, [ref]$e) | Out-Null; if ($e) { $e; exit 1 }"
    r = subprocess.run([ps, "-NoProfile", "-Command", check], input=upload.WINDOWS_SCRIPT, capture_output=True, text=True)
    assert r.returncode == 0, r.stdout


# ---------------------------------------------------------------------------- pick: cancel, notice, files
@pytest.fixture
def dialog(monkeypatch):
    """`dialog(code, out, err)` makes the dialog process end that way."""
    monkeypatch.setattr(upload, "command", lambda start, platform=None, env=None: (["dialog"], {}))

    def say(code=0, out="", err=""):
        monkeypatch.setattr(upload, "_run", lambda argv, env: (code, out, err))
    return say


def test_the_chosen_files_come_back_in_order(dialog, tmp_path):
    a, b = files_in(tmp_path, a__png="1", b__pdf="2")
    dialog(0, f"{a}\n{b}\r\n\n{tmp_path / 'gone.txt'}\n")
    assert upload.pick(tmp_path).files == [a, b]                           # a name that is gone is dropped


@pytest.mark.parametrize("platform, code, err", [("win32", 0, ""), ("linux", 1, ""), ("darwin", 0, "")])
def test_cancel_is_no_files_and_no_notice(dialog, platform, code, err):
    dialog(code, "", err)
    assert upload.pick(None, platform) == Picked()


@pytest.mark.parametrize("platform, code, err", [("win32", 1, "#< CLIXML"), ("linux", 5, "timeout"), ("linux", 1, "cannot open display :0")])
def test_a_failed_dialog_says_so_in_german(dialog, platform, code, err):
    dialog(code, "", err)
    assert upload.pick(None, platform) == Picked(notice=upload.NO_PICKER)


def test_no_dialog_here_is_the_notice_and_nothing_else(monkeypatch):
    monkeypatch.setattr(upload, "_run", lambda argv, env: pytest.fail("a process was started"))
    monkeypatch.setenv("SSH_CONNECTION", "1 2 3 4")
    assert upload.pick(None, "linux") == Picked(notice="Kein Datei-Dialog hier möglich – Pfad eintippen oder per Telegram schicken.")
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    for name in ("SSH_TTY", "SSH_CLIENT", "DISPLAY", "WAYLAND_DISPLAY"):
        monkeypatch.delenv(name, raising=False)
    assert upload.pick(None, "linux").notice == upload.NO_PICKER           # no display


def test_a_missing_tool_or_a_hung_dialog(monkeypatch):
    monkeypatch.setattr(upload, "command", lambda start, platform=None, env=None: (["dialog"], {}))

    def missing(argv, env):
        raise FileNotFoundError("dialog")

    def hung(argv, env):
        raise subprocess.TimeoutExpired("dialog", 1)
    monkeypatch.setattr(upload, "_run", missing)
    assert upload.pick().notice == upload.NO_PICKER
    monkeypatch.setattr(upload, "_run", hung)
    assert upload.pick() == Picked()


def test_the_dialog_opens_in_the_last_folder_else_downloads(tmp_path, monkeypatch):
    monkeypatch.setattr(upload.Path, "home", classmethod(lambda cls: tmp_path))
    assert upload.start_folder(None) == tmp_path                           # no Downloads folder: the home folder
    (tmp_path / "Downloads").mkdir()
    assert upload.start_folder(None) == tmp_path / "Downloads"
    last = tmp_path / "Pictures"
    assert upload.start_folder(last) == tmp_path / "Downloads"             # a folder that is gone does not count
    last.mkdir()
    assert upload.start_folder(last) == last


# ---------------------------------------------------------------------------- copy and marker
def test_files_are_copied_into_uploads_and_marked_relative_to_the_root(tmp_path):
    root, run = tmp_path / "tree", tmp_path / "tree" / "runs" / "r1"
    run.mkdir(parents=True)
    src = tmp_path / "from"
    src.mkdir()
    a, b = files_in(src, foto__png="PNG", **{"Bericht Q3__pdf": "PDF"})
    done = upload.attach([a, b], run, root)
    assert done.lines == ["[Anhang: runs/r1/uploads/foto.png]", "[Anhang: runs/r1/uploads/Bericht Q3.pdf]"] and done.notes == []
    assert (run / "uploads" / "foto.png").read_text() == "PNG" and a.exists()        # a copy, the original stays


def test_a_taken_name_gets_a_number_before_the_extension(tmp_path):
    run = tmp_path / "run"
    src = tmp_path / "from"
    src.mkdir()
    (a,) = files_in(src, foto__png="x")
    lines = [l for _ in range(3) for l in upload.attach([a], run, tmp_path / "tree").lines]
    names = sorted(p.name for p in (run / "uploads").iterdir())
    assert names == ["foto-1.png", "foto-2.png", "foto.png"]
    assert [l.rsplit("/", 1)[1] for l in lines] == ["foto.png]", "foto-1.png]", "foto-2.png]"]
    (c,) = files_in(src, Makefile="x")                                     # no extension, and a dot file
    (d,) = files_in(src, **{".env": "x"})
    for f in (c, d, c, d):
        upload.attach([f], run, tmp_path / "tree")
    assert {"Makefile", "Makefile-1", ".env", ".env-1"} <= {p.name for p in (run / "uploads").iterdir()}


def test_a_file_outside_the_tree_gets_an_absolute_path_with_forward_slashes(tmp_path):
    src = tmp_path / "from"
    src.mkdir()
    (a,) = files_in(src, a__txt="x")
    run = tmp_path / "elsewhere" / "run"
    line = upload.attach([a], run, tmp_path / "tree").lines[0]
    assert line == f"[Anhang: {(run / 'uploads' / 'a.txt').as_posix()}]" and "\\" not in line


def test_a_file_over_the_limit_is_not_copied_and_keeps_its_path(tmp_path, monkeypatch):
    run, src = tmp_path / "run", tmp_path / "from"
    src.mkdir()
    big, small = files_in(src, big__iso="x" * 20, small__txt="x")
    monkeypatch.setattr(upload, "MAX_COPY", 10)
    done = upload.attach([big, small], run, tmp_path / "tree")
    assert done.lines[0] == f"[Anhang: {big.as_posix()}]" and done.lines[1].endswith("uploads/small.txt]")
    assert not (run / "uploads" / "big.iso").exists() and "über 500 MB" in done.notes[0]


def test_the_limit_is_500_mb():
    assert upload.MAX_COPY == 500 * 1024 * 1024


def test_a_file_that_cannot_be_copied_keeps_its_path_and_a_gone_file_is_left_out(tmp_path, monkeypatch):
    src = tmp_path / "from"
    src.mkdir()
    (a,) = files_in(src, a__txt="x")

    def broken(*args, **kw):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(upload.shutil, "copy2", broken)
    done = upload.attach([a, src / "gone.txt"], tmp_path / "run", tmp_path / "tree")
    assert done.lines == [f"[Anhang: {a.as_posix()}]"]
    assert "a.txt: nicht kopiert" in done.notes[0] and "gone.txt: nicht gefunden" in done.notes[1]


def test_brackets_in_a_name_cannot_end_the_marker(tmp_path):
    src = tmp_path / "from"
    src.mkdir()
    (a,) = files_in(src, **{"scan [1]__pdf": "x"})
    line = upload.attach([a], tmp_path / "run", tmp_path / "tree").lines[0]
    assert line.endswith("scan (1).pdf]") and line.count("]") == 1


def test_the_marker_names_the_attachment_and_the_read_tool_explains_it():
    assert upload.marker(Path("/x/y.png"), Path("/z")) == "[Anhang: /x/y.png]"
    desc = tools._BUILTINS["read"]["description"]
    assert "[Anhang: <path>]" in desc and "attached" in desc and "media skill" in desc


# ---------------------------------------------------------------------------- the lines in the input
def test_the_lines_go_in_at_the_cursor_as_lines_of_their_own():
    lines = ["[Anhang: a.png]", "[Anhang: b.pdf]"]
    assert upload.insert_lines("", 0, lines) == ("[Anhang: a.png]\n[Anhang: b.pdf]\n", 32)
    text, at = upload.insert_lines("Schau an", 5, lines)                  # mid-line: a line break before, one after
    assert text == "Schau\n[Anhang: a.png]\n[Anhang: b.pdf]\n an" and text[at:] == " an"
    text, at = upload.insert_lines("eins\nzwei", 5, ["[Anhang: a]"])      # at a line start: no extra break before
    assert text == "eins\n[Anhang: a]\nzwei" and text[at:] == "zwei"
    assert upload.insert_lines("x", 99, ["[Anhang: a]"])[0] == "x\n[Anhang: a]\n"


def test_the_confirmation_counts():
    assert upload.confirmation(1) == "1 Datei angehängt" and upload.confirmation(2) == "2 Dateien angehängt"


# ---------------------------------------------------------------------------- lm chat
def pick_returns(monkeypatch, files=(), notice=""):
    seen = []

    def pick(start=None, platform=None):
        seen.append(start)
        return Picked(list(files), notice)
    monkeypatch.setattr(upload, "pick", pick)
    return seen


def test_chat_the_files_go_in_front_of_the_next_message(lm, monkeypatch, tmp_path):
    src = tmp_path / "from"
    src.mkdir()
    a, b = files_in(src, foto__png="PNG", doc__pdf="PDF")
    pick_returns(monkeypatch, [a, b])
    script(lm, [Text("gesehen"), Usage()])
    code, out, err = lm("chat", stdin="/upload\nwas ist das?\n/quit\n")
    assert code == 0 and "2 Dateien angehängt" in out
    text = sent(lm)[0]
    first, second, rest = text.split("\n", 2)
    assert first.startswith("[Anhang: runs/") and first.endswith("/uploads/foto.png]") and second.endswith("/uploads/doc.pdf]")
    assert rest == "was ist das?"
    path = lm.tree / first[len("[Anhang: "):-1]
    assert path.read_text() == "PNG"                                       # the copy lives in the chat's run folder


def test_chat_a_cancel_sends_and_changes_nothing(lm, monkeypatch):
    pick_returns(monkeypatch, [])
    script(lm, [Text("ok"), Usage()])
    code, out, err = lm("chat", stdin="/upload\nhallo\n/quit\n")
    assert sent(lm) == ["hallo"] and "angehängt" not in out and cli._ATTACHED == []


def test_chat_no_dialog_says_so_and_sends_nothing_extra(lm, monkeypatch):
    pick_returns(monkeypatch, [], notice=upload.NO_PICKER)
    script(lm, [Text("ok"), Usage()])
    code, out, err = lm("chat", stdin="/datei\nhallo\n/quit\n")
    assert upload.NO_PICKER in out and sent(lm) == ["hallo"]


def test_chat_the_dialog_opens_where_the_last_file_was(lm, monkeypatch, tmp_path):
    src = tmp_path / "from"
    src.mkdir()
    (a,) = files_in(src, a__txt="x")
    seen = pick_returns(monkeypatch, [a])
    script(lm, [Text("ok"), Usage()])
    lm("chat", stdin="/upload\n/upload\n/quit\n")
    assert seen[1] == src
