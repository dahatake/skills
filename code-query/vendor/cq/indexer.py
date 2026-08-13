"""Incremental index builder for cq (FR-CQ-04).

Files whose SHA-1, size and mtime are unchanged are skipped, files that vanished
are pruned, and files that cannot be parsed at full fidelity are degraded to the
lite extractor rather than dropped.
"""

from __future__ import annotations

import hashlib
import sqlite3
import sys
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from cq import chunking, discovery, graph, store, traces
from cq.config import Profile
from cq.languages import ExtractionError, RawSymbol, lite, support_for


@dataclass
class IndexReport:
    indexed: int = 0
    skipped: int = 0
    pruned: int = 0
    degraded: int = 0
    errors: int = 0
    symbols: int = 0
    chunks: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "indexed": self.indexed,
            "skipped": self.skipped,
            "pruned": self.pruned,
            "degraded": self.degraded,
            "errors": self.errors,
            "symbols": self.symbols,
            "chunks": self.chunks,
        }


def symbol_id(path: str, symbol: RawSymbol, occurrence: int) -> str:
    """Stable across runs and independent of surrounding line shifts."""
    key = f"{path}|{symbol.qualname}|{symbol.kind}|{occurrence}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()


def build_index(
    repo_root: Path,
    profile: Profile,
    *,
    db_path: Path | None = None,
    rebuild: bool = False,
    prune: bool = True,
    lister=discovery.git_lister,
) -> IndexReport:
    target = Path(db_path) if db_path else repo_root / store.db_path_for(profile.name)
    if rebuild and target.exists():
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(str(target) + suffix)
            if candidate.exists():
                candidate.unlink()

    report = IndexReport()
    files = discovery.iter_files(repo_root, profile, lister=lister)
    with closing(store.open_store(target)) as conn:
        known = {
            row["path"]: (row["sha1"], row["size_bytes"])
            for row in conn.execute("SELECT path, sha1, size_bytes FROM files")
        }
        seen: set[str] = set()
        for found in files:
            seen.add(found.path)
            read = _read(repo_root / found.path)
            if read is None:
                report.errors += 1
                continue
            raw, source = read
            sha1 = hashlib.sha1(raw).hexdigest()
            previous = known.get(found.path)
            if previous and previous[0] == sha1 and previous[1] == found.size_bytes:
                report.skipped += 1
                continue
            symbols, parser = _extract(found.lang, source)
            if parser == "lite":
                report.degraded += 1
            chunks = chunking.chunk_source(source, found.lang)
            _write_file(conn, found, sha1, parser, symbols, chunks, source)
            report.indexed += 1
            report.symbols += len(symbols)
            report.chunks += len(chunks)

        stale = sorted(set(known) - seen) if prune else []
        if stale:
            conn.executemany("DELETE FROM files WHERE path = ?", [(p,) for p in stale])
            report.pruned = len(stale)
        conn.commit()
    return report


def _read(path: Path) -> tuple[bytes, str] | None:
    """Return raw bytes plus decoded text. `utf-8-sig` strips a BOM, which would
    otherwise make `ast.parse` reject perfectly valid source with U+FEFF."""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    return raw, raw.decode("utf-8-sig", errors="replace")


# One line per (language, exception) so a systematically broken extractor is
# visible without printing a message for every file it fails on.
_REPORTED_EXTRACTOR_FAILURES: set[tuple[str, str]] = set()


def _extract(lang: str, source: str) -> tuple[tuple[RawSymbol, ...], str]:
    support = support_for(lang)
    if support is not None:
        try:
            if support.extract_ex is not None:
                return support.extract_ex(source)
            return support.extract(source), support.parser
        except (ExtractionError, ImportError):
            # An absent optional grammar degrades exactly like a parse failure
            # instead of aborting the index (FR-CQ-11). A language module is
            # responsible for turning its own loader errors into ExtractionError.
            pass
        except Exception as exc:
            # An unexpected extractor bug must cost one file, not the whole run.
            key = (lang, type(exc).__name__)
            if key not in _REPORTED_EXTRACTOR_FAILURES:
                _REPORTED_EXTRACTOR_FAILURES.add(key)
                print(
                    f"cq: the {lang} extractor raised {type(exc).__name__}; "
                    "affected files are indexed at lite fidelity",
                    file=sys.stderr,
                )
    return lite.extract(source), "lite"


def _chunk_owner(
    symbols: tuple[RawSymbol, ...], start_line: int, end_line: int
) -> int | None:
    """Index of the smallest symbol opening on ``start_line`` that encloses the chunk.

    Several symbols can share a start line (a one-line class with a one-line
    method), so the chunk's own extent decides which of them describes it.
    """
    opening = [i for i, s in enumerate(symbols) if s.start_line == start_line]
    if not opening:
        return None
    enclosing = [i for i in opening if symbols[i].end_line >= end_line]
    if enclosing:
        return min(enclosing, key=lambda i: (symbols[i].end_line, symbols[i].qualname))
    return max(opening, key=lambda i: (symbols[i].end_line, symbols[i].qualname))


def _write_file(
    conn: sqlite3.Connection,
    found: discovery.DiscoveredFile,
    sha1: str,
    parser: str,
    symbols: tuple[RawSymbol, ...],
    chunks: tuple[chunking.Chunk, ...],
    source: str,
) -> None:
    conn.execute("DELETE FROM files WHERE path = ?", (found.path,))
    conn.execute(
        "INSERT INTO files(path, lang, sha1, mtime, size_bytes, parser)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (found.path, found.lang, sha1, found.mtime, found.size_bytes, parser),
    )
    counts: dict[tuple[str, str], int] = {}
    rows = []
    ids: list[str] = []
    for symbol in symbols:
        key = (symbol.qualname, symbol.kind)
        counts[key] = counts.get(key, 0) + 1
        identifier = symbol_id(found.path, symbol, counts[key])
        ids.append(identifier)
        rows.append((
            identifier,
            found.path,
            symbol.qualname,
            symbol.name,
            symbol.kind,
            symbol.start_line,
            symbol.end_line,
            symbol.signature,
            symbol.parent,
            symbol.doc_head,
            ";".join(symbol.decorators) or None,
            int(symbol.is_test),
        ))
    conn.executemany(
        "INSERT INTO symbols(symbol_id, path, qualname, name, kind, start_line,"
        " end_line, signature, parent, doc_head, decorators, is_test)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    chunk_rows = []
    for index, chunk in enumerate(chunks, start=1):
        owner = _chunk_owner(symbols, chunk.start_line, chunk.end_line)
        chunk_rows.append((
            hashlib.sha1(f"{found.path}|{index}".encode("utf-8")).hexdigest(),
            found.path,
            ids[owner] if owner is not None else None,
            chunk.name,
            chunk.signature,
            chunk.ident_text,
            chunk.start_line,
            chunk.end_line,
            len(chunk.text) // 4,
            chunk.text,
        ))
    conn.executemany(
        "INSERT INTO chunks(chunk_id, path, symbol_id, name, signature, ident_text,"
        " start_line, end_line, token_est, text)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        chunk_rows,
    )
    _write_graph(conn, found, source)


def _write_graph(conn: sqlite3.Connection, found: discovery.DiscoveredFile, source: str) -> None:
    refs, imports = graph.extract(source, found.lang)
    conn.executemany(
        "INSERT INTO refs(path, line, name) VALUES (?, ?, ?)",
        [(found.path, r.line, r.name) for r in refs],
    )
    conn.executemany(
        "INSERT INTO imports(path, line, module) VALUES (?, ?, ?)",
        [(found.path, i.line, i.module) for i in imports],
    )
    conn.executemany(
        "INSERT INTO traces(path, line, trace_id, doc_path, anchor) VALUES (?, ?, ?, ?, ?)",
        [(found.path, t.line, t.trace_id, t.doc_path, t.anchor)
         for t in traces.extract(source)],
    )
