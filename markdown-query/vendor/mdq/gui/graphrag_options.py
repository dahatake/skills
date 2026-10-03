"""GraphRAG options form widget.

Self-contained ``QWidget`` exposing the ``graphrag`` strategy parameters that
the GUI needs to adjust. Mirrors :mod:`pageindex_options` in structure.

Settings keys (``[mdq]`` section):
  - ``graphrag_llm_timeout`` (int seconds, 0 = code default)

Public API:
  - :class:`GraphRagOptionsWidget`
    - :meth:`load_from(mdq_settings_dict)` populate fields
    - :meth:`to_settings_dict() -> dict` return the [mdq] subset to save
    - :meth:`to_runtime_kwargs() -> dict` kwargs for ``GraphRAGConfig``
    - signal :data:`changed` fires after any user-visible change
"""
from __future__ import annotations

from typing import Any, Dict

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QFormLayout,
    QFrame,
    QLabel,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from mdq.strategies_graphrag import GraphRAGConfig

_BANNER_HTML = (
    "graphrag は LightRAG と Ollama を使い、文書からエンティティと関係を"
    "抽出してナレッジグラフを構築します。任意依存 [graphrag] と、"
    "ローカルで起動した Ollama（既定モデル: "
    f"{GraphRAGConfig().llm_model} / {GraphRAGConfig().embed_model}）が必要です。"
)


class GraphRagOptionsWidget(QWidget):
    """Settings form for the graphrag chunking strategy."""

    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

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

        default_timeout = int(GraphRAGConfig().llm_timeout)
        self._llm_timeout = QSpinBox()
        self._llm_timeout.setRange(0, 36000)
        self._llm_timeout.setSpecialValueText(f"既定 ({default_timeout} 秒)")
        self._llm_timeout.setSingleStep(60)
        self._llm_timeout.setSuffix(" 秒")
        self._llm_timeout.setToolTip(
            "LLM 1 回の呼び出しを打ち切るまでの秒数。CLI の "
            "--graphrag-timeout と同じ値で、LightRAG 側の実行タイムアウトにも"
            "適用されます。大きい文書ほど 1 回の抽出に時間がかかるため、"
            "ビルド結果に documents_failed が出る場合はこの値を増やして"
            "再実行します。実測（qwen2.5:7b / CPU）では 17KB・5 chunk の"
            "文書が 1200 秒では失敗し 1800 秒で成功しました。"
            f"0 でコード既定 ({default_timeout} 秒)。"
        )
        self._llm_timeout.valueChanged.connect(self._on_any_change)
        form.addRow("LLM タイムアウト", self._llm_timeout)

        layout.addWidget(form_frame)
        layout.addStretch(1)

    # --- public API ----------------------------------------------------

    def load_from(self, mdq_settings: Dict[str, Any]) -> None:
        try:
            self._llm_timeout.setValue(
                int(mdq_settings.get("graphrag_llm_timeout", 0) or 0)
            )
        except (TypeError, ValueError):
            self._llm_timeout.setValue(0)

    def to_settings_dict(self) -> Dict[str, Any]:
        return {"graphrag_llm_timeout": int(self._llm_timeout.value())}

    def to_runtime_kwargs(self) -> Dict[str, Any]:
        """Zero is omitted so ``GraphRAGConfig`` keeps its own default."""
        seconds = int(self._llm_timeout.value())
        return {"llm_timeout": float(seconds)} if seconds > 0 else {}

    # --- internal ------------------------------------------------------

    def _on_any_change(self, *_a) -> None:
        self.changed.emit()
