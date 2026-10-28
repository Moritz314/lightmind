"""A tiny JSON checker for `lm run --schema`: the final answer must be JSON that fits a schema.

Understands the part of JSON Schema that matters for a worker's answer: `type` (also a list),
`required`, `properties`, `items`, `enum`, `additionalProperties: false`.  Everything else in a
schema is ignored, never an error.  No dependencies.  May import: nothing.
"""
from __future__ import annotations

import json
import re

_TYPES = {"string": str, "boolean": bool, "array": list, "object": dict, "null": type(None)}
_FENCE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.S | re.I)


def _is(value, name: str) -> bool:
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    return isinstance(value, _TYPES[name]) if name in _TYPES else True      # an unknown type name: no check


def validate(value, schema: dict, path: str = "$") -> list[str]:
    """Every mismatch as one line naming the place (`$.items[2].id: expected integer, got string`)."""
    errs: list[str] = []
    want = schema.get("type")
    if want is not None:
        names = want if isinstance(want, list) else [want]
        if not any(_is(value, n) for n in names):
            return [f"{path}: expected {' or '.join(map(str, names))}, got {type(value).__name__}"]
    if "enum" in schema and value not in schema["enum"]:
        errs.append(f"{path}: must be one of {schema['enum']}")
    if isinstance(value, dict):
        errs += [f"{path}.{k}: required" for k in schema.get("required", []) if k not in value]
        props = schema.get("properties") or {}
        for k, sub in props.items():
            if k in value and isinstance(sub, dict):
                errs += validate(value[k], sub, f"{path}.{k}")
        if schema.get("additionalProperties") is False:
            errs += [f"{path}.{k}: not allowed" for k in value if k not in props]
    if isinstance(value, list) and isinstance(schema.get("items"), dict):
        for i, item in enumerate(value):
            errs += validate(item, schema["items"], f"{path}[{i}]")
    return errs


def extract_json(text: str):
    """The JSON value in a model answer: the whole text, a fenced block, or the first object or array
    in it.  ValueError when there is none."""
    text = text.strip()
    for cand in [text, *_FENCE.findall(text)]:
        try:
            return json.loads(cand)
        except ValueError:
            pass
    dec = json.JSONDecoder()
    for m in re.finditer(r"[{\[]", text):
        try:
            return dec.raw_decode(text[m.start():])[0]
        except ValueError:
            continue
    raise ValueError("the answer holds no JSON")


def check_answer(text: str, schema: dict) -> tuple[object, list[str]]:
    """(parsed value, errors).  No JSON at all is one error and the value None."""
    try:
        value = extract_json(text)
    except ValueError as e:
        return None, [str(e)]
    return value, validate(value, schema)
