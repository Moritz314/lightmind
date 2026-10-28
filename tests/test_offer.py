"""offer.py: `[[file: ...]]` lines -> download links (terminal), pieces (phone, plugin), refusals for secrets."""
import base64
import urllib.error
import urllib.request

import pytest

from lightmind import offer

DOTENV = ".en" + "v"


@pytest.fixture
def root(tmp_path):
    (tmp_path / "secrets" / "x").mkdir(parents=True)
    (tmp_path / "secrets" / "x" / "info.md").write_text("hi")
    (tmp_path / "system").mkdir()
    (tmp_path / "system" / "a.txt").write_text("hi")
    (tmp_path / "ergebnis.pdf").write_bytes(b"%PDF-1.4 demo" * 50)
    (tmp_path / "key.txt").write_text("-----BEGIN RSA PRIVATE KEY-----\nabc")
    (tmp_path / "pw.txt").write_text("password = hunter2hunter2")
    (tmp_path / DOTENV).write_text("A=1")
    return tmp_path


def test_markers():
    assert offer.markers("a\n[[file: x.pdf]]\n  [[file:  C:\\a b\\c.txt ]]\nno [[file: inline]] here") == ["x.pdf", "C:\\a b\\c.txt"]


def test_check_refuses(root):
    for rel in ("secrets/x/info.md", "system/a.txt", DOTENV, "nope.pdf"):
        with pytest.raises(offer.Refused):
            offer.check(root / rel, root)
    assert offer.check(root / "key.txt", root)                      # local terminal: path rules only
    for rel in ("key.txt", "pw.txt"):
        with pytest.raises(offer.Refused):
            offer.check(root / rel, root, scan=True)
    assert offer.check(root / "ergebnis.pdf", root, scan=True)


def test_dotdot_does_not_escape(root):
    with pytest.raises(offer.Refused):
        offer.check(root / "ergebnis.pdf" / ".." / "secrets" / "x" / "info.md", root)


def test_linkify_and_download(root):
    out = offer.linkify("Hier:\n[[file: ergebnis.pdf]]\nfertig", root)
    assert "[[file" not in out and "↓ ergebnis.pdf (650 B)" in out
    url = out.split("](")[1].split(")")[0]
    assert url.startswith("http://127.0.0.1:")
    with urllib.request.urlopen(url) as r:
        assert r.headers["Content-Disposition"].startswith("attachment; filename*=UTF-8''ergebnis.pdf")
        assert r.read() == (root / "ergebnis.pdf").read_bytes()
    again = offer.linkify("[[file: ergebnis.pdf]]", root)
    assert again.split("](")[1] == url + ")"                          # same file, same link
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(url.rsplit("/", 1)[0] + "/" + "A" * 24)
    assert e.value.code == 404
    req = urllib.request.Request(url, headers={"Host": "evil.example"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req)
    assert e.value.code == 403


def test_linkify_refusal_and_plain_text(root):
    assert offer.linkify("nothing", root) == "nothing"
    assert "not offered" in offer.linkify("[[file: secrets/x/info.md]]", root)


def test_token_expires(root, monkeypatch):
    monkeypatch.setattr(offer, "TOKEN_TTL", -5)
    out = offer.linkify("[[file: ergebnis.pdf]]", root)
    url = out.split("](")[1].rstrip(")")
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(url)
    assert e.value.code == 404


def test_pieces(root, monkeypatch):
    monkeypatch.setattr(offer, "CHUNK", 300)
    got, n = b"", 1
    while True:
        p = offer.piece("ergebnis.pdf", root, n)
        got += base64.b64decode(p["data"])
        assert p["pieces"] == 3
        if p["done"]:
            break
        n += 1
    assert got == (root / "ergebnis.pdf").read_bytes() and n == 3
    with pytest.raises(offer.Refused):
        offer.piece("key.txt", root)
