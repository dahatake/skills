"""Standalone QMainWindow for the Markdown-Query GUI."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import QMainWindow, QWidget

from .settings_section import MdqIndexSection


class StandaloneWindow(QMainWindow):
    """A minimal QMainWindow that hosts only the MdqIndexSection."""

    def __init__(
        self, *, repo_root: Path, parent: Optional[QWidget] = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Markdown-Query 設定")
        self.resize(900, 700)
        self._section = MdqIndexSection(repo_root=repo_root, parent=self)
        self.setCentralWidget(self._section)
