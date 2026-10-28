"""read on a picture returns an image block; bash gives a referenced tool its granted secrets (fake secrets only)."""
import base64
import io

from PIL import Image

from lightmind.images import MAX_EDGE, load_image
from lightmind.model import Message
from lightmind.providers import anthropic_api, openai_compat
from lightmind.model import Request
from lightmind.secrets import Masker, build_tool_env

from test_tools import FakeApi, add_tool, make, yes

ENV_SCRIPT = "import os;print('tok=' + os.environ.get('TOK','none'))\n"
SHOW = "python tools/sec/run.py"


def png(path, size=(40, 30)):
    Image.new("RGB", size, (200, 30, 30)).save(path)
    return path


def jpg_big(path, size=(3000, 2000)):
    Image.effect_noise(size, 80).convert("RGB").save(path, "JPEG", quality=95)
    return path


# ---------------------------------------------------------------- images
async def test_read_png_returns_image_block(home):
    f = png(home.root / "pic.png")
    d, *_ = make(home)
    r = await d.call("read", {"path": "pic.png"})
    assert r.ok and r.text == "image 40x30"
    assert r.image["media_type"] in ("image/png", "image/jpeg")
    assert Image.open(io.BytesIO(base64.b64decode(r.image["data"]))).size == (40, 30)
    assert f.read_bytes()[:4] not in r.text.encode()      # never the bytes as text


async def test_read_text_has_no_image(home):
    (home.root / "a.txt").write_text("hello\n", "utf-8")
    d, *_ = make(home)
    r = await d.call("read", {"path": "a.txt"})
    assert r.image is None and "hello" in r.text


def test_big_picture_is_scaled_and_small_enough(tmp_path):
    f = jpg_big(tmp_path / "big.jpg")
    image, w, h = load_image(f)
    raw = base64.b64decode(image["data"])
    assert max(w, h) == MAX_EDGE and len(raw) <= 3_500_000
    assert max(Image.open(io.BytesIO(raw)).size) == MAX_EDGE


async def test_broken_image_is_an_error_not_a_crash(home):
    (home.root / "bad.png").write_bytes(b"not a picture")
    d, *_ = make(home)
    r = await d.call("read", {"path": "bad.png"})
    assert not r.ok and "cannot read image" in r.text


async def test_image_data_is_not_in_the_event_log(home):
    png(home.root / "pic.png")
    d, _, run = make(home)
    await d.call("read", {"path": "pic.png"})
    log = "".join(p.read_text("utf-8") for p in run.path.glob("*.jsonl"))
    assert '"tool"' in log
    assert "iVBOR" not in log and "/9j/" not in log


def test_anthropic_api_gets_an_image_block():
    img = {"media_type": "image/png", "data": "QUJD"}
    out = anthropic_api.to_api_messages([Message("tool", "image 1x1", tool_call_id="t1", image=img)])
    content = out[0]["content"][0]["content"]
    assert content[1] == {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}}
    plain = anthropic_api.to_api_messages([Message("tool", "x", tool_call_id="t1")])
    assert plain[0]["content"][0]["content"] == "x"


def test_openai_compat_gets_a_text_note_only():
    img = {"media_type": "image/png", "data": "QUJD"}
    out = openai_compat.to_api_messages(Request(system="s", messages=[
        Message("tool", "image 1x1", tool_call_id="t1", image=img)]), False)
    assert "QUJD" not in str(out) and "cannot show pictures" in out[-1]["content"]


async def test_claude_sdk_handler_passes_the_image():
    from types import SimpleNamespace
    from lightmind.providers.claude_sdk import ClaudeSdkProvider as P
    res = SimpleNamespace(ok=True, text="image 1x1", image={"media_type": "image/png", "data": "QUJD"})

    async def call(name, args):
        return res
    prov = P.__new__(P)
    prov.dispatcher = SimpleNamespace(call=call)
    out = await P._handler(prov, "read")({"path": "x.png"})
    assert out["content"][1] == {"type": "image", "data": "QUJD", "mimeType": "image/png"}


# ---------------------------------------------------------------- secrets in bash
def write_secret(home, ref="svc/main", body="TOK=fake-secret-value-123\n"):
    name, acct = ref.split("/")
    d = home.path("secrets") / name
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{acct}.env").write_text(body, "utf-8")


async def test_bash_injects_secret_of_referenced_tool_and_masks(home):
    add_tool(home, "sec", ENV_SCRIPT, "secrets: [svc/main]\n")
    d, api, _ = make(home, asker=yes)
    r = await d.call("bash", {"command": SHOW})
    assert r.text.strip() == "tok=<secret:svc/main:TOK>" and "secret_use" in api.audits


async def test_bash_without_a_tool_reference_gets_no_secret(home):
    add_tool(home, "sec", ENV_SCRIPT, "secrets: [svc/main]\n")
    d, api, _ = make(home, asker=yes)
    r = await d.call("bash", {"command": "python -c \"import os;print(os.environ.get('TOK','none'))\""})
    assert r.text.strip() == "none" and "secret_use" not in api.audits


async def test_bash_only_injects_the_referenced_tools_secrets(home):
    add_tool(home, "sec", ENV_SCRIPT, "secrets: [svc/main]\n")
    add_tool(home, "other", "print(1)\n", "secrets: [svc/other]\n")
    d, api, _ = make(home, asker=yes)
    await d.call("bash", {"command": SHOW})
    assert [a for a in api.audits if a == "secret_use"] == ["secret_use"]      # sec only, not other


async def test_bash_denied_secret_is_not_injected(home):
    add_tool(home, "sec", ENV_SCRIPT, "secrets: [svc/main]\n")
    d, *_ = make(home, FakeApi(deny={("secret", "svc/main")}), asker=yes)
    assert (await d.call("bash", {"command": SHOW})).text.strip() == "tok=none"


async def test_bash_tools_folder_of_another_place_gets_nothing(home, tmp_path):
    add_tool(home, "sec", ENV_SCRIPT, "secrets: [svc/main]\n")
    d, *_ = make(home, asker=yes)
    assert d._bash_tools("python /elsewhere/tools/sec/run.py") == []
    assert d._bash_tools("python mytools/sec/run.py") == []
    assert [t.name for t in d._bash_tools(f"python {home.root}/tools/sec/run.py")] == ["sec"]
    assert [t.name for t in d._bash_tools("cd tools/sec && uv run run.py")] == ["sec"]
    assert d._bash_tools("python tools/../secrets/x") == []


async def test_bash_with_the_real_secret_code_masks_by_value(home):
    """Real build_tool_env and Masker on a fake .env in the tmp tree."""
    write_secret(home)
    add_tool(home, "sec", ENV_SCRIPT, "secrets: [svc/main]\n")
    api = FakeApi()
    api.Masker = Masker
    api.build_tool_env = build_tool_env
    d, *_ = make(home, api, asker=yes)
    r = await d.call("bash", {"command": SHOW})
    assert "fake-secret-value-123" not in r.text and r.text.strip() == "tok=<secret:svc/main:TOK>"
