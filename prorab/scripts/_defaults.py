#!/usr/bin/env python3
"""Loader for prorab/references/defaults.json.

Keys whose names end in ``_path`` or ``_dir`` hold filesystem paths; they are returned
with ``~`` expanded to the user's home directory.
"""

import json
import os
import re

DEFAULT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "references",
    "defaults.json",
)


def _expand(node):
    if isinstance(node, dict):
        out = {}
        for key, value in node.items():
            if key.endswith(("_path", "_dir")) and isinstance(value, str):
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


_LONG_OPTION = re.compile(r"^--[a-z][a-z0-9_-]*$")


def cli_argv(argv):
    """Spell long options with hyphens: `--caller_pane` -> `--caller-pane`.

    Only the option name (the part before the first `=`) changes; a bare `--` ends the
    rewriting, and every other token, values included, stays as it is."""
    out = []
    for i, token in enumerate(argv):
        if token == "--":
            return out + list(argv[i:])
        name, eq, value = token.partition("=")
        if "_" in name and _LONG_OPTION.match(name):
            token = name.replace("_", "-") + eq + value
        out.append(token)
    return out
