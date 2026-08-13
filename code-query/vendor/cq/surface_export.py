"""Surface-symbol extraction shared by cq and the HVE inventory generator (FR-CQ-10).

The algorithm lives here so that it exists exactly once. All HVE-specific policy
(which paths belong to which execution surface, which literals are normative) is
injected by the caller, keeping this module free of HVE assumptions.

The *entrypoint* remains `hve-dev/generate_tdd_inventory.py`, as required by
FR-MAINT-05; this module deliberately exposes no CLI so that no second
entrypoint can drift from it.
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Sequence

SURFACE_FIELDNAMES = [
    "surface",
    "kind",
    "symbol",
    "file",
    "line",
    "behavior_summary",
    "rule_tokens",
    "callers_count",
]

_SHELL_FUNCTION_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*\(\)\s*\{")
_WORKFLOW_NAME_RE = re.compile(r"\s*-?\s*name:\s*(.+)")


@dataclass(frozen=True)
class SurfacePolicy:
    """Caller-supplied rules: cq itself knows nothing about HVE surfaces."""

    surface_for_path: Callable[[str], str | None]
    is_test_path: Callable[[str], bool]
    normative_literals: Sequence[str]


def compact(value: object, limit: int = 360) -> str:
    if value is None:
        return ""
    text = re.sub(r"\s+", " ", str(value)).strip()
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def safe_unparse(node: ast.AST) -> str:
    try:
        return ast.unparse(node)
    except Exception:
        return type(node).__name__


def literals_in(text: str, literals: Sequence[str]) -> str:
    return ";".join(literal for literal in literals if literal in text)


def summarize_definition(node: ast.AST, segment: str) -> str:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        doc = ast.get_docstring(node)
        if doc and doc.strip():
            return compact(doc.strip().splitlines()[0], 300)
        for child in ast.walk(node):
            if isinstance(child, ast.Raise):
                return compact(safe_unparse(child), 300)
        for child in ast.walk(node):
            if isinstance(child, ast.Return) and child.value is not None:
                return compact(safe_unparse(child), 300)
    return compact(segment.splitlines()[0] if segment else "", 300)


def python_surface_rows(
    path: str, surface: str, source: str, tree: ast.Module, usage, literals: Sequence[str]
) -> list[dict[str, object]]:
    lines = source.splitlines()
    rows: list[dict[str, object]] = []
    for node in tree.body:
        end = getattr(node, "end_lineno", None) or getattr(node, "lineno", 1)
        segment = "\n".join(lines[node.lineno - 1:end]) if hasattr(node, "lineno") else ""
        tokens = literals_in(segment, literals)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            kind = "class" if isinstance(node, ast.ClassDef) else "function"
            symbol = node.name
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and tokens:
            # 規範リテラルを保持する定数だけを索引に載せる（判定実装の一部となるため）。
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            if not names:
                continue
            kind = "constant"
            symbol = names[0]
        else:
            continue
        rows.append({
            "surface": surface,
            "kind": kind,
            "symbol": symbol,
            "file": path,
            "line": node.lineno,
            "behavior_summary": summarize_definition(node, segment),
            "rule_tokens": tokens,
            "callers_count": usage.get(symbol, 0),
        })
    return rows


def shell_surface_rows(
    path: str, surface: str, source: str, literals: Sequence[str]
) -> list[dict[str, object]]:
    lines = source.splitlines()
    starts = [
        (index, match.group(1))
        for index, line in enumerate(lines)
        if (match := _SHELL_FUNCTION_RE.match(line))
    ]
    rows: list[dict[str, object]] = []
    for position, (index, name) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else len(lines)
        segment = "\n".join(lines[index:end])
        rows.append({
            "surface": surface,
            "kind": "shell_function",
            "symbol": name,
            "file": path,
            "line": index + 1,
            "behavior_summary": compact(lines[index], 300),
            "rule_tokens": literals_in(segment, literals),
            "callers_count": sum(
                1 for line in lines
                if re.search(rf"(?<![\w-]){re.escape(name)}\b", line)
            ) - 1,
        })
    return rows


def workflow_surface_rows(
    path: str, surface: str, source: str, literals: Sequence[str]
) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    current = ""
    for number, line in enumerate(source.splitlines(), start=1):
        if match := _WORKFLOW_NAME_RE.match(line):
            current = compact(match.group(1), 200)
        tokens = literals_in(line, literals)
        if not tokens:
            continue
        rows.append({
            "surface": surface,
            "kind": "ci_rule",
            "symbol": current or PurePosixPath(path).name,
            "file": path,
            "line": number,
            "behavior_summary": compact(line, 300),
            "rule_tokens": tokens,
            "callers_count": 0,
        })
    return rows


def collect(
    repo_root: Path, files: Iterable[str], policy: SurfacePolicy
) -> list[dict[str, object]]:
    """Return the surface inventory rows, sorted deterministically."""
    from collections import Counter

    # テストは test inventory が正本なので、面横断索引では扱わない（二重索引の回避）。
    targets = [
        (path, surface)
        for path in sorted(files)
        if (surface := policy.surface_for_path(path)) and not policy.is_test_path(path)
    ]

    sources: dict[str, str] = {}
    trees: dict[str, ast.Module] = {}
    for path, _ in targets:
        if not path.endswith(".py"):
            continue
        try:
            source = (repo_root / path).read_text(encoding="utf-8", errors="replace")
            trees[path] = ast.parse(source)
        except (OSError, SyntaxError, ValueError):
            continue
        sources[path] = source

    usage: Counter = Counter()
    for tree in trees.values():
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                usage[node.id] += 1
            elif isinstance(node, ast.Attribute):
                usage[node.attr] += 1

    literals = policy.normative_literals
    rows: list[dict[str, object]] = []
    for path, surface in targets:
        if path in trees:
            rows.extend(python_surface_rows(
                path, surface, sources[path], trees[path], usage, literals
            ))
            continue
        try:
            source = (repo_root / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if path.endswith((".sh", ".bash")):
            rows.extend(shell_surface_rows(path, surface, source, literals))
        elif path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml")):
            rows.extend(workflow_surface_rows(path, surface, source, literals))
    rows.sort(key=lambda row: (
        str(row["file"]), int(str(row["line"])), str(row["kind"]), str(row["symbol"])
    ))
    return rows
