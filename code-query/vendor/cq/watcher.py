"""Filesystem watcher that keeps the cq index current (FR-CQ-08).

Mirrors the structure of :class:`mdq.watcher.MdqWatcher`: debounced, coalescing,
start/stop, and a no-op when the optional `watchdog` dependency is absent.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

from cq import discovery, freshness, languages
from cq.config import Profile

DEFAULT_DEBOUNCE_MS = 400


class CqWatcher:
    """Re-index source files shortly after they change on disk."""

    def __init__(
        self,
        repo_root: Path,
        profile: Profile,
        *,
        db_path: Path,
        debounce_ms: int = DEFAULT_DEBOUNCE_MS,
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.profile = profile
        self.db_path = Path(db_path)
        self.debounce_ms = max(0, int(debounce_ms))
        self._observer = None
        self._pending: set[str] = set()
        self._lock = threading.Lock()
        self._timer: threading.Timer | None = None

    @property
    def is_running(self) -> bool:
        return self._observer is not None

    def is_relevant(self, path: Path) -> bool:
        """True when the path is a source file inside the profile's roots."""
        try:
            rel = Path(path).resolve().relative_to(self.repo_root).as_posix()
        except ValueError:
            return False
        if not rel.startswith(tuple(self.profile.roots)):
            return False
        if Path(rel).suffix.lower() not in languages.indexable_suffixes():
            return False
        return not discovery.is_excluded(rel, self.profile.exclude)

    def start(self) -> bool:
        """Return True when the watcher is running; False when unavailable."""
        if self.is_running:
            return True
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer
        except ImportError:
            return False

        watcher = self

        class _Handler(FileSystemEventHandler):
            def on_any_event(self, event) -> None:
                if getattr(event, "is_directory", False):
                    return
                for attribute in ("src_path", "dest_path"):
                    raw = getattr(event, attribute, None)
                    if raw:
                        watcher._enqueue(Path(raw))

        observer = Observer()
        for root in self.profile.roots:
            target = self.repo_root / root
            if target.is_dir():
                observer.schedule(_Handler(), str(target), recursive=True)
        observer.daemon = True
        observer.start()
        self._observer = observer
        return True

    def stop(self) -> None:
        with self._lock:
            timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()
        observer, self._observer = self._observer, None
        if observer is not None:
            observer.stop()
            observer.join(timeout=2.0)
        self._flush()

    def _enqueue(self, path: Path) -> None:
        if not self.is_relevant(path):
            return
        rel = path.resolve().relative_to(self.repo_root).as_posix()
        with self._lock:
            self._pending.add(rel)
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.debounce_ms / 1000.0, self._flush)
            self._timer.daemon = True
            self._timer.start()

    def _flush(self) -> None:
        with self._lock:
            pending, self._pending = self._pending, set()
            self._timer = None
        if not pending:
            return
        try:
            freshness.refresh(self.repo_root, self.profile, self.db_path, tuple(sorted(pending)))
        except Exception:
            # 監視は補助機構。索引更新の失敗で常駐プロセスを落とさない。
            pass

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """Block until the debounce queue drains (used by callers and tests)."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if not self._pending and self._timer is None:
                    return True
            time.sleep(0.02)
        return False
