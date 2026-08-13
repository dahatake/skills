"""Collapsible "試し検索" preview panel (Phase 2 / T07).

Q4=B: collapsible (default closed) so it does not eat vertical space.
Q8=A: hits are rendered as a QTableWidget with path / heading_path / score
       / snippet columns.
Q9=A: fusion_alpha is only forwarded when the active strategy is
       semantic_paragraph AND late_chunking is enabled. The caller passes
       ``fusion_alpha=None`` to opt out; this widget is unaware of the
       strategy detail and trusts its caller.

The widget runs queries on a :class:`SearchPreviewThread` so the UI stays
responsive even when the BM25 corpus is large.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from .threads import SearchPreviewThread


class TestSearchPanel(QWidget):
    """Collapsible top-k preview panel for the index management tab."""

    # Emitted after a successful search; consumer can log the hit count.
    searched = Signal(int)

    def __init__(
        self,
        *,
        repo_root: Path,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._repo_root = repo_root
        self._lang: str = "ja-jp"
        self._strategy: str = "heading"
        self._fusion_alpha: Optional[float] = None
        self._thread: Optional[SearchPreviewThread] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # --- Collapsible header (Q4=B) ----------------------------------
        self._toggle = QToolButton()
        self._toggle.setText("試し検索（折りたたみ）")
        self._toggle.setCheckable(True)
        self._toggle.setChecked(False)
        self._toggle.setArrowType(Qt.ArrowType.RightArrow)
        self._toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self._toggle.toggled.connect(self._on_toggle)
        layout.addWidget(self._toggle, alignment=Qt.AlignmentFlag.AlignLeft)

        # --- Body (hidden by default) -----------------------------------
        self._body = QWidget()
        body_layout = QVBoxLayout(self._body)
        body_layout.setContentsMargins(0, 4, 0, 0)

        row = QHBoxLayout()
        self._query_input = QLineEdit()
        self._query_input.setPlaceholderText("検索クエリを入力して Enter")
        self._query_input.returnPressed.connect(self._on_run_clicked)
        self._btn_run = QPushButton("検索")
        self._btn_run.clicked.connect(self._on_run_clicked)
        row.addWidget(self._query_input)
        row.addWidget(self._btn_run)
        body_layout.addLayout(row)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        body_layout.addWidget(self._status)

        self._table = QTableWidget()
        self._table.setColumnCount(4)
        self._table.setHorizontalHeaderLabels(
            ["path", "heading_path", "score", "snippet"]
        )
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectRows)
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(QHeaderView.Interactive)
        hdr.setSectionResizeMode(3, QHeaderView.Stretch)  # snippet stretches
        self._table.setMinimumHeight(120)
        body_layout.addWidget(self._table)

        layout.addWidget(self._body)
        self._body.setVisible(False)

    # --- public API ---------------------------------------------------

    def set_context(
        self,
        *,
        lang: str,
        strategy: str,
        fusion_alpha: Optional[float] = None,
    ) -> None:
        """Update which (lang, strategy) DB to search, plus optional fusion."""
        self._lang = lang
        self._strategy = strategy
        self._fusion_alpha = fusion_alpha

    def clear_results(self) -> None:
        self._table.setRowCount(0)
        self._status.setText("")

    # --- internal -----------------------------------------------------

    def _on_toggle(self, expanded: bool) -> None:
        self._body.setVisible(expanded)
        self._toggle.setArrowType(
            Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        )

    def _on_run_clicked(self) -> None:
        q = self._query_input.text().strip()
        if not q:
            self._status.setText("クエリを入力してください。")
            return
        if self._thread is not None and self._thread.isRunning():
            self._status.setText("検索中…前回の処理が完了するまでお待ちください。")
            return
        self._status.setText("検索中…")
        self._table.setRowCount(0)
        self._btn_run.setEnabled(False)
        self._thread = SearchPreviewThread(
            repo_root=self._repo_root,
            query=q,
            lang=self._lang,
            strategy=self._strategy,
            top_k=3,
            fusion_alpha=self._fusion_alpha,
            parent=self,
        )
        self._thread.succeeded.connect(self._on_succeeded)
        self._thread.failed.connect(self._on_failed)
        self._thread.finished.connect(lambda: self._btn_run.setEnabled(True))
        self._thread.start()

    def _on_succeeded(self, rows: list) -> None:
        self._populate_table(rows)
        self._status.setText(f"{len(rows)} 件のヒット。")
        self.searched.emit(len(rows))

    def _on_failed(self, msg: str) -> None:
        self._status.setText(f"検索に失敗: {msg}")

    def _populate_table(self, rows: list) -> None:
        self._table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            self._table.setItem(r, 0, QTableWidgetItem(str(row.get("path", ""))))
            self._table.setItem(r, 1, QTableWidgetItem(str(row.get("heading_path", ""))))
            self._table.setItem(r, 2, QTableWidgetItem(f"{float(row.get('score', 0.0)):.3f}"))
            self._table.setItem(r, 3, QTableWidgetItem(str(row.get("snippet", ""))))
        self._table.resizeColumnsToContents()


__all__ = ["TestSearchPanel"]
