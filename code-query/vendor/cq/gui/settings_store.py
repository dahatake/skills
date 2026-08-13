"""Repository-local settings store for the standalone Code Query GUI.

HVE repositories reuse ``hve/.settings.txt``.  Other repositories keep GUI
state in ``.cq-gui-settings.txt``.  Only ``[cq]`` and the two CQ watcher keys
in ``[options]`` are owned by this module; all other data is preserved.
"""

from __future__ import annotations

import configparser
import os
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

_CQ_DEFAULTS: Dict[str, Any] = {
    "profile": "",
    "build_profiles": "",
}
_OPTION_DEFAULTS: Dict[str, Any] = {
    "cq_watch": "",
    "cq_watch_debounce_ms": 0,
}


def defaults() -> Dict[str, Dict[str, Any]]:
    return {
        "cq": dict(_CQ_DEFAULTS),
        "options": dict(_OPTION_DEFAULTS),
    }


def detect_settings_path(repo_root: Path) -> Path:
    root = Path(repo_root).resolve()
    hve_settings = root / "hve" / ".settings.txt"
    if hve_settings.exists() or (root / "hve").is_dir():
        return hve_settings
    return root / ".cq-gui-settings.txt"


def load(repo_root: Path) -> Dict[str, Dict[str, Any]]:
    path = detect_settings_path(repo_root)
    merged = defaults()
    if not path.exists():
        return merged

    parser = configparser.ConfigParser()
    try:
        parser.read(path, encoding="utf-8")
    except (configparser.Error, OSError):
        return merged

    for section in parser.sections():
        target = merged.setdefault(section, {})
        for key, raw in parser.items(section):
            if section == "options" and key == "cq_watch_debounce_ms":
                try:
                    target[key] = int(raw)
                except ValueError:
                    target[key] = _OPTION_DEFAULTS[key]
            else:
                target[key] = raw
    return merged


def save(repo_root: Path, settings: Dict[str, Dict[str, Any]]) -> None:
    """Atomically save CQ-owned values while preserving all foreign settings."""
    path = detect_settings_path(repo_root)
    parser = configparser.ConfigParser()
    if path.exists():
        try:
            parser.read(path, encoding="utf-8")
        except (configparser.Error, OSError):
            parser = configparser.ConfigParser()

    if "cq" not in parser:
        parser["cq"] = {}
    source_cq = settings.get("cq", {})
    for key, default in _CQ_DEFAULTS.items():
        parser["cq"][key] = _to_string(source_cq.get(key, default))

    if "options" not in parser:
        parser["options"] = {}
    source_options = settings.get("options", {})
    for key, default in _OPTION_DEFAULTS.items():
        parser["options"][key] = _to_string(source_options.get(key, default))

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        parser.write(handle)
    os.replace(temporary, path)


def parse_semicolon_list(raw: str) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for item in (raw or "").split(";"):
        value = item.strip()
        if not value or value in seen:
            continue
        seen.add(value)
        values.append(value)
    return values


def serialize_semicolon_list(values: Optional[Iterable[str]]) -> str:
    normalised: list[str] = []
    seen: set[str] = set()
    for item in values or ():
        value = str(item).strip()
        if not value or value in seen:
            continue
        seen.add(value)
        normalised.append(value)
    return ";".join(normalised)


def _to_string(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value)
