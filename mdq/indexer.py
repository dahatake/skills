"""Markdown indexer: extracts frontmatter and heading-based chunks.

Pure-stdlib implementation. Handles fenced code blocks so '#' inside code
is not misread as a heading.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

try:
    import yaml  # PyYAML is already a project dependency
except Exception:  # pragma: no cover
    yaml = None

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
FENCE_RE = re.compile(r"^(`{3,}|~{3,})")

# Directory names that are pruned from the recursive walk by default.
# These are virtual environments, dependency caches, build outputs and the
# index's own scratch directory — files inside them are almost never the
# documentation an author wants to search.
DEFAULT_EXCLUDE_DIRS: frozenset[str] = frozenset({
    ".git",
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".mdq",
    "dist",
    "build",
    ".next",
    ".cache",
})

# Markdown file extensions that are indexed.
MARKDOWN_EXTENSIONS: tuple[str, ...] = (".md", ".markdown")


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


def _subdivide(text: str, start_line: int, max_chars: int
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
                  tags: list[str], max_chunk_chars: int = 0) -> list[Chunk]:
    """Split body into heading-bounded chunks.

    A chunk spans from a heading line until the next heading of equal-or-lower
    depth. The pre-heading preface (lines before first heading) becomes a
    synthetic chunk with heading_path = '(preface)'.

    When ``max_chunk_chars`` > 0, each heading chunk whose body exceeds the
    budget is further subdivided via :func:`_subdivide` (paragraph-first,
    line-fallback, fence-preserving). Sub-chunks share the same heading_path
    and level; their ordering is recorded in part_index / part_total.
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
                parts = _subdivide(text, base_start, max_chunk_chars)
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
              max_chunk_chars: int = 0) -> tuple[dict, list[Chunk]]:
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
                           max_chunk_chars=max_chunk_chars)
    return fm, chunks


def iter_markdown(root: Path, roots: Iterable[str]) -> Iterable[Path]:
    """Yield Markdown files under each *roots* entry beneath *root*.

    Both ``*.md`` and ``*.markdown`` are emitted. Well-known dependency,
    build and VCS directories (see :data:`DEFAULT_EXCLUDE_DIRS`) are pruned
    from the recursive walk so the index does not get polluted by, e.g.,
    ``node_modules`` or ``.venv``. The pruning is name-based: a directory
    whose basename matches an entry in ``DEFAULT_EXCLUDE_DIRS`` is skipped
    entirely (its subtree is not descended into).

    Note: if a user explicitly passes one of those names via ``--root``,
    that root is still scanned — the pruning only applies to *descendants*
    of the walked base directory.
    """
    for r in roots:
        base = (root / r).resolve()
        if not base.exists() or not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            # Prune excluded directories in-place so os.walk does not
            # descend into them (this is the documented behaviour of
            # os.walk when topdown=True, which is the default).
            dirnames[:] = [d for d in dirnames if d not in DEFAULT_EXCLUDE_DIRS]
            for fn in filenames:
                if fn.endswith(MARKDOWN_EXTENSIONS):
                    yield Path(dirpath) / fn


def _sha1_bytes(b: bytes) -> str:
    return hashlib.sha1(b).hexdigest()


def index_one_file(repo_root: Path, file_path: Path, conn,
                   rebuild: bool = False,
                   max_chunk_chars: int = 0) -> dict:
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

    fm, chunks = scan_file(repo_root, file_path,
                           max_chunk_chars=max_chunk_chars)
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
    fm_json = json.dumps(fm, ensure_ascii=False) if fm else None
    _store.upsert_file(conn, rel, sha1, mtime, len(raw), fm_json)
    _store.delete_chunks_for(conn, rel)
    rows = [(
        c.chunk_id, c.path, c.heading_path, c.level,
        c.start_line, c.end_line, c.token_est, c.text,
        json.dumps(c.tags, ensure_ascii=False) if c.tags else None,
        c.part_index, c.part_total,
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
                max_chunk_chars: int = 0) -> dict:
    """Walk Markdown files under roots and persist chunks.

    Returns a summary dict: {files_indexed, files_skipped, chunks_written,
    pruned_files, pruned_chunks}.
    Incremental: skips files whose (sha1, mtime) match the store.
    Prune (default on): files that previously lived under any of the given
    roots but are no longer present on disk are removed from the store
    (chunks are removed via ON DELETE CASCADE). Files outside the given
    roots are left untouched.
    """
    from . import store as _store  # local import to avoid cycles

    files_indexed = 0
    files_skipped = 0
    chunks_written = 0
    seen: set[str] = set()
    # Normalise root list once for prune scoping.
    roots_list = [r.rstrip("/") for r in roots]

    for path in iter_markdown(repo_root, roots_list):
        rel = path.relative_to(repo_root).as_posix()
        seen.add(rel)
        result = index_one_file(repo_root, path, conn, rebuild=rebuild,
                                max_chunk_chars=max_chunk_chars)
        if result["action"] == "indexed":
            files_indexed += 1
            chunks_written += result["chunks"]
        elif result["action"] == "skipped":
            files_skipped += 1

    pruned_files = 0
    pruned_chunks = 0
    if prune and not rebuild:
        stored = _store.list_all_paths(conn)
        for stored_path in stored:
            if stored_path in seen:
                continue
            # Only prune files that belong to one of the requested roots.
            in_scope = any(
                stored_path == r or stored_path.startswith(r + "/")
                for r in roots_list
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
