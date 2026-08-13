"""Standalone window for managing a Code Query index."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from PySide6.QtWidgets import QMainWindow, QWidget

from .settings_section import CqIndexSection


class StandaloneWindow(QMainWindow):
    """Host the shared Code Query panel for one explicit repository root."""

    def __init__(
        self, *, repo_root: Path, parent: Optional[QWidget] = None
    ) -> None:
        super().__init__(parent)
        self.repo_root = Path(repo_root).resolve()
        self.setWindowTitle(f"Code-Query 管理 — {self.repo_root}")
        self.resize(960, 760)
        self._section = CqIndexSection(repo_root=self.repo_root, parent=self)
        self.setCentralWidget(self._section)
