"""Semantic-paragraph options form widget (Phase 2 / T05).

A self-contained ``QWidget`` that exposes the semantic_paragraph parameters
required to drive ``mdq.strategies_semantic`` from the GUI.

Design (per work/mdq-gui-index-management/plan.md):
- Q3=A: single profile. The widget reads / writes the ``[mdq] semantic_*``
  keys via :mod:`mdq.gui.settings_store`.
- Q5=A: when the optional ``[semantic]`` extra is missing, all fields are
  disabled and the banner_message from
  :mod:`mdq.gui.extras_status` is rendered above
  the form.
- Q11=B: contextualize is a CheckBox defaulting to True.
- Q9=B+A: fusion_alpha slider is shown only when ``late_chunking`` is ON
  (visibility-only; persistence still happens via the SpinBox).

Public API:
- :class:`SemanticOptionsWidget`
  - :meth:`load_from(settings_store_dict_for_mdq)` populate fields
  - :meth:`to_settings_dict() -> dict` return the [mdq] subset to save
  - signal :data:`changed` fires after any user-visible change
"""
from __future__ import annotations

from typing import Dict, Any

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QLabel,
    QLineEdit,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from . import extras_status


class SemanticOptionsWidget(QWidget):
    """Settings form for the semantic_paragraph chunking strategy."""

    changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)

        self._extras = extras_status.probe()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # --- Warning banner (Q5=A) --------------------------------------
        self._banner = QLabel("")
        self._banner.setWordWrap(True)
        self._banner.setStyleSheet(
            "QLabel { background: palette(alternate-base); "
            "border: 1px solid palette(mid); padding: 6px; }"
        )
        self._banner.setVisible(False)
        layout.addWidget(self._banner)

        form_frame = QFrame()
        form = QFormLayout(form_frame)
        form.setContentsMargins(0, 0, 0, 0)

        # --- max_chunk_chars --------------------------------------------
        self._max_chars = QSpinBox()
        self._max_chars.setRange(0, 100000)
        self._max_chars.setSpecialValueText("既定 (1000)")
        self._max_chars.setSingleStep(100)
        self._max_chars.setToolTip(
            "1 チャンクの最大文字数。0 でコード側既定 (SEMANTIC_MAX_CHARS=1000)。"
        )
        form.addRow("最大チャンク文字数", self._max_chars)

        # --- min_chars ---------------------------------------------------
        self._min_chars = QSpinBox()
        self._min_chars.setRange(0, 100000)
        self._min_chars.setSpecialValueText("既定 (200)")
        self._min_chars.setSingleStep(50)
        self._min_chars.setToolTip(
            "最小チャンク文字数。未満なら直前 chunk へ merge。0 でコード既定 (200)。"
        )
        form.addRow("最小チャンク文字数", self._min_chars)

        # --- breakpoint percentile lo / hi -------------------------------
        self._pct_lo = QDoubleSpinBox()
        self._pct_lo.setRange(0.0, 100.0)
        self._pct_lo.setDecimals(1)
        self._pct_lo.setSingleStep(1.0)
        self._pct_lo.setSpecialValueText("既定 (50.0)")
        self._pct_lo.setToolTip("Kamradt-modified バイナリ探索の下限パーセンタイル。")
        form.addRow("Breakpoint percentile (下限)", self._pct_lo)

        self._pct_hi = QDoubleSpinBox()
        self._pct_hi.setRange(0.0, 100.0)
        self._pct_hi.setDecimals(1)
        self._pct_hi.setSingleStep(1.0)
        self._pct_hi.setSpecialValueText("既定 (99.0)")
        self._pct_hi.setToolTip("Kamradt-modified バイナリ探索の上限パーセンタイル。")
        form.addRow("Breakpoint percentile (上限)", self._pct_hi)

        # --- embed provider / model -------------------------------------
        self._embed_provider = QComboBox()
        # Only fastembed and null are first-class today.
        self._embed_provider.addItem("(既定 fastembed)", "")
        self._embed_provider.addItem("fastembed", "fastembed")
        self._embed_provider.addItem("null (deterministic test)", "null")
        self._embed_provider.setToolTip(
            "埋め込み provider。null は decisive ハッシュベース、テスト用。"
        )
        form.addRow("Embed provider", self._embed_provider)

        self._embed_model = QLineEdit()
        self._embed_model.setPlaceholderText("(既定 intfloat/multilingual-e5-large)")
        self._embed_model.setToolTip(
            "埋め込み model 名。空でコード既定 (intfloat/multilingual-e5-large、MIT、初回 ~2.2GB DL)。"
        )
        form.addRow("Embed model", self._embed_model)

        # --- contextualize ----------------------------------------------
        self._contextualize = QCheckBox(
            "Context テンプレートを各 chunk に prepend する (Q11=B 既定 ON)"
        )
        self._contextualize.setChecked(True)
        self._contextualize.setToolTip(
            "[Context] {path} > {heading_path} を chunk 先頭に付与する。"
            "原文は text_raw 列に保存される。"
        )
        form.addRow("Contextualize", self._contextualize)

        # --- late_chunking ----------------------------------------------
        self._late_chunking = QCheckBox(
            "Late-chunking で chunk_embedding をストアに保存"
        )
        self._late_chunking.setToolTip(
            "各 chunk の float32 ベクトルを chunk_embedding 列へ保存し、"
            "検索時に線形加重 fusion で利用可能にする。"
        )
        form.addRow("Late chunking", self._late_chunking)

        # --- fusion_alpha (visible only when late_chunking is on) --------
        self._fusion_alpha = QDoubleSpinBox()
        self._fusion_alpha.setRange(0.0, 1.0)
        self._fusion_alpha.setDecimals(2)
        self._fusion_alpha.setSingleStep(0.05)
        self._fusion_alpha.setValue(0.5)
        self._fusion_alpha.setToolTip(
            "final = alpha * bm25_norm + (1 - alpha) * cosine_sim。"
            "1.0 = BM25 単独、0.0 = cosine 単独。"
        )
        self._fusion_row_label = QLabel("Fusion alpha (検索時)")
        form.addRow(self._fusion_row_label, self._fusion_alpha)

        layout.addWidget(form_frame)

        # --- Signal wiring ----------------------------------------------
        for w in (
            self._max_chars, self._min_chars, self._pct_lo, self._pct_hi,
            self._fusion_alpha,
        ):
            w.valueChanged.connect(self._on_any_change)
        self._embed_provider.currentIndexChanged.connect(self._on_any_change)
        self._embed_model.textChanged.connect(self._on_any_change)
        self._contextualize.stateChanged.connect(self._on_any_change)
        self._late_chunking.stateChanged.connect(self._on_late_chunking_changed)

        self._apply_extras_state()
        self._refresh_fusion_visibility()

    # --- public API ----------------------------------------------------

    def load_from(self, mdq_settings: Dict[str, Any]) -> None:
        """Populate fields from a settings dict (the ``[mdq]`` section)."""
        try:
            self._max_chars.setValue(int(mdq_settings.get("semantic_max_chunk_chars", 0) or 0))
        except (TypeError, ValueError):
            self._max_chars.setValue(0)
        try:
            self._min_chars.setValue(int(mdq_settings.get("semantic_min_chars", 0) or 0))
        except (TypeError, ValueError):
            self._min_chars.setValue(0)
        try:
            self._pct_lo.setValue(float(mdq_settings.get("semantic_breakpoint_percentile_lo", 0.0) or 0.0))
        except (TypeError, ValueError):
            self._pct_lo.setValue(0.0)
        try:
            self._pct_hi.setValue(float(mdq_settings.get("semantic_breakpoint_percentile_hi", 0.0) or 0.0))
        except (TypeError, ValueError):
            self._pct_hi.setValue(0.0)
        provider = str(mdq_settings.get("semantic_embed_provider", "") or "")
        idx = self._embed_provider.findData(provider)
        if idx >= 0:
            self._embed_provider.setCurrentIndex(idx)
        self._embed_model.setText(str(mdq_settings.get("semantic_embed_model", "") or ""))
        self._contextualize.setChecked(bool(mdq_settings.get("semantic_contextualize", True)))
        self._late_chunking.setChecked(bool(mdq_settings.get("semantic_late_chunking", False)))
        try:
            self._fusion_alpha.setValue(float(mdq_settings.get("semantic_fusion_alpha", 0.5) or 0.5))
        except (TypeError, ValueError):
            self._fusion_alpha.setValue(0.5)
        self._refresh_fusion_visibility()

    def to_settings_dict(self) -> Dict[str, Any]:
        """Return values suitable for merging into the ``[mdq]`` section."""
        return {
            "semantic_max_chunk_chars": int(self._max_chars.value()),
            "semantic_min_chars": int(self._min_chars.value()),
            "semantic_breakpoint_percentile_lo": float(self._pct_lo.value()),
            "semantic_breakpoint_percentile_hi": float(self._pct_hi.value()),
            "semantic_embed_provider": str(self._embed_provider.currentData() or ""),
            "semantic_embed_model": str(self._embed_model.text()).strip(),
            "semantic_contextualize": bool(self._contextualize.isChecked()),
            "semantic_late_chunking": bool(self._late_chunking.isChecked()),
            "semantic_fusion_alpha": float(self._fusion_alpha.value()),
        }

    @property
    def semantic_ok(self) -> bool:
        """Whether the [semantic] extra is available."""
        return self._extras.semantic_ok

    # --- internal ------------------------------------------------------

    def _apply_extras_state(self) -> None:
        msg = self._extras.banner_message()
        if msg:
            self._banner.setText(msg)
            self._banner.setVisible(True)
            for w in (
                self._max_chars, self._min_chars, self._pct_lo, self._pct_hi,
                self._embed_provider, self._embed_model,
                self._contextualize, self._late_chunking, self._fusion_alpha,
            ):
                w.setEnabled(False)
        else:
            self._banner.setVisible(False)

    def _on_any_change(self, *_a) -> None:
        self.changed.emit()

    def _on_late_chunking_changed(self, *_a) -> None:
        self._refresh_fusion_visibility()
        self.changed.emit()

    def _refresh_fusion_visibility(self) -> None:
        """Q9=A: fusion_alpha is meaningful only when late_chunking is ON."""
        visible = bool(self._late_chunking.isChecked())
        self._fusion_row_label.setVisible(visible)
        self._fusion_alpha.setVisible(visible)


__all__ = ["SemanticOptionsWidget"]
