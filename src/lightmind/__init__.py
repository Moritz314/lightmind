"""Lightmind core."""
from __future__ import annotations

import re

__version__ = "0.1.3"      # also in pyproject.toml (tests/test_release.py checks that both say the same)

_SERIES = re.compile(r"0\.1\.(\d+)")


def codename(version: str | None = None) -> str:
    """The internal name of a release: the series 0.1.x is "orbit" and 0.1.n is `orbit vn` (0.1.1 is `orbit v1`, 0.1.7 is
    `orbit v7`).  A version outside that series has none: the empty string.  The product is called Lightmind, always; this
    is the only place that derives the codename, and every display (`lm --version`, the head of the boot checklist, the foot
    of the start screen, the runtime line of `lm check`) asks here."""
    m = _SERIES.match((version if version is not None else __version__).strip())
    return f"orbit v{int(m.group(1))}" if m else ""


def title(version: str | None = None) -> str:
    """`Lightmind 0.1.1 · orbit v1`, or `Lightmind 0.2.0` when the version has no codename."""
    v = version if version is not None else __version__
    return " · ".join(x for x in (f"Lightmind {v}", codename(v)) if x)


def version_text(version: str | None = None) -> str:
    """What `lm --version` prints: `lm 0.1.1 (orbit v1)`, or `lm 0.2.0` when the version has no codename."""
    v = version if version is not None else __version__
    name = codename(v)
    return f"lm {v}" + (f" ({name})" if name else "")
