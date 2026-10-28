"""Prepare a picture file for a model: shrink it to what vision APIs accept and return it as base64.

Used by the read tool so an agent can look at a PNG/JPEG/GIF/WebP instead of getting binary noise.
"""
from __future__ import annotations

import base64
import io
from pathlib import Path

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
MAX_EDGE = 1568            # longer edges are scaled down by the API anyway; sending them only costs bytes
MAX_BYTES = 3_500_000      # raw bytes; base64 stays below the 5 MB per-image limit of the API


def is_image(path: Path) -> bool:
    """True when the file name says picture."""
    return path.suffix.lower() in IMAGE_SUFFIXES


def load_image(path: Path) -> tuple[dict[str, str], int, int]:
    """Return ({"media_type", "data"} with base64 data, width, height) of the shrunk picture.

    Raises ValueError when the file is not a readable picture.
    """
    from PIL import Image, UnidentifiedImageError
    try:
        img = Image.open(path)
        img.load()
    except (UnidentifiedImageError, OSError) as e:
        raise ValueError(f"not a readable image: {e}") from e
    img = _first_frame_rgb(img)
    if max(img.size) > MAX_EDGE:
        img.thumbnail((MAX_EDGE, MAX_EDGE))
    data, media = _encode(img)
    return {"media_type": media, "data": base64.b64encode(data).decode("ascii")}, img.width, img.height


def _first_frame_rgb(img):
    """Animated GIF/WebP: first frame only. Keep alpha (PNG) only where the picture has any."""
    img.seek(0)
    return img.convert("RGBA" if "A" in img.getbands() or "transparency" in img.info else "RGB")


def _encode(img) -> tuple[bytes, str]:
    """PNG when transparent, else JPEG; shrink quality, then size, until it fits MAX_BYTES."""
    alpha = img.mode == "RGBA"
    for scale in (1.0, 0.75, 0.5, 0.35):
        cur = img if scale == 1.0 else img.resize((max(1, int(img.width * scale)), max(1, int(img.height * scale))))
        for quality in ((None,) if alpha else (88, 65)):
            buf = io.BytesIO()
            if alpha:
                cur.save(buf, "PNG", optimize=True)
            else:
                cur.save(buf, "JPEG", quality=quality)
            if buf.tell() <= MAX_BYTES:
                return buf.getvalue(), "image/png" if alpha else "image/jpeg"
    raise ValueError("image cannot be shrunk below the size limit")
