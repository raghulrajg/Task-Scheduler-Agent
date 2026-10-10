"""Small helpers shared by the config-driven drivers."""

import re
from typing import Any, Dict, Iterable

_MISSING = object()
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def dig(obj: Any, path: str, default: Any = None) -> Any:
    """Read a value at a dotted path. Numeric parts index lists:
    dig({"a": [{"b": 1}]}, "a.0.b") -> 1. Returns default if missing."""
    if not path:
        return obj
    for part in path.split("."):
        if isinstance(obj, dict):
            obj = obj.get(part, _MISSING)
        elif isinstance(obj, list) and part.lstrip("-").isdigit():
            idx = int(part)
            obj = obj[idx] if -len(obj) <= idx < len(obj) else _MISSING
        else:
            return default
        if obj is _MISSING:
            return default
    return obj


def matches(value: Any, values: Iterable[Any]) -> bool:
    """Type-strict membership: 1 never matches True, "1" never matches 1."""
    return any(type(value) is type(v) and value == v for v in values)


def render(template: Any, ctx: Dict[str, Any]) -> Any:
    """Fill {placeholders} in a JSON-like template.

    A string that is exactly "{name}" is replaced by the value itself, keeping
    its type (so numeric ids stay numbers); placeholders inside longer strings
    are formatted as text. Unknown placeholders raise KeyError, so a typo in a
    config fails loudly instead of sending a half-filled request to a robot.
    """
    if isinstance(template, dict):
        return {render(k, ctx): render(v, ctx) for k, v in template.items()}
    if isinstance(template, list):
        return [render(v, ctx) for v in template]
    if isinstance(template, str):
        whole = _PLACEHOLDER.fullmatch(template)
        if whole:
            return ctx[whole.group(1)]
        return _PLACEHOLDER.sub(lambda m: str(ctx[m.group(1)]), template)
    return template
