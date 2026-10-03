"""Background QThreads for index refresh and usage-report regeneration."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QWidget

from . import index_service
from .index_service import SettingsBackend


class IndexRefreshThread(QThread):
    succeeded = Signal(dict)
    failed = Signal(str)
    # Phase 2 (Q11=A): file-level progress (relative_path, current, total).
    progressed = Signal(str, int, int)

    def __init__(
        self,
        *,
        repo_root: Path,
        lang: str = "ja-jp",
        strategy: str = "heading",
        overlap_paragraphs: int | None = None,
        force: bool = False,
        semantic_options: dict | None = None,
        pageindex_options: dict | None = None,
        graphrag_options: dict | None = None,
        settings_backend: Optional[SettingsBackend] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._repo_root = repo_root
        self._lang = lang
        self._strategy = strategy
        self._overlap_paragraphs = overlap_paragraphs
        self._force = bool(force)
        self._semantic_options = semantic_options
        self._pageindex_options = pageindex_options
        self._graphrag_options = graphrag_options
        self._settings_backend = settings_backend

    def run(self) -> None:  # type: ignore[override]
        try:
            summary = index_service.rebuild_index(
                self._repo_root,
                lang=self._lang,
                strategy=self._strategy,
                overlap_paragraphs=self._overlap_paragraphs,
                force=self._force,
                semantic_options=self._semantic_options,
                pageindex_options=self._pageindex_options,
                graphrag_options=self._graphrag_options,
                settings_backend=self._settings_backend,
                progress_callback=self._emit_progress,
            )
            self.succeeded.emit(summary)
        except Exception as e:  # pragma: no cover - defensive
            self.failed.emit(str(e))

    def _emit_progress(self, rel: str, cur: int, total: int) -> None:
        # Signal is thread-safe: Qt queues across thread boundaries.
        try:
            self.progressed.emit(str(rel), int(cur), int(total))
        except Exception:  # noqa: BLE001 -- never break indexing on UI errors
            pass


class SearchPreviewThread(QThread):
    """Run a top-k preview search off the UI thread (Phase 2 / T06)."""

    succeeded = Signal(list)  # list[dict] from index_service.search_preview
    failed = Signal(str)

    def __init__(
        self,
        *,
        repo_root: Path,
        query: str,
        lang: str = "ja-jp",
        strategy: str = "heading",
        top_k: int = 3,
        fusion_alpha: float | None = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._repo_root = repo_root
        self._query = query
        self._lang = lang
        self._strategy = strategy
        self._top_k = int(top_k)
        self._fusion_alpha = fusion_alpha

    def run(self) -> None:  # type: ignore[override]
        try:
            rows = index_service.search_preview(
                self._repo_root,
                self._query,
                lang=self._lang,
                strategy=self._strategy,
                top_k=self._top_k,
                fusion_alpha=self._fusion_alpha,
            )
            self.succeeded.emit(rows)
        except Exception as e:  # pragma: no cover - defensive
            self.failed.emit(str(e))


class UsageReportThread(QThread):
    """Regenerate the usage report in a background thread."""

    succeeded = Signal(dict)  # {"md": "...", "json": "..."}
    failed = Signal(str)

    def __init__(
        self,
        *,
        repo_root: Path,
        lang: str = "ja-jp",
        strategy: str = "heading",
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._repo_root = repo_root
        self._lang = lang
        self._strategy = strategy

    def run(self) -> None:  # type: ignore[override]
        try:
            from mdq import usage_report

            paths = usage_report.generate_report(
                self._repo_root, lang=self._lang, strategy=self._strategy
            )
            self.succeeded.emit({k: str(v) for k, v in paths.items()})
        except Exception as e:  # pragma: no cover - defensive
            self.failed.emit(str(e))
