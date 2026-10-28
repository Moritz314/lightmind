# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""text-stats: count lines, words and characters of text files, the example tool of a new tree.

    uv run tools/text-stats/text_stats.py FILE [FILE ...] [--json]

A folder argument counts the `*.md` files inside it (not deeper).  Prints one row per file and a total; `--json`
prints a list of objects instead.  Exit 0 when every file was read, 1 when one could not be read, 2 on bad input.
"""
import argparse
import json
import sys
from pathlib import Path


def expand(args: list[str]) -> list[Path]:
    """The files to count: each argument as it is, a folder as its `*.md` files.
    `args`: the paths given on the command line."""
    files: list[Path] = []
    for a in args:
        p = Path(a)
        files.extend(sorted(p.glob("*.md")) if p.is_dir() else [p])
    return files


def count(path: Path) -> dict:
    """Lines, words and characters of one file; `error` instead when it cannot be read.
    `path`: the file to read (UTF-8, broken bytes are replaced so a stray byte does not stop the count)."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return {"file": str(path), "error": e.strerror or type(e).__name__}
    return {"file": str(path), "lines": len(text.splitlines()), "words": len(text.split()), "chars": len(text)}


def table(rows: list[dict]) -> str:
    """The rows as a plain text table with a total line.  `rows`: results of `count`."""
    ok = [r for r in rows if "error" not in r]
    width = max([len(r["file"]) for r in rows] + [5])
    out = [f"{'file':<{width}}  {'lines':>7} {'words':>8} {'chars':>9}"]
    for r in rows:
        out.append(f"{r['file']:<{width}}  error: {r['error']}" if "error" in r
                   else f"{r['file']:<{width}}  {r['lines']:>7} {r['words']:>8} {r['chars']:>9}")
    if len(ok) > 1:
        out.append(f"{'total':<{width}}  {sum(r['lines'] for r in ok):>7} {sum(r['words'] for r in ok):>8} "
                   f"{sum(r['chars'] for r in ok):>9}")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("paths", nargs="+", help="text files, or folders whose *.md files are counted")
    ap.add_argument("--json", action="store_true", help="print a JSON list instead of a table")
    args = ap.parse_args()
    files = expand(args.paths)
    if not files:
        print("text-stats: no files found", file=sys.stderr)
        return 2
    rows = [count(f) for f in files]
    print(json.dumps(rows, ensure_ascii=False) if args.json else table(rows))
    return 1 if any("error" in r for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
