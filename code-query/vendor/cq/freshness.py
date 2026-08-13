"""Index freshness without a running watcher (FR-CQ-08).

The check compares only `stat()` metadata — never file contents — so it stays
cheap enough to run on every query. Content hashing happens in the indexer.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from cq import discovery, store
from cq.config import Profile

DEFAULT_AUTO_REINDEX_LIMIT = 50


@dataclass(frozen=True)
class FreshnessReport:
    changed: tuple[str, ...]

    @property
    def is_fresh(self) -> bool:
        return not self.changed


def check(
    repo_root: Path,
    profile: Profile,
    db_path: Path,
    *,
    include_new: bool = False,
    lister=discovery.git_lister,
) -> FreshnessReport:
    """Return the indexed paths whose size or mtime no longer match the index.

    ``include_new`` additionally enumerates the working tree to spot files that
    were never indexed. That costs a `git ls-files` subprocess (~250 ms on this
    repository), so it is off by default: the per-query guard must stay cheap
    enough not to dominate the response time (NFR-CQ-01).
    """
    with closing(store.open_store(db_path, create=False)) as conn:
        known = {
            row["path"]: (row["size_bytes"], row["mtime"])
            for row in conn.execute("SELECT path, size_bytes, mtime FROM files")
        }

    changed: set[str] = set()
    for path, (size, mtime) in known.items():
        try:
            stat = (repo_root / path).stat()
        except OSError:
            changed.add(path)  # 消えた、または読めなくなった
            continue
        if stat.st_size != size or stat.st_mtime != mtime:
            changed.add(path)

    if include_new:
        for found in discovery.iter_files(repo_root, profile, lister=lister):
            if found.path not in known:
                changed.add(found.path)

    return FreshnessReport(tuple(sorted(changed)))


def refresh(
    repo_root: Path,
    profile: Profile,
    db_path: Path,
    paths: tuple[str, ...],
):
    """Re-index only ``paths``; files that vanished are removed individually.

    Pruning is disabled so that narrowing the file list does not delete the rest
    of the index. Existence is decided by the filesystem rather than another
    `git ls-files` call, which would dominate the cost of a single-file update.
    """
    from cq import indexer

    existing = tuple(sorted(p for p in paths if (repo_root / p).is_file()))
    vanished = tuple(sorted(set(paths) - set(existing)))

    report = indexer.build_index(
        repo_root, profile, db_path=db_path, prune=False,
        lister=lambda _root: existing,
    )
    if vanished:
        with closing(store.open_store(db_path, create=False)) as conn:
            conn.executemany("DELETE FROM files WHERE path = ?", [(p,) for p in vanished])
            conn.commit()
        report.pruned = len(vanished)
    return report
