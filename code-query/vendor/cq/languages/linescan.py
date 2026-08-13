"""Shared line scanning for the regex-based extractors (FR-MAINT-07).

Brace depth drives parent attribution in the C# and JavaScript extractors, so
braces that appear inside string literals or comments must not be counted. Both
extractors share this one implementation.
"""

from __future__ import annotations

import re

_STRING_OR_COMMENT = re.compile(
    r"""
      "(?:\\.|[^"\\])*"       # double-quoted string
    | '(?:\\.|[^'\\])*'       # single-quoted string
    | `(?:\\.|[^`\\])*`       # template literal
    | //[^\n]*                # line comment
    | /\*.*?\*/               # single-line block comment
    """,
    re.VERBOSE | re.DOTALL,
)


def code_only(line: str) -> str:
    """Return ``line`` with string literals and comments blanked out."""
    return _STRING_OR_COMMENT.sub(lambda m: " " * len(m.group(0)), line)


def brace_delta(line: str) -> int:
    """Net brace depth change contributed by the code on ``line``."""
    code = code_only(line)
    return code.count("{") - code.count("}")
