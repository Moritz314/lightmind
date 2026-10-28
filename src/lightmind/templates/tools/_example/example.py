# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Example tool: count the lines, words and characters of a text file.

    uv run example.py FILE [--json]

Dependencies go into the list above; `uv run` installs them on first use.  Print the result to
stdout, errors to stderr, exit 0 on success.
"""
import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("file", help="text file to read")
    ap.add_argument("--json", action="store_true", help="print a JSON object instead of one line")
    args = ap.parse_args()
    try:
        text = Path(args.file).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"cannot read {args.file}: {e}", file=sys.stderr)
        return 1
    stats = {"lines": len(text.splitlines()), "words": len(text.split()), "chars": len(text)}
    print(json.dumps(stats) if args.json else f"{stats['lines']} {stats['words']} {stats['chars']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
