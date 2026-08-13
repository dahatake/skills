"""Background workers shared by standalone and HVE Code Query GUIs."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import QWidget

from . import index_service


class CqIndexBuildThread(QThread):
    succeeded = Signal(dict)
    failed = Signal(str)

    def __init__(
        self,
        *,
        repo_root: Path,
        profile: str,
        rebuild: bool = False,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._repo_root = repo_root
        self._profile = profile
        self._rebuild = bool(rebuild)

    def run(self) -> None:  # type: ignore[override]
        try:
            report = index_service.build(
                self._repo_root, self._profile, rebuild=self._rebuild
            )
        except Exception as exc:  # noqa: BLE001 - deliver worker failures to the UI
            self.failed.emit(str(exc))
            return
        if report.get("error"):
            self.failed.emit(str(report["error"]))
            return
        self.succeeded.emit(report)


class CqSearchPreviewThread(QThread):
    succeeded = Signal(dict)
    failed = Signal(str)

    def __init__(
        self,
        *,
        repo_root: Path,
        profile: str,
        query: str,
        mode: str = "auto",
        top_k: int = 5,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._repo_root = repo_root
        self._profile = profile
        self._query = query
        self._mode = mode
        self._top_k = int(top_k)

    def run(self) -> None:  # type: ignore[override]
        try:
            result = index_service.search_preview(
                self._repo_root,
                self._profile,
                self._query,
                mode=self._mode,
                top_k=self._top_k,
            )
        except Exception as exc:  # noqa: BLE001 - deliver worker failures to the UI
            self.failed.emit(str(exc))
            return
        if result.get("error"):
            self.failed.emit(str(result["error"]))
            return
        self.succeeded.emit(result)


class CqBenchmarkThread(QThread):
    succeeded = Signal(str)
    failed = Signal(str)

    def __init__(
        self,
        *,
        repo_root: Path,
        profile: str,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._repo_root = repo_root
        self._profile = profile

    def command(self) -> list[str]:
        return [
            sys.executable,
            "-m",
            "cq.benchmark",
            "--repo-root",
            str(self._repo_root),
            "--profile",
            self._profile,
            "--with-cq",
        ]

    def run(self) -> None:  # type: ignore[override]
        try:
            completed = subprocess.run(
                self.command(),
                cwd=self._repo_root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            self.failed.emit(str(exc))
            return
        if completed.returncode != 0:
            self.failed.emit(
                (completed.stderr or completed.stdout or "").strip()
                or f"exit code {completed.returncode}"
            )
            return
        self.succeeded.emit(completed.stdout)
