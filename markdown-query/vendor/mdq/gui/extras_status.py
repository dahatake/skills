"""Detect optional dependencies needed by the `semantic_paragraph` strategy.

Returns a structured status used by the GUI to render a banner and to
enable/disable build buttons (Q5=A: warning + disabled, no auto pip).

Public API:
- :func:`probe() -> ExtrasStatus` — pure-Python probe. Safe to call from
  any thread; performs only ``import`` attempts (no subprocess).
- :class:`ExtrasStatus` — frozen dataclass.

The probe is intentionally fast (microseconds) so the GUI can call it on
every panel refresh without batching.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass(frozen=True)
class ExtrasStatus:
    """Snapshot of optional-extras availability."""

    fastembed_available: bool
    nltk_available: bool
    numpy_available: bool
    missing: tuple[str, ...] = field(default_factory=tuple)

    @property
    def semantic_ok(self) -> bool:
        """True iff all packages required for `semantic_paragraph` import."""
        return (
            self.fastembed_available
            and self.nltk_available
            and self.numpy_available
        )

    def install_hint(self) -> str:
        """Return a one-line install hint for the GUI banner."""
        return "pip install -e .[semantic]"

    def banner_message(self) -> str:
        """Return a user-facing message (Japanese; SoT)."""
        if self.semantic_ok:
            return ""
        names = ", ".join(self.missing) or "semantic 依存"
        return (
            f"[semantic] extra が未インストールです（不足: {names}）。"
            "semantic_paragraph 戦略のビルドは無効化されています。"
            f"次のコマンドでインストール: {self.install_hint()}"
        )


def _try_import(module_name: str) -> bool:
    """Return True if ``import module_name`` succeeds without side effects."""
    try:
        __import__(module_name)
        return True
    except ImportError:
        return False
    except Exception:  # noqa: BLE001 -- defensive: corrupt install etc.
        return False


def probe() -> ExtrasStatus:
    """Detect required packages for the `semantic_paragraph` strategy."""
    fe = _try_import("fastembed")
    nl = _try_import("nltk")
    np = _try_import("numpy")
    missing: List[str] = []
    if not fe:
        missing.append("fastembed")
    if not nl:
        missing.append("nltk")
    if not np:
        missing.append("numpy")
    return ExtrasStatus(
        fastembed_available=fe,
        nltk_available=nl,
        numpy_available=np,
        missing=tuple(missing),
    )


__all__ = ["ExtrasStatus", "probe"]
