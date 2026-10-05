"""Markdown indexer: extracts frontmatter and heading-based chunks.

Also indexes declared tabular files (CSV / TSV) row-by-row (FR-MDQ-02).

Pure-stdlib implementation. Handles fenced code blocks so '#' inside code
is not misread as a heading.
"""
from __future__ import annotations

import csv
import fnmatch
import hashlib
import io
import json
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable

from mdq import config as mdq_config

try:
    import yaml  # PyYAML is already a project dependency
except Exception:  # pragma: no cover
    yaml = None

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
FENCE_RE = re.compile(r"^(`{3,}|~{3,})")


def _segment_by_fence(lines: list[str]) -> list[tuple[str, list[str]]]:
    """Group consecutive lines into ('fence', [...]) or ('text', [...]) blocks.

    A fenced block is inclusive of its opening and closing fence lines and is
    treated as indivisible by the subdivider. Unterminated fences (no closing
    marker) collapse into a single text segment to avoid losing content.
    """
    segments: list[tuple[str, list[str]]] = []
    i = 0
    n = len(lines)
    while i < n:
        m = FENCE_RE.match(lines[i])
        if m:
            marker = m.group(1)[:3]
            j = i + 1
            closed = False
            while j < n:
                if lines[j].startswith(marker):
                    closed = True
                    break
                j += 1
            if closed:
                segments.append(("fence", lines[i:j + 1]))
                i = j + 1
                continue
            # Unterminated fence -> treat the rest as a single text segment
            # (we never lose content).
        # Accumulate text lines until the next fence opener.
        j = i
        while j < n:
            mm = FENCE_RE.match(lines[j])
            if mm:
                # Peek: only treat as fence if it has a matching close, else
                # keep as text.
                marker = mm.group(1)[:3]
                k = j + 1
                while k < n and not lines[k].startswith(marker):
                    k += 1
                if k < n:
                    break  # real fence ahead, stop text accumulation
            j += 1
        segments.append(("text", lines[i:j]))
        i = j
    return segments


def _split_text_segment(seg_lines: list[str], max_chars: int) -> list[list[str]]:
    """Split a 'text' segment into sub-segments under max_chars.

    Splits at blank-line paragraph boundaries first. Paragraphs that are still
    over the budget are further split line-by-line. As a last resort, a single
    long line is hard-cut by character count.
    """
    if max_chars <= 0:
        return [seg_lines]
    # Build paragraphs (list of list-of-lines) separated by blank lines.
    paragraphs: list[list[str]] = []
    current: list[str] = []
    for ln in seg_lines:
        if ln.strip() == "":
            if current:
                paragraphs.append(current)
                current = []
            # blank line itself is dropped as separator
        else:
            current.append(ln)
    if current:
        paragraphs.append(current)

    # Helper: split a single oversized paragraph by lines, hard-cutting any
    # single line that still exceeds max_chars.
    def _split_paragraph(par: list[str]) -> list[list[str]]:
        out: list[list[str]] = []
        buf: list[str] = []
        size = 0
        for ln in par:
            ln_len = len(ln) + 1
            if ln_len > max_chars:
                # hard cut the single long line
                if buf:
                    out.append(buf)
                    buf, size = [], 0
                for start in range(0, len(ln), max_chars):
                    out.append([ln[start:start + max_chars]])
                continue
            if size + ln_len > max_chars and buf:
                out.append(buf)
                buf, size = [], 0
            buf.append(ln)
            size += ln_len
        if buf:
            out.append(buf)
        return out

    # Greedy-pack paragraphs into sub-segments. Oversized paragraphs are
    # pre-split.
    expanded: list[list[str]] = []
    for par in paragraphs:
        par_len = sum(len(x) + 1 for x in par)
        if par_len > max_chars:
            expanded.extend(_split_paragraph(par))
        else:
            expanded.append(par)

    sub_segments: list[list[str]] = []
    buf: list[str] = []
    size = 0
    for par in expanded:
        par_len = sum(len(x) + 1 for x in par)
        sep = 1 if buf else 0  # blank line between paragraphs in the same sub
        if buf and size + sep + par_len > max_chars:
            sub_segments.append(buf)
            buf, size = [], 0
            sep = 0
        if buf:
            buf.append("")  # restore blank line as separator
            size += 1
        buf.extend(par)
        size += par_len
    if buf:
        sub_segments.append(buf)
    return sub_segments


def _subdivide(text: str, start_line: int, max_chars: int,
               overlap_paragraphs: int = 0
               ) -> list[tuple[str, int, int]]:
    """Split text into sub-chunks of at most ``max_chars`` characters.

    Returns a list of (text, start_line, end_line) tuples (1-based, inclusive).
    Fenced code blocks (``` or ~~~) are kept intact and never split. When
    ``max_chars`` is 0 or negative, a single tuple is returned unchanged.

    Algorithm (content-aware, recursive-character-style):
      1. Split into 'fence' (indivisible) and 'text' segments.
      2. Each text segment is split at paragraph boundaries (\\n\\n).
      3. Paragraphs that still exceed the budget are split line-by-line.
      4. Lines longer than the budget are hard-cut by character count.

    ``overlap_paragraphs`` (>= 0): when >0 and the input is split into
    multiple text sub-chunks, the trailing N paragraphs of the previous text
    sub-chunk are prepended to the next one as an overlap window. Fenced
    code blocks are NEVER duplicated by overlap (they remain singletons).
    The duplicated overlap lines retain their *original* line numbers so
    downstream search can dedup hits by (path, start_line, end_line).
    """
    lines = text.splitlines()
    if not lines:
        return [(text, start_line, start_line)]
    if max_chars <= 0:
        end_line = start_line + len(lines) - 1
        return [(text, start_line, end_line)]

    segments = _segment_by_fence(lines)

    # Materialise each segment into 0..N sub-segments (list of line-lists)
    # together with their original line offsets so we can compute correct
    # start/end_line per output chunk.
    out: list[tuple[str, int, int]] = []
    cursor_line = start_line  # 1-based line tracker

    # Walk segments and split text ones. For fences we emit a single sub.
    pending_subs: list[list[str]] = []  # sub-segments awaiting flush/pack

    def _flush_pending(emit_line_start: int) -> int:
        """Emit pending text sub-segments as separate chunks; return new cursor."""
        nonlocal out
        line = emit_line_start
        for sub in pending_subs:
            if not sub:
                continue
            body = "\n".join(sub)
            s = line
            e = line + len(sub) - 1
            out.append((body, s, e))
            # Account for the blank line separator that used to sit between
            # paragraphs inside this sub: none added here because we keep
            # blanks inside the sub itself.
            line = e + 1
        pending_subs.clear()
        return line

    for kind, seg_lines in segments:
        if kind == "fence":
            # Flush any pending text subs first (preserve order).
            cursor_line = _flush_pending(cursor_line)
            body = "\n".join(seg_lines)
            s = cursor_line
            e = cursor_line + len(seg_lines) - 1
            out.append((body, s, e))
            cursor_line = e + 1
        else:  # text
            subs = _split_text_segment(seg_lines, max_chars)
            # When the whole text segment fits and there are no other subs
            # queued, we can still emit it as a single chunk.
            pending_subs.extend(subs)
            cursor_line = _flush_pending(cursor_line)

    # Final flush (no-op if already flushed).
    _flush_pending(cursor_line)

    # If no chunks emitted (e.g. all blank lines), return original.
    if not out:
        end_line = start_line + len(lines) - 1
        return [(text, start_line, end_line)]

    # Apply paragraph-level overlap (text sub-chunks only). The previous
    # sub-chunk's trailing N paragraphs are prepended to the next sub-chunk.
    # Fence-only sub-chunks are skipped as donor and as receiver to keep code
    # blocks unique and avoid BM25 score inflation on identical fence text.
    if overlap_paragraphs and overlap_paragraphs > 0 and len(out) > 1:
        out = _apply_paragraph_overlap(out, overlap_paragraphs)

    return out


def _is_fence_text(text: str) -> bool:
    s = text.lstrip()
    return s.startswith("```") or s.startswith("~~~")


def _trailing_paragraphs(text: str, n: int) -> tuple[str, int]:
    """Return (overlap_text, line_count) for the last ``n`` paragraphs.

    A paragraph is a run of non-blank lines separated by one or more blank
    lines. Fenced sub-chunks (input starting with a fence marker) return
    ("", 0) — overlap is suppressed for code blocks.
    """
    if n <= 0 or not text or _is_fence_text(text):
        return "", 0
    lines = text.splitlines()
    # Walk from the end, collecting paragraphs.
    paragraphs: list[list[str]] = []
    current: list[str] = []
    for ln in reversed(lines):
        if ln.strip() == "":
            if current:
                paragraphs.append(list(reversed(current)))
                current = []
                if len(paragraphs) >= n:
                    break
        else:
            current.append(ln)
    if current and len(paragraphs) < n:
        paragraphs.append(list(reversed(current)))
    if not paragraphs:
        return "", 0
    # paragraphs were collected back-to-front; restore forward order.
    paragraphs.reverse()
    flat: list[str] = []
    for i, par in enumerate(paragraphs):
        if i > 0:
            flat.append("")
        flat.extend(par)
    return "\n".join(flat), len(flat)


def _apply_paragraph_overlap(
    parts: list[tuple[str, int, int]], overlap_paragraphs: int
) -> list[tuple[str, int, int]]:
    """Prepend trailing paragraphs of the previous text sub-chunk to each next
    text sub-chunk. Fenced sub-chunks are passed through unchanged.

    The overlap text keeps its *original* line numbers; the resulting
    (start_line, end_line) range therefore widens leftward to include the
    overlap region. This makes downstream deduplication by (path, start, end)
    natural while preserving traceability.
    """
    out: list[tuple[str, int, int]] = []
    prev_text: str | None = None
    prev_end: int | None = None
    for body, s, e in parts:
        if prev_text is not None and prev_end is not None and not _is_fence_text(body):
            overlap_text, overlap_lines = _trailing_paragraphs(
                prev_text, overlap_paragraphs
            )
            if overlap_text:
                # Splice overlap in front; line numbers shift left by
                # overlap_lines + 1 (the blank separator we add).
                new_body = overlap_text + "\n\n" + body
                new_start = max(1, s - (overlap_lines + 1))
                out.append((new_body, new_start, e))
            else:
                out.append((body, s, e))
        else:
            out.append((body, s, e))
        # Only text sub-chunks donate overlap downstream.
        if not _is_fence_text(body):
            prev_text, prev_end = body, e
        else:
            # Reset donor — overlap must come from the immediately-preceding
            # text sub-chunk, not from a sub-chunk across a fence boundary.
            prev_text, prev_end = None, None
    return out


@dataclass
class Chunk:
    path: str
    heading_path: str
    level: int
    start_line: int
    end_line: int
    text: str
    tags: list[str] = field(default_factory=list)
    part_index: int = 0
    part_total: int = 1
    # Occurrence index of this heading_path within the file (0 for the first
    # appearance). Used to make chunk_id stable against line shifts and to
    # disambiguate duplicate headings. Assigned in index_one_file.
    occurrence_index: int = 0
    # chunk_id of the nearest ancestor heading chunk within the same file.
    # NULL/None for: top-level headings, preface chunks, fixed_window chunks.
    # Sub-parts of a subdivided heading share the same parent as the heading
    # itself (i.e. one level up in the heading hierarchy). Populated in
    # index_one_file after occurrence_index assignment.
    parent_chunk_id: str | None = None
    # Original body before any contextualizer template was prepended (v5).
    # When None, ``text`` is already the raw body. Populated by the
    # `semantic_paragraph` strategy when contextualization is enabled
    # (Q11=B default ON).
    text_raw: str | None = None
    # Float32 chunk embedding bytes from late chunking (v5, Q9=B). When
    # None, this chunk has no associated dense vector.
    embedding_bytes: bytes | None = None
    # Per-chunk summary (v6) populated by the `pageindex` strategy. NULL
    # for chunks produced by other strategies.
    summary: str | None = None

    @property
    def chunk_id(self) -> str:
        # Stable across line-number shifts: SHA1(path \0 heading_path \0
        # occurrence_index \0 part_index). Duplicate (heading_path,
        # occurrence_index, part_index) within one file is prevented by the
        # assignment logic in index_one_file.
        key = (
            f"{self.path}\0{self.heading_path}"
            f"\0{self.occurrence_index}\0{self.part_index}"
        )
        return hashlib.sha1(key.encode("utf-8")).hexdigest()

    @property
    def token_est(self) -> int:
        # Conservative estimate: ~4 chars/token average for mixed JA/EN.
        return max(1, len(self.text) // 4)


def _parse_frontmatter(text: str) -> tuple[dict, int]:
    """Return (frontmatter_dict, body_start_line_index_0based)."""
    if not text.startswith("---\n") and not text.startswith("---\r\n"):
        return {}, 0
    lines = text.splitlines()
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end = i
            break
    if end is None:
        return {}, 0
    body_offset = end + 1
    if yaml is None:
        return {}, body_offset
    try:
        fm = yaml.safe_load("\n".join(lines[1:end])) or {}
        if not isinstance(fm, dict):
            fm = {}
        return fm, body_offset
    except Exception:
        return {}, body_offset


def _split_chunks(path: str, lines: list[str], body_start: int,
                  tags: list[str], max_chunk_chars: int = 0,
                  overlap_paragraphs: int = 0) -> list[Chunk]:
    """Split body into heading-bounded chunks.

    A chunk spans from a heading line until the next heading of equal-or-lower
    depth. The pre-heading preface (lines before first heading) becomes a
    synthetic chunk with heading_path = '(preface)'.

    When ``max_chunk_chars`` > 0, each heading chunk whose body exceeds the
    budget is further subdivided via :func:`_subdivide` (paragraph-first,
    line-fallback, fence-preserving). Sub-chunks share the same heading_path
    and level; their ordering is recorded in part_index / part_total.
    ``overlap_paragraphs`` (>=0) instructs :func:`_subdivide` to prepend the
    trailing N paragraphs of each text sub-chunk to the next.
    """
    chunks: list[Chunk] = []
    in_fence = False
    fence_marker = ""
    heading_stack: list[tuple[int, str]] = []  # (level, title)
    current_start: int | None = None
    current_level = 0
    current_title = "(preface)"
    buf: list[str] = []

    def flush(end_line: int) -> None:
        nonlocal buf, current_start, current_level, current_title
        if current_start is None:
            return
        text = "\n".join(buf).strip("\n")
        if text.strip():
            hp = " > ".join(t for _, t in heading_stack) or current_title
            base_start = current_start + 1  # 1-based
            if max_chunk_chars and len(text) > max_chunk_chars:
                parts = _subdivide(text, base_start, max_chunk_chars,
                                   overlap_paragraphs=overlap_paragraphs)
                total = len(parts)
                for i, (sub_text, s, e) in enumerate(parts):
                    chunks.append(Chunk(
                        path=path, heading_path=hp, level=current_level,
                        start_line=s, end_line=e,
                        text=sub_text, tags=list(tags),
                        part_index=i, part_total=total,
                    ))
            else:
                chunks.append(Chunk(
                    path=path, heading_path=hp, level=current_level,
                    start_line=base_start,
                    end_line=end_line,
                    text=text, tags=list(tags),
                ))
        buf = []
        current_start = None

    for idx in range(body_start, len(lines)):
        line = lines[idx]
        fm = FENCE_RE.match(line)
        if fm:
            marker = fm.group(1)
            if not in_fence:
                in_fence = True
                fence_marker = marker[:3]
            elif line.startswith(fence_marker):
                in_fence = False

        if not in_fence:
            hm = HEADING_RE.match(line)
            if hm:
                # flush previous chunk up to previous line
                flush(idx)
                level = len(hm.group(1))
                title = hm.group(2).strip()
                # pop stack to maintain hierarchy
                while heading_stack and heading_stack[-1][0] >= level:
                    heading_stack.pop()
                heading_stack.append((level, title))
                current_start = idx
                current_level = level
                current_title = title
                buf = [line]
                continue

        if current_start is None:
            # preface region
            current_start = idx
            current_level = 0
            current_title = "(preface)"
        buf.append(line)

    flush(len(lines))
    return chunks


def scan_file(repo_root: Path, file_path: Path,
              max_chunk_chars: int = 0,
              overlap_paragraphs: int = 0) -> tuple[dict, list[Chunk]]:
    text = file_path.read_text(encoding="utf-8", errors="replace")
    fm, body_offset = _parse_frontmatter(text)
    lines = text.splitlines()
    rel = file_path.relative_to(repo_root).as_posix()
    tags = []
    raw_tags = fm.get("tags") if isinstance(fm, dict) else None
    if isinstance(raw_tags, list):
        tags = [str(t) for t in raw_tags]
    elif isinstance(raw_tags, str):
        tags = [raw_tags]
    chunks = _split_chunks(rel, lines, body_offset, tags,
                           max_chunk_chars=max_chunk_chars,
                           overlap_paragraphs=overlap_paragraphs)
    return fm, chunks


# Dependency and build trees are never documentation. They are skipped
# unconditionally so that a repository cannot accidentally index thousands of
# Markdown files shipped by its own dependencies.
ALWAYS_EXCLUDED_DIRS: frozenset[str] = frozenset({
    ".git", ".mdq", ".cq", "node_modules", "__pycache__",
    "dist", "build", ".next", ".cache", "venv",
})


def _in_excluded_dir(rel: PurePosixPath, skip: int = 0) -> bool:
    """True when a directory below the requested root is a dependency or build tree.

    ``skip`` is the depth of the root the caller asked for, so naming ``build``
    as a root still indexes it while ``docs/build`` stays out.
    """
    return any(
        part in ALWAYS_EXCLUDED_DIRS or part.startswith(".venv")
        for part in rel.parts[skip:-1]
    )


def _match_segments(parts: tuple[str, ...], pattern: tuple[str, ...]) -> bool:
    if not pattern:
        return not parts
    head, rest = pattern[0], pattern[1:]
    if head == "**":
        if not rest:
            return True
        return any(_match_segments(parts[i:], rest) for i in range(len(parts) + 1))
    if not parts or not fnmatch.fnmatchcase(parts[0], head):
        return False
    return _match_segments(parts[1:], rest)


def _matches_glob(rel: str, pattern: str) -> bool:
    """Case-sensitive glob match. ``*`` stays inside one path segment; ``**``
    spans any number of them, including none."""
    cleaned = pattern.strip("/")
    if not cleaned:
        return False
    return _match_segments(PurePosixPath(rel).parts, tuple(cleaned.split("/")))


def iter_markdown(root: Path, roots: Iterable[str],
                  excludes: Iterable[str] | None = None) -> Iterable[Path]:
    """Yield the Markdown files under ``roots`` that should be indexed.

    ``excludes`` are repo-root-relative globs; when omitted they are read from
    ``[index].exclude``. Every caller therefore honours the configuration
    without having to thread the value through.
    """
    patterns = (
        list(excludes) if excludes is not None
        else mdq_config.resolve_excludes(root)
    )
    for r in roots:
        base = (root / r).resolve()
        if not base.exists():
            continue
        cleaned = str(r).strip("/")
        depth = 0 if cleaned in ("", ".") else len(PurePosixPath(cleaned).parts)
        for p in base.rglob("*.md"):
            if not p.is_file():
                continue
            try:
                rel = p.relative_to(root).as_posix()
            except ValueError:
                yield p
                continue
            if _in_excluded_dir(PurePosixPath(rel), skip=depth):
                continue
            if any(_matches_glob(rel, pattern) for pattern in patterns):
                continue
            yield p


# --- Tabular (CSV / TSV) row-level indexing (FR-MDQ-02) -------------------

TABULAR_SUFFIXES: frozenset[str] = frozenset({".csv", ".tsv"})

# Number of leading columns used to build the machine-generated context
# header (heading_path) and tags for each row.
TABULAR_CONTEXT_COLUMNS = 3


def iter_tabular(root: Path, globs: Iterable[str] | None) -> Iterable[Path]:
    """Yield tabular files matching repo-root-relative glob patterns.

    Absolute patterns and patterns containing ``..`` are rejected so a config
    file cannot pull content from outside the repository. The same dependency
    and ``[index].exclude`` filtering as ``iter_markdown`` applies, so a broad
    pattern cannot drag in a vendored tool's own data.
    """
    excludes = mdq_config.resolve_excludes(root)
    seen: set[Path] = set()
    for raw in globs or []:
        pattern = str(raw).replace("\\", "/").strip()
        if not pattern:
            continue
        if pattern.startswith("/") or ".." in pattern.split("/") or ":" in pattern:
            continue
        for p in sorted(root.glob(pattern)):
            if not p.is_file() or p.suffix.lower() not in TABULAR_SUFFIXES:
                continue
            try:
                rel = p.relative_to(root).as_posix()
            except ValueError:
                continue
            if _in_excluded_dir(PurePosixPath(rel)):
                continue
            if any(_matches_glob(rel, item) for item in excludes):
                continue
            resolved = p.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            yield p


def _row_context(header: list[str], row: list[str],
                 limit: int = TABULAR_CONTEXT_COLUMNS) -> list[str]:
    """``name=value`` for the first ``limit`` columns, skipping empty values."""
    out: list[str] = []
    for i, name in enumerate(header[:limit]):
        value = row[i].strip() if i < len(row) else ""
        if not name or not value:
            continue
        out.append(f"{name}={value}")
    return out


def scan_tabular_file(repo_root: Path,
                      file_path: Path) -> tuple[dict, list[Chunk]]:
    """Turn each data row of a CSV / TSV file into one :class:`Chunk`.

    ``start_line`` / ``end_line`` are **physical** line numbers, so a record
    with a quoted newline spans more than one line. The header row is never
    emitted as a chunk.
    """
    rel = file_path.relative_to(repo_root).as_posix()
    stem = file_path.stem
    delimiter = "\t" if file_path.suffix.lower() == ".tsv" else ","
    text = file_path.read_text(encoding="utf-8-sig", errors="replace")

    chunks: list[Chunk] = []
    header: list[str] | None = None
    prev_line = 0
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    for row in reader:
        start_line = prev_line + 1
        end_line = max(reader.line_num, start_line)
        prev_line = end_line
        if header is None:
            header = [str(c).strip() for c in row]
            continue
        pairs = [
            (name, row[i].strip())
            for i, name in enumerate(header)
            if name and i < len(row) and row[i].strip()
        ]
        if not pairs:
            continue
        context = _row_context(header, [str(c) for c in row])
        chunks.append(Chunk(
            path=rel,
            heading_path=" > ".join([stem, *context]),
            level=0,
            start_line=start_line,
            end_line=end_line,
            text="\n".join(f"{n}: {v}" for n, v in pairs),
            tags=context,
        ))
    return {}, chunks


def _sha1_bytes(b: bytes) -> str:
    return hashlib.sha1(b).hexdigest()


def index_one_file(repo_root: Path, file_path: Path, conn,
                   rebuild: bool = False,
                   max_chunk_chars: int = 0,
                   strategy: str = "heading",
                   overlap_paragraphs: int | None = None) -> dict:
    """Index a single Markdown file incrementally.

    Used by both ``build_index`` (batch walk) and the realtime watcher
    (``mdq.watcher``). Returns a small status dict with keys:
      - action: "indexed" | "skipped" | "missing"
      - chunks: int (rows written; 0 when skipped/missing)
      - sha1:   str | None
    Behaviour:
      - If ``file_path`` does not exist, returns ``action="missing"``.
      - If ``rebuild`` is False and the stored sha1 matches, returns
        ``action="skipped"``.
      - Otherwise upserts the file row, replaces chunk rows, and returns
        ``action="indexed"``.
    The caller is responsible for committing the transaction.
    """
    from . import store as _store  # local import to avoid cycles

    if not file_path.exists() or not file_path.is_file():
        return {"action": "missing", "chunks": 0, "sha1": None}

    raw = file_path.read_bytes()
    sha1 = _sha1_bytes(raw)
    mtime = file_path.stat().st_mtime
    rel = file_path.relative_to(repo_root).as_posix()

    if not rebuild:
        existing = _store.get_file_meta(conn, rel)
        if existing and existing[0] == sha1:
            return {"action": "skipped", "chunks": 0, "sha1": sha1}

    # Strategy dispatch: 'heading' uses the legacy scan_file path with
    # max_chunk_chars honoured (back-compat). Other strategies override.
    # NOTE: overlap_paragraphs is intentionally suppressed for the bare
    # 'heading' strategy (Q3=A in the design plan): overlap applies only
    # to 'heading_recursive'. Even when a user passes
    # --strategy heading --overlap-paragraphs 2, we force overlap=0 here.
    is_tabular = file_path.suffix.lower() in TABULAR_SUFFIXES
    if is_tabular:
        # Row chunks are strategy-independent by contract (FR-MDQ-02).
        fm, chunks = scan_tabular_file(repo_root, file_path)
    elif strategy and strategy != "heading":
        from . import strategies as _strat
        fm, chunks = _strat.scan_file_for_strategy(
            repo_root, file_path, _strat.normalize(strategy),
            max_chunk_chars=max_chunk_chars,
            overlap_paragraphs=overlap_paragraphs,
        )
    else:
        fm, chunks = scan_file(
            repo_root, file_path,
            max_chunk_chars=max_chunk_chars,
            overlap_paragraphs=0,  # Q3=A: heading strategy never overlaps.
        )
    # Assign occurrence_index per (heading_path) within this file. Sub-parts
    # of the same heading share the same occurrence_index — they are
    # disambiguated by part_index in chunk_id.
    occ_counter: dict[str, int] = {}
    last_key: tuple[str, int] | None = None
    for c in chunks:
        key = (c.heading_path, c.start_line)
        # Same (heading_path, start_line) means a sub-part: reuse occurrence.
        if last_key is not None and last_key[0] == c.heading_path and c.part_index > 0:
            c.occurrence_index = occ_counter[c.heading_path] - 1
        else:
            c.occurrence_index = occ_counter.get(c.heading_path, 0)
            occ_counter[c.heading_path] = c.occurrence_index + 1
        last_key = key

    # Populate parent_chunk_id (nearest ancestor heading chunk).
    #
    # Lookup table: heading_path -> (chunk_id of first occurrence, part_index=0).
    # The "parent" is resolved by rsplit(" > ", 1) on heading_path. For
    # fixed_window rows ('(window)') or preface rows, parent stays None.
    # For sub-parts (part_index > 0) of a subdivided heading, parent is the
    # *ancestor* heading (one level up), NOT the part_index=0 sibling — this
    # matches the user requirement that parent = nearest ancestor heading.
    by_hp: dict[str, str] = {}
    for c in chunks:
        # Skip synthetic heading_paths so they cannot be referenced as parents.
        if c.heading_path in ("(preface)", "(window)"):
            continue
        # Record only the first chunk_id per heading_path (part_index == 0)
        # so siblings share a stable parent reference target.
        if c.heading_path not in by_hp and c.part_index <= 0:
            by_hp[c.heading_path] = c.chunk_id
    for c in chunks:
        # Tabular rows have no heading hierarchy; a " > " prefix match would
        # link unrelated rows to each other.
        if is_tabular:
            break
        hp = c.heading_path or ""
        if not hp or hp in ("(preface)", "(window)"):
            continue
        if " > " not in hp:
            # Top-level heading: no ancestor within this file.
            continue
        parent_hp = hp.rsplit(" > ", 1)[0]
        c.parent_chunk_id = by_hp.get(parent_hp)

    # `default=str`: unquoted YAML dates load as `datetime.date`, which json refuses.
    fm_json = json.dumps(fm, ensure_ascii=False, default=str) if fm else None
    _store.upsert_file(conn, rel, sha1, mtime, len(raw), fm_json)
    _store.delete_chunks_for(conn, rel)
    rows = [(
        c.chunk_id, c.path, c.heading_path, c.level,
        c.start_line, c.end_line, c.token_est, c.text,
        json.dumps(c.tags, ensure_ascii=False) if c.tags else None,
        c.part_index, c.part_total, c.parent_chunk_id,
        c.text_raw, c.embedding_bytes, c.summary,
    ) for c in chunks]
    _store.insert_chunks(conn, rows)
    return {"action": "indexed", "chunks": len(rows), "sha1": sha1}


def delete_one_file(rel_path: str, conn) -> dict:
    """Remove a file (and its chunks) from the index by relative path.

    Returns ``{"action": "deleted", "chunks": N}`` where N is the number of
    chunk rows removed. If the file is not in the store, returns
    ``{"action": "absent", "chunks": 0}``. The caller commits.
    """
    from . import store as _store

    if rel_path not in _store.list_all_paths(conn):
        return {"action": "absent", "chunks": 0}
    removed = _store.delete_file(conn, rel_path)
    return {"action": "deleted", "chunks": int(removed)}


def build_index(repo_root: Path, roots: Iterable[str], conn,
                rebuild: bool = False, prune: bool = True,
                max_chunk_chars: int = 0,
                strategy: str = "heading",
                overlap_paragraphs: int | None = None,
                progress_callback=None,
                tabular_globs: Iterable[str] | None = None) -> dict:
    """Walk Markdown files under roots and persist chunks.

    ``tabular_globs`` (optional): repo-root-relative glob patterns of CSV /
    TSV files indexed row-by-row (FR-MDQ-02). ``None``/empty means no tabular
    file is indexed.

    Returns a summary dict: {files_indexed, files_skipped, chunks_written,
    pruned_files, pruned_chunks}.
    Incremental: skips files whose (sha1, mtime) match the store.
    Prune (default on): files that previously lived under any of the given
    roots but are no longer present on disk are removed from the store
    (chunks are removed via ON DELETE CASCADE). Files outside the given
    roots are left untouched.

    ``progress_callback`` (optional, default None): when provided, called
    after each file with the signature
    ``Callable[[str, int, int], None]`` -- ``(message, current, total)``.
    The callable must be thread-safe with respect to the caller's UI
    thread; the indexer simply invokes it inline. ``total`` is the total
    file count for the current walk (pre-computed); ``current`` is the
    1-based index of the most-recently processed file. Callback exceptions
    are caught and ignored so they cannot break indexing.
    """
    from . import store as _store  # local import to avoid cycles

    files_indexed = 0
    files_skipped = 0
    chunks_written = 0
    seen: set[str] = set()
    # Normalise root list once for prune scoping.
    roots_list = [r.rstrip("/") for r in roots]

    # Pre-enumerate so progress_callback knows the total up front.
    tabular_list = [str(g) for g in (tabular_globs or [])]
    all_paths = list(iter_markdown(repo_root, roots_list))
    all_paths.extend(iter_tabular(repo_root, tabular_list))
    total = len(all_paths)
    for idx, path in enumerate(all_paths, start=1):
        rel = path.relative_to(repo_root).as_posix()
        seen.add(rel)
        result = index_one_file(repo_root, path, conn, rebuild=rebuild,
                                max_chunk_chars=max_chunk_chars,
                                strategy=strategy,
                                overlap_paragraphs=overlap_paragraphs)
        if result["action"] == "indexed":
            files_indexed += 1
            chunks_written += result["chunks"]
        elif result["action"] == "skipped":
            files_skipped += 1
        if progress_callback is not None:
            try:
                progress_callback(rel, idx, total)
            except Exception:  # noqa: BLE001 -- never propagate UI errors
                pass

    pruned_files = 0
    pruned_chunks = 0
    if prune and not rebuild:
        import fnmatch as _fnmatch

        stored = _store.list_all_paths(conn)
        for stored_path in stored:
            if stored_path in seen:
                continue
            # Only prune files that belong to one of the requested roots or
            # match a declared tabular glob.
            in_scope = any(
                stored_path == r or stored_path.startswith(r + "/")
                for r in roots_list
            ) or any(
                _fnmatch.fnmatch(stored_path, g.replace("\\", "/"))
                for g in tabular_list
            )
            if not in_scope:
                continue
            pruned_chunks += _store.delete_file(conn, stored_path)
            pruned_files += 1

    conn.commit()
    return {
        "files_indexed": files_indexed,
        "files_skipped": files_skipped,
        "chunks_written": chunks_written,
        "pruned_files": pruned_files,
        "pruned_chunks": pruned_chunks,
    }


# --- GraphRAG strategy dispatch -------------------------------------------
#
# The ``graphrag`` strategy bypasses the SQLite index entirely. LightRAG
# (lightrag-hku) owns its own working directory layout (KV / Vector /
# Graph stores) and provides its own change-detection. The CLI calls
# ``build_graphrag_index`` instead of ``build_index`` when
# ``--strategy graphrag`` is selected.

# Files LightRAG writes into its working directory. Presence of any of them
# is what distinguishes a built index from a bare (or unrelated) directory.
_LIGHTRAG_MARKERS = (
    "kv_store_doc_status.json",
    "kv_store_full_docs.json",
    "kv_store_text_chunks.json",
    "vdb_entities.json",
    "vdb_relationships.json",
    "vdb_chunks.json",
    "graph_chunk_entity_relation.graphml",
)


def has_lightrag_index(working_dir: Path) -> bool:
    """Return True when ``working_dir`` holds a LightRAG index.

    An existing but empty directory is not an index: ``build_graphrag_index``
    creates the directory before LightRAG runs, so a build that fails (e.g.
    the optional extra is missing) leaves one behind.
    """
    working_dir = Path(working_dir)
    return any((working_dir / marker).exists() for marker in _LIGHTRAG_MARKERS)


def read_doc_status_counts(working_dir: Path) -> dict[str, int]:
    """Return ``{status: count}`` as recorded by LightRAG for ``working_dir``.

    ``ainsert`` swallows per-document extraction failures, so a returning call
    is not evidence that the document was indexed. LightRAG records the real
    outcome in ``kv_store_doc_status.json``.
    """
    path = Path(working_dir) / "kv_store_doc_status.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    counts: dict[str, int] = {}
    for record in raw.values():
        if not isinstance(record, dict):
            continue
        status = str(record.get("status", "unknown"))
        counts[status] = counts.get(status, 0) + 1
    return counts

def build_graphrag_index(
    repo_root: Path,
    roots: Iterable[str],
    working_dir: Path,
    *,
    rebuild: bool = False,
    progress_callback=None,
) -> dict:
    """Walk Markdown files under ``roots`` and insert them into LightRAG.

    Parameters
    ----------
    repo_root, roots:
        Same semantics as :func:`build_index`. ``roots`` are repo-relative
        directory or file prefixes.
    working_dir:
        Directory LightRAG uses for its KV/Vector/Graph storage. The CLI
        computes this as ``.mdq/graphrag-<lang>/``.
    rebuild:
        When ``True``, the entire ``working_dir`` is removed and recreated
        before inserting. As a safety measure the directory must look like
        a LightRAG working directory (i.e. contain one of the known LightRAG
        marker files) or be empty; otherwise a ``ValueError`` is raised so
        a mistyped ``--graphrag-working-dir`` cannot wipe an unrelated path.
        LightRAG's internal duplicate detection (doc_id / content hash)
        skips already-ingested content when ``rebuild=False``.
    progress_callback:
        Optional ``Callable[[str, int, int], None]`` invoked after each
        file with ``(rel, current, total)``. Callback exceptions are
        swallowed to keep behaviour identical to :func:`build_index`.

    Returns a summary dict::

        {
            "strategy": "graphrag",
            "working_dir": str(working_dir),
            "files_total": int,
            "files_ok": int,
            "files_skipped": int,
            "files_error": int,
            "errors": [(rel, message), ...],
        }
    """
    # Lazy import: keeps the indexer module import-time independent of the
    # optional ``graphrag`` extra.
    from . import strategies_graphrag as _gr

    working_dir = Path(working_dir)
    if rebuild and working_dir.exists():
        # Safety: only rmtree directories that look like a LightRAG working
        # directory. If the user accidentally points --graphrag-working-dir
        # at an unrelated path (e.g. their home directory or `/`), refuse to
        # delete its contents. Empty directories are allowed.
        has_marker = has_lightrag_index(working_dir)
        try:
            is_empty = not any(working_dir.iterdir())
        except OSError:
            is_empty = False
        if not (has_marker or is_empty):
            raise ValueError(
                f"--rebuild refused: {working_dir} does not look like a "
                "LightRAG working directory (no LightRAG marker files "
                "found and the directory is not empty). Remove it manually "
                "or choose a different --graphrag-working-dir."
            )
        import shutil
        shutil.rmtree(working_dir)
    working_dir.mkdir(parents=True, exist_ok=True)

    all_paths = list(iter_markdown(repo_root, list(roots)))
    total = len(all_paths)
    if total == 0:
        return {
            "strategy": "graphrag",
            "working_dir": str(working_dir),
            "files_total": 0,
            "files_ok": 0,
            "files_skipped": 0,
            "files_error": 0,
            "documents_processed": 0,
            "documents_failed": 0,
            "errors": [],
        }

    # Batch all files into a single LightRAG session so initialise_storages
    # and finalize_storages run exactly once per build. Forward per-file
    # progress to ``progress_callback`` in real time (converting absolute
    # paths back to repo-relative for display).
    def _per_file_cb(path_str: str, cur: int, tot: int, status: str) -> None:
        if progress_callback is None:
            return
        try:
            rel = Path(path_str).relative_to(repo_root).as_posix()
        except ValueError:
            rel = path_str
        try:
            progress_callback(rel, cur, tot)
        except Exception:  # noqa: BLE001 -- never propagate UI errors
            pass

    results = _gr.insert_paths_sync(working_dir, all_paths, progress_callback=_per_file_cb)

    files_ok = 0
    files_skipped = 0
    files_error = 0
    errors: list[tuple[str, str]] = []
    for path in all_paths:
        rel = path.relative_to(repo_root).as_posix()
        status = results.get(str(path), "error: missing from results")
        if status == "ok":
            files_ok += 1
        elif status.startswith("skipped"):
            files_skipped += 1
        else:
            files_error += 1
            errors.append((rel, status))

    status_counts = read_doc_status_counts(working_dir)
    return {
        "strategy": "graphrag",
        "working_dir": str(working_dir),
        "files_total": total,
        "files_ok": files_ok,
        "files_skipped": files_skipped,
        "files_error": files_error,
        "documents_processed": int(status_counts.get("processed", 0)),
        "documents_failed": int(status_counts.get("failed", 0)),
        "errors": errors,
    }
