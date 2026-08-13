"""Minimal standalone reimplementation of small Qt widgets that the
``settings_section`` module depends on.

The HVE GUI ships richer versions (with help popups, i18n integration, etc.).
For the standalone Skill we only need the visual structure, so the widgets
here intentionally drop those HVE-only dependencies.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QLabel,
    QVBoxLayout,
    QWidget,
)


class LabeledField(QWidget):
    """Title (bold) + optional description tooltip + input widget."""

    def __init__(
        self,
        title: str,
        description: str,
        input_widget: QWidget,
        *,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 4, 0, 4)
        outer.setSpacing(2)

        title_label = QLabel(title)
        title_label.setStyleSheet("font-weight: bold; font-size: 10pt;")
        outer.addWidget(title_label)

        if description:
            desc = QLabel(description)
            desc.setWordWrap(True)
            desc.setStyleSheet("font-size: 9pt;")
            outer.addWidget(desc)
            input_widget.setToolTip(description)

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(input_widget)
        row.addStretch(1)
        outer.addLayout(row)


class TriStateCombo(QComboBox):
    """3-state selector matching ``argparse.BooleanOptionalAction``.

    UserData:
      "inherit" → None
      "on"      → True
      "off"     → False
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.addItem(self.tr("継承（未指定）"), userData="inherit")
        self.addItem(self.tr("明示 ON"), userData="on")
        self.addItem(self.tr("明示 OFF"), userData="off")

    def get_tristate(self) -> Optional[bool]:
        data = self.currentData()
        if data == "on":
            return True
        if data == "off":
            return False
        return None

    def set_tristate(self, value: Optional[bool]) -> None:
        if value is True:
            self.setCurrentIndex(1)
        elif value is False:
            self.setCurrentIndex(2)
        else:
            self.setCurrentIndex(0)
