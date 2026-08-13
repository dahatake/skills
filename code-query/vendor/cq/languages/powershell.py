"""PowerShell symbol extraction via tree-sitter (FR-CQ-04 / FR-CQ-11).

文法は 5 / 27 ファイルで誤って ERROR ノードを作り、その周辺の定義を落とす。
回復ノードが残ったファイルに限り、`pwsh` の公式パーサ（`Parser.ParseInput`）へ
エスカレーションする。ソースは stdin からデータとして渡すだけで、スクリプトは
実行しない。`pwsh` が無い環境では tree-sitter の結果をそのまま使う。
"""

from __future__ import annotations

import functools
import json
import subprocess

from cq.languages import ChunkSpan, RawSymbol, treesitter as ts

_TYPES = {
    "class_statement": "class",
    "enum_statement": "enum",
}

# 宣言の終端。この文法は `body` フィールドを持たないので、最初の本体ノードで切る。
_BODIES = ("script_block", "class_property_definition", "class_method_definition", "enum_member")

_PWSH_TIMEOUT_SECONDS = 30

# 定義と呼び出しだけを JSON で返す。`ParseInput` は構文解析のみで実行しない。
_PWSH_AST_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$reader = New-Object System.IO.StreamReader(
    [Console]::OpenStandardInput(), [System.Text.UTF8Encoding]::new($false))
$src = $reader.ReadToEnd()
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseInput($src, [ref]$null, [ref]$errors)
if ($errors.Count -gt 0) { exit 3 }

function Get-CqSignature($extent) {
    $text = $extent.Text
    $brace = $text.IndexOf('{')
    if ($brace -ge 0) { $text = $text.Substring(0, $brace) }
    return (($text -split '\s+') | Where-Object { $_ }) -join ' '
}

$symbols = @()
foreach ($node in $ast.FindAll({ param($n)
        $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -or
        $n -is [System.Management.Automation.Language.TypeDefinitionAst] -or
        $n -is [System.Management.Automation.Language.FunctionMemberAst] }, $true)) {
    $kind = 'function'
    $parent = ''
    if ($node -is [System.Management.Automation.Language.TypeDefinitionAst]) {
        $kind = if ($node.IsEnum) { 'enum' } else { 'class' }
    } elseif ($node -is [System.Management.Automation.Language.FunctionMemberAst]) {
        $kind = 'method'
        $parent = $node.Parent.Name
    } elseif ($node.Parent -is [System.Management.Automation.Language.FunctionMemberAst]) {
        continue  # クラスメソッドは FunctionMemberAst 側で既に拾っている
    }
    $symbols += [ordered]@{
        kind      = $kind
        name      = $node.Name
        parent    = $parent
        start     = $node.Extent.StartLineNumber
        end       = $node.Extent.EndLineNumber
        signature = Get-CqSignature $node.Extent
    }
}

$refs = @()
foreach ($node in $ast.FindAll({ param($n)
        $n -is [System.Management.Automation.Language.CommandAst] }, $true)) {
    $name = $node.GetCommandName()
    if ($name) { $refs += [ordered]@{ line = $node.Extent.StartLineNumber; name = $name } }
}

$json = [ordered]@{ symbols = $symbols; refs = $refs } | ConvertTo-Json -Depth 4 -Compress
$bytes = [System.Text.Encoding]::UTF8.GetBytes($json)
$stdout = [Console]::OpenStandardOutput()
$stdout.Write($bytes, 0, $bytes.Length)
$stdout.Flush()
"""


def _name(node, src) -> str:
    target = ts.named_child_by_type(node, "function_name", "simple_name")
    return ts.text(target, src) if target is not None else ""


def _signature(node, src) -> str:
    body = ts.named_child_by_type(node, *_BODIES)
    end = body.start_byte if body is not None else node.end_byte
    text = " ".join(src[node.start_byte : end].decode("utf-8", "replace").split())
    return text.rstrip("{ ")[:200]


def _callee(node, src) -> str:
    target = ts.named_child_by_type(node, "command_name")
    return ts.text(target, src) if target is not None else ""


GRAMMAR = ts.Grammar(
    lang="powershell",
    module="tree_sitter_powershell",
    kinds={
        **_TYPES,
        "function_statement": "function",
        "class_method_definition": "method",
    },
    scopes=_TYPES,
    name_of=_name,
    scope_name_of=_name,
    doc_markers=("#",),
    signature_of=_signature,
    # `Import-Module` や `using module` も文法上はただの command なので、
    # import ではなく参照として記録される。
    call_nodes=frozenset({"command"}),
    callee_of=_callee,
)


def _official_ast(source: str):
    """Parsed output of `pwsh`, or `None` when it is absent or the source is invalid."""
    try:
        proc = subprocess.run(
            ["pwsh", "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", _PWSH_AST_SCRIPT],
            input=source.encode("utf-8"),
            capture_output=True,
            timeout=_PWSH_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    try:
        return json.loads(proc.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _doc_head(lines: list[str], start_line: int) -> str | None:
    """tree-sitter 経路は前の兄弟ノードを見るので、間の空行は同じく読み飛ばす。"""
    for index in range(start_line - 2, -1, -1):
        line = lines[index].strip()
        if line:
            return line if line.startswith("#") else None
    return None


def _official_symbols(source: str, parsed) -> tuple[RawSymbol, ...]:
    lines = source.splitlines()
    total = max(len(lines), 1)
    out = []
    for row in parsed["symbols"]:
        name, parent = row["name"], row["parent"] or None
        start = min(row["start"], total)
        out.append(RawSymbol(
            name=name,
            qualname=f"{parent}.{name}" if parent else name,
            kind=row["kind"],
            start_line=start,
            end_line=min(max(row["end"], start), total),
            signature=row["signature"][:200],
            parent=parent,
            doc_head=_doc_head(lines, start),
        ))
    out.sort(key=lambda s: (s.start_line, s.qualname, s.kind))
    return tuple(out)


@functools.lru_cache(maxsize=1)
def _analyse(source: str):
    """`(symbols, refs, escalated, has_error)`. Cached because the indexer asks three times."""
    symbols = ts.extract(GRAMMAR, source)
    refs, _ = ts.extract_graph(GRAMMAR, source)
    has_error = ts.parse(GRAMMAR, source).root_node.has_error
    if not has_error:
        return symbols, refs, False, False
    parsed = _official_ast(source)
    if parsed is None:
        return symbols, refs, False, True
    official_refs = tuple((row["line"], row["name"]) for row in parsed["refs"])
    return _official_symbols(source, parsed), official_refs, True, True


def extract(source: str) -> tuple[RawSymbol, ...]:
    return _analyse(source)[0]


def extract_ex(source: str) -> tuple[tuple[RawSymbol, ...], str]:
    symbols, _refs, escalated, has_error = _analyse(source)
    # A successful `pwsh` escalation replaces the tree-sitter result outright
    # (the official parser has no ERROR nodes of its own), so only a
    # non-escalated ERROR recovery counts as partial fidelity.
    parser = "tree-sitter-partial" if has_error and not escalated else "tree-sitter"
    return symbols, parser


def chunk_spans(source: str, lines: list[str], max_chars: int) -> tuple[ChunkSpan, ...]:
    symbols, _, escalated, _has_error = _analyse(source)
    if not escalated:
        return ts.chunk_spans(GRAMMAR, source, lines, max_chars)
    # 公式パーサ経路は構文木を返さないので、定義の範囲を span にして core に隙間を埋めさせる。
    return tuple(
        ChunkSpan(s.start_line, s.end_line, s.qualname, s.signature)
        for s in symbols
        if s.kind != "method"
    )


def extract_graph(source: str):
    return _analyse(source)[1], ()
