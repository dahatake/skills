"""Low-fidelity regex extractor used when a full parser is unavailable (FR-CQ-11).

It never raises: a degraded index entry is strictly better than a missing file,
as long as the reduced fidelity is recorded and reported to the caller.
"""

from __future__ import annotations

import re

from cq.languages import RawSymbol

_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # 修飾子は言語ごとに多様（public sealed / internal static partial ...）なので
    # 個別に列挙せず、識別子と空白だけの前置きを許す。
    (re.compile(r"^[\w\s]*\b(?:class|record|struct|interface)\s+(?P<name>\w+)"), "class"),
    (re.compile(r"^\s*(?:async\s+)?def\s+(?P<name>\w+)\s*\("), "function"),
    (re.compile(r"^\s*(?:export\s+)?(?:async\s+)?function\s+(?P<name>\w+)\s*\("), "function"),
    # POSIX シェル関数。`name() {`
    (re.compile(r"^\s*(?:function\s+)?(?P<name>[A-Za-z_][A-Za-z0-9_]*)\s*\(\)\s*\{"), "function"),
    # PowerShell 関数。`function Name {` / `function Verb-Noun(`
    (re.compile(r"^\s*function\s+(?P<name>[A-Za-z_][\w-]*)"), "function"),
    # 複数行シグネチャの C# / Java 風メソッド。行末が `(` で終わる継続行も拾う。
    (re.compile(r"^\s*(?:public|private|protected|internal|static|async|override|virtual|sealed)"
                r"[\w\s<>,\[\]\?\.]*\s+(?P<name>\w+)\s*\((?:[^;]*\)\s*\{?\s*|\s*)$"), "method"),
)


def extract(source: str) -> tuple[RawSymbol, ...]:
    found: list[RawSymbol] = []
    for number, line in enumerate(source.splitlines(), start=1):
        for pattern, kind in _PATTERNS:
            match = pattern.match(line)
            if not match:
                continue
            name = match.group("name")
            found.append(RawSymbol(
                name=name,
                qualname=name,
                kind=kind,
                start_line=number,
                end_line=number,
                signature=line.strip()[:200],
                is_test=name.startswith("test_") or name.startswith("Test"),
            ))
            break
    return tuple(found)
