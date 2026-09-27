"""Parse the payloads of the @@MARKER: {...} lines agents write into their memory field."""
import ast
import json
import re

_BARE_JSON_WORDS = {"true": "True", "false": "False", "null": "None"}
# A bare true/false/null used as a value, i.e. not inside a quoted string.
_BARE_VALUE = re.compile(r"(?<=[:\[,\s])(true|false|null)(?=\s*[,}\]])")


def parse_marker(payload: str) -> dict | None:
    """The dict in a marker payload, or None if it isn't one.

    Agents are asked for JSON, but models sometimes write Python-style dicts
    ({'title': 'x', 'easy_apply': true}). Those used to be dropped silently,
    losing collected jobs and Q&A answers that had already cost a request.
    """
    try:
        value = json.loads(payload)
    except (ValueError, TypeError):
        pythonish = _BARE_VALUE.sub(lambda m: _BARE_JSON_WORDS[m.group(1)], payload)
        try:
            value = ast.literal_eval(pythonish)
        except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
            return None
    return value if isinstance(value, dict) else None
