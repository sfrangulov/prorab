#!/usr/bin/env python3
"""Loader for prorab/references/defaults.json.

Keys whose names end in ``_path`` hold filesystem paths; they are returned
with ``~`` expanded to the user's home directory.
"""

import json
import os

DEFAULT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "references",
    "defaults.json",
)


def _expand(node):
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if key.endswith("_path") and isinstance(value, str):
                out[key] = os.path.expanduser(value)
            else:
                out[key] = _expand(value)
        return out
    if isinstance(node, list):
        return [_expand(item) for item in node]
    return node


def load(path=None):
    """Return the defaults dict; ``path`` defaults to references/defaults.json."""
    with open(path or DEFAULT_PATH, encoding="utf-8") as fh:
        return _expand(json.load(fh))
