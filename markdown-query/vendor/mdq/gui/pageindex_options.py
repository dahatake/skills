"""PageIndex options form widget.

Self-contained ``QWidget`` exposing the ``pageindex`` chunking strategy
parameters. Mirrors the structure of :mod:`semantic_options` but is
intentionally smaller: pageindex has no optional dependencies and only
three configuration knobs.

Settings keys (``[mdq]`` section in ``hve/.settings.txt``):
  - ``pageindex_summary_chars`` (int, 0 = code default 200)
  - ``pageindex_summary_mode``  (``head`` / ``first_paragraph``)

NOTE: A previously planned ``pageindex_default_tree_depth`` setting was
removed before release because there is no code path that reads it back
from settings into the CLI search defaults; users should pass
``--pageindex-tree-depth N`` explicitly on each ``mdq search`` call.

Public API:
  - :class:`PageIndexOptionsWidget`
    - :meth:`load_from(mdq_settings_dict)` populate fields
    - :meth:`to_settings_dict() -> dict` return the [mdq] subset to save
    - :meth:`to_runtime_kwargs() -> dict` kwargs for
      ``mdq.strategies_pageindex.set_runtime_config``
    - signal :data:`changed` fires after any user-visible change
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


_BANNER_HTML = (
    "pageindex は見出しベースのツリー索引を構築し、各ノードに"
    "サマリ（先頭抜粋）を保存します。LLM 呼び出しは行いません "
    "(Index-only)。"
)


class PageIndexOptionsWidget(QWidget):
    """Settings form for the pageindex chunking strategy."""

    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # --- Info banner (blue, not a warning) -------------------------
        self._banner = QLabel(_BANNER_HTML)
        self._banner.setWordWrap(True)
        self._banner.setStyleSheet(
            "QLabel { color: palette(link); background: palette(alternate-base); "
            "border: 1px solid palette(mid); padding: 6px; }"
        )
        layout.addWidget(self._banner)

        form_frame = QFrame()
        form = QFormLayout(form_frame)
        form.setContentsMargins(0, 0, 0, 0)

        # --- summary_chars ---------------------------------------------
        self._summary_chars = QSpinBox()
        self._summary_chars.setRange(0, 2000)
        self._summary_chars.setSpecialValueText("既定 (200)")
        self._summary_chars.setSingleStep(50)
        self._summary_chars.setToolTip(
            "chunks.summary に保存される抜粋の最大文字数。"
            "0 でコード既定 (PAGEINDEX_SUMMARY_CHARS=200)。"
        )
        form.addRow("ノードサマリ最大文字数", self._summary_chars)

        # --- summary_mode ----------------------------------------------
        self._summary_mode = QComboBox()
        self._summary_mode.addItem("先頭抜粋 (head)", "head")
        self._summary_mode.addItem("最初の段落 (first_paragraph)", "first_paragraph")
        self._summary_mode.setToolTip(
            "head = 本文先頭 N 文字。first_paragraph = 見出し直後の"
            "最初の段落を採用し N 文字でクリップ。"
        )
        form.addRow("サマリ抽出方式", self._summary_mode)


        layout.addWidget(form_frame)

        # --- Preview (collapsed by default) ----------------------------
        self._preview_group = QGroupBox("プレビュー (現在の設定で試行ビルド)")
        self._preview_group.setCheckable(True)
        self._preview_group.setChecked(False)
        preview_layout = QVBoxLayout(self._preview_group)

        button_row = QHBoxLayout()
        self._preview_button = QPushButton("ファイルを選んで試行ビルド…")
        self._preview_button.setToolTip(
            "選択した 1 ファイルを現在の設定で in-memory でビルドし、"
            "ツリー構造と各ノードのサマリを表示します（DB は変更しません）。"
        )
        self._preview_button.clicked.connect(self._on_preview_clicked)
        button_row.addWidget(self._preview_button)
        button_row.addStretch(1)
        preview_layout.addLayout(button_row)

        self._preview_output = QTextEdit()
        self._preview_output.setReadOnly(True)
        self._preview_output.setPlaceholderText(
            "ここにツリー構造とサマリが表示されます。"
        )
        self._preview_output.setMinimumHeight(120)
        preview_layout.addWidget(self._preview_output)

        layout.addWidget(self._preview_group)

        # --- Signal wiring ---------------------------------------------
        self._summary_chars.valueChanged.connect(self._on_any_change)
        self._summary_mode.currentIndexChanged.connect(self._on_any_change)

    # --- public API ----------------------------------------------------

    def load_from(self, mdq_settings: Dict[str, Any]) -> None:
        """Populate fields from the ``[mdq]`` section dict."""
        try:
            self._summary_chars.setValue(
                int(mdq_settings.get("pageindex_summary_chars", 0) or 0)
            )
        except (TypeError, ValueError):
            self._summary_chars.setValue(0)
        mode = str(
            mdq_settings.get("pageindex_summary_mode", "head") or "head"
        )
        idx = self._summary_mode.findData(mode)
        if idx >= 0:
            self._summary_mode.setCurrentIndex(idx)

    def to_settings_dict(self) -> Dict[str, Any]:
        """Return values suitable for merging into the ``[mdq]`` section."""
        return {
            "pageindex_summary_chars": int(self._summary_chars.value()),
            "pageindex_summary_mode": str(
                self._summary_mode.currentData() or "head"
            ),
        }

    def to_runtime_kwargs(self) -> Dict[str, Any]:
        """Return kwargs for ``strategies_pageindex.set_runtime_config``.

        Zero/empty values are omitted so the strategy uses its module-level
        defaults instead of being overridden with 0.
        """
        kwargs: Dict[str, Any] = {}
        n = int(self._summary_chars.value())
        if n > 0:
            kwargs["summary_chars"] = n
        mode = str(self._summary_mode.currentData() or "head")
        if mode:
            kwargs["summary_mode"] = mode
        return kwargs

    # --- internal ------------------------------------------------------

    def _on_any_change(self, *_a) -> None:
        self.changed.emit()

    def _on_preview_clicked(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            "プレビュー対象の Markdown ファイルを選択",
            "",
            "Markdown (*.md);;All files (*)",
        )
        if not path_str:
            return
        path = Path(path_str)
        # Resolve a repo_root to satisfy relative_to() in the strategy. Use
        # the file's own parent; the relative path inside the chunk dict
        # ends up as the file name only, which is fine for the preview.
        repo_root = path.parent

        # Apply current settings as a runtime override, then restore.
        try:
            from mdq import strategies_pageindex as pi
        except Exception as exc:  # pragma: no cover - GUI-only
            self._preview_output.setPlainText(
                f"mdq.strategies_pageindex の import に失敗しました: {exc}"
            )
            return

        pi.clear_runtime_config()
        pi.set_runtime_config(**self.to_runtime_kwargs())
        try:
            _fm, chunks = pi.scan_file_pageindex(repo_root, path)
        except Exception as exc:  # pragma: no cover - GUI-only
            self._preview_output.setPlainText(
                f"ビルド中にエラーが発生しました: {exc}"
            )
            return
        finally:
            pi.clear_runtime_config()

        if not chunks:
            self._preview_output.setPlainText(
                "(チャンクが生成されませんでした)"
            )
            return

        lines: list[str] = []
        for c in chunks:
            hp = c.heading_path or "(no heading)"
            depth = hp.count(" > ")
            indent = "  " * depth
            summary = (c.summary or "").replace("\n", " ")
            if len(summary) > 80:
                summary = summary[:80] + "…"
            lines.append(f"{indent}- {hp}")
            if summary:
                lines.append(f"{indent}    [summary] {summary}")
        self._preview_output.setPlainText("\n".join(lines))


__all__ = ["PageIndexOptionsWidget"]
