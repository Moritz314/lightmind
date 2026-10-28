# /// script
# requires-python = ">=3.11"
# dependencies = ["pymupdf"]
# ///
"""pdf tool: text / find / split.  Params as JSON on stdin, result on stdout."""
import json
import sys

import fitz  # PyMuPDF


def pages_of(spec: str | None, n: int) -> list[int]:
    if not spec:
        return list(range(n))
    out: list[int] = []
    for part in spec.split(","):
        a, _, b = part.strip().partition("-")
        lo, hi = int(a), int(b or a)
        out.extend(range(lo - 1, min(hi, n)))
    return out


def main() -> int:
    p = json.load(sys.stdin)
    doc = fitz.open(p["file"])
    cmd = p["command"]
    if cmd == "text":
        for i in pages_of(p.get("pages"), len(doc)):
            print(f"--- page {i + 1} ---")
            print(doc[i].get_text())
    elif cmd == "find":
        q = p["query"].lower()
        for i, page in enumerate(doc):
            for line in page.get_text().splitlines():
                if q in line.lower():
                    print(f"{i + 1}: {line.strip()}")
    elif cmd == "split":
        out = fitz.open()
        for i in pages_of(p.get("pages"), len(doc)):
            out.insert_pdf(doc, from_page=i, to_page=i)
        out.save(p["out"])
        print(f"wrote {p['out']} ({len(out)} pages)")
    else:
        print(f"unknown command {cmd}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
