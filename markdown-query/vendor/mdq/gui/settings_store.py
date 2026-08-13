"""Settings store for the Markdown-Query GUI.

Two file locations are supported:

1. **hve mode** — when the target repository is the HVE source tree itself,
   the existing INI at ``<repo_root>/hve/.settings.txt`` is used so that the
   HVE GUI and the standalone launcher read and write the same file.
   Detection: ``<repo_root>/hve/.settings.txt`` exists OR ``<repo_root>/hve``
   is a directory.
2. **standalone mode** — otherwise, ``<repo_root>/.mdq-gui-settings.txt`` is
   used (created on first save).

Detection is path-based on purpose: this module must not import ``hve``
(FR-KIT-05). Hosts that own a different settings API inject an adapter
implementing the same functions instead.

Only the ``[mdq]`` section is read/written by this module. Other sections
present in the file (e.g. ``[options]`` used by HVE) are preserved verbatim
across save() calls.
"""

from __future__ import annotations

import configparser
import os
from pathlib import Path
from typing import Any, Dict, List, Optional


# Default values for the [mdq] section. Keep in sync with HVE's defaults.
def defaults() -> Dict[str, Any]:
    return {
        "auto_refresh_on_start": False,
        "tokenize_language": "ja-jp",
        "chunk_strategy": "heading",
        "target_folders": "",
        # heading_recursive 戦略専用: text 系サブチャンクへ重ねる段落数。
        # 0 で overlap 無効。既定値は mdq.strategies と一致 (=1)。
        "overlap_paragraphs": 1,
        # 一括ビルド対象 Strategy 群 (T17)。
        # ";" 区切りの Strategy 名リスト。空文字列 = 全 Strategy 選択扱い。
        # 既定: 空 (= 全アクティブ。Q3 ご回答「全 Strategy アクティブ」に整合)。
        # 値の解釈は ``parse_build_strategies()`` を介す。
        "build_strategies": "",
        # --- semantic_paragraph 戦略専用 (Q3=A 単一プロファイル) -----------
        # CLI フラグと 1:1 対応。空文字列 / 0 値は mdq.strategies_semantic
        # の SEMANTIC_* 定数（コード側 SoT）にフォールバックする。
        "semantic_max_chunk_chars": 0,        # 0=コード側既定 1000 を採用
        "semantic_min_chars": 0,              # 0=コード側既定 200 を採用
        "semantic_breakpoint_percentile_lo": 0.0,  # 0.0=コード側既定 50.0
        "semantic_breakpoint_percentile_hi": 0.0,  # 0.0=コード側既定 99.0
        "semantic_embed_provider": "",        # 空=fastembed (env override 可)
        "semantic_embed_model": "",           # 空=BAAI/bge-m3 (env override 可)
        "semantic_contextualize": True,       # Q11=B 既定 ON
        "semantic_late_chunking": False,      # Q9=B opt-in
        "semantic_fusion_alpha": 0.5,         # late_chunking ON 時の検索融合係数
        # Q6=B: 大容量モデル初回 DL 確認ダイアログを「以後表示しない」フラグ
        # (key 名は QSettings 下位互換性のため bge_m3 のまま保持)
        "semantic_bge_m3_warning_dismissed": False,
        # --- pageindex 戦略専用 ---------------------------------------
        # CLI フラグと 1:1 対応。0 / "" 値は mdq.strategies の
        # PAGEINDEX_* 定数（コード側 SoT）にフォールバックする。
        "pageindex_summary_chars": 0,         # 0=コード側既定 200 を採用
        "pageindex_summary_mode": "head",     # head / first_paragraph
    }


def detect_settings_path(repo_root: Path) -> Path:
    """Return the path to the INI file to read/write.

    - HVE mode: ``<repo_root>/hve/.settings.txt`` (created lazily).
    - Standalone mode: ``<repo_root>/.mdq-gui-settings.txt``.
    """
    hve_settings = repo_root / "hve" / ".settings.txt"
    if hve_settings.exists() or (repo_root / "hve").is_dir():
        return hve_settings
    return repo_root / ".mdq-gui-settings.txt"


def load(repo_root: Path) -> Dict[str, Dict[str, Any]]:
    """Load the full settings (all sections) from disk.

    Returns at minimum ``{"mdq": defaults()}``; other sections are preserved
    if present.
    """
    path = detect_settings_path(repo_root)
    merged: Dict[str, Dict[str, Any]] = {"mdq": dict(defaults())}
    if not path.exists():
        return merged
    cp = configparser.ConfigParser()
    try:
        cp.read(path, encoding="utf-8")
    except (configparser.Error, OSError):
        return merged
    for section in cp.sections():
        if section not in merged:
            merged[section] = {}
        for key, raw_value in cp.items(section):
            if section == "mdq":
                default_value = defaults().get(key)
                merged[section][key] = _coerce(raw_value, default_value)
            else:
                # Preserve verbatim for non-mdq sections (HVE owns them).
                merged[section][key] = raw_value
    return merged


def save(repo_root: Path, settings: Dict[str, Dict[str, Any]]) -> None:
    """Atomically save settings. Preserves non-[mdq] sections from disk."""
    path = detect_settings_path(repo_root)
    cp = configparser.ConfigParser()

    # Re-read any current file to preserve sections we do not own.
    if path.exists():
        try:
            cp.read(path, encoding="utf-8")
        except (configparser.Error, OSError):
            cp = configparser.ConfigParser()

    for section, vals in settings.items():
        if section not in cp:
            cp[section] = {}
        for k, v in vals.items():
            cp[section][k] = _to_str(v)

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        cp.write(f)
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# Target folders helpers (parity with hve.gui.settings_store).
# ---------------------------------------------------------------------------
def _normalize_target_folder(raw: str) -> Optional[str]:
    s = (raw or "").strip().strip('"').strip("'")
    if not s:
        return None
    s = s.replace("\\", "/")
    while s.endswith("/") and len(s) > 1:
        s = s[:-1]
    if s in ("", "."):
        return None
    return s


def parse_target_folders(raw: str) -> List[str]:
    out: List[str] = []
    seen: set[str] = set()
    for part in (raw or "").split(";"):
        norm = _normalize_target_folder(part)
        if norm is None or norm in seen:
            continue
        seen.add(norm)
        out.append(norm)
    return out


def serialize_target_folders(folders: List[str]) -> str:
    normed: List[str] = []
    seen: set[str] = set()
    for item in folders or []:
        norm = _normalize_target_folder(str(item))
        if norm is None or norm in seen:
            continue
        seen.add(norm)
        normed.append(norm)
    return ";".join(normed)


def get_mdq_target_folders(
    repo_root: Path,
    *,
    settings: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[str]:
    s = settings if settings is not None else load(repo_root)
    raw = s.get("mdq", {}).get("target_folders", "")
    return parse_target_folders(str(raw))


# ---------------------------------------------------------------------------
# Build-strategies helpers (T17).
# ---------------------------------------------------------------------------
def known_strategies() -> tuple[str, ...]:
    """Return the SoT strategy list (``mdq.strategies.ALL_STRATEGIES``)."""
    from mdq.strategies import ALL_STRATEGIES

    return tuple(ALL_STRATEGIES)


# 後方互換: 既存テストが参照する private 名を維持。
_known_strategies = known_strategies


def parse_build_strategies(raw: str) -> List[str]:
    """``build_strategies`` 文字列を Strategy 名のリストへ変換する。

    - 空文字列 → 全 Strategy (``known_strategies()``) を返す
    - ";" 区切り。空白除去・重複除去
    - 未知の Strategy 名は黙って除外（後方互換）
    - 並び順は ``known_strategies()`` の順序を保持

    **注意 (No.11)**: 空文字列は「未指定 = 全選択」と解釈されるため、
    「空選択を永続化する」ことはできない。ユーザーが全チェックを外した
    状態は保存可能だが、次回ロード時には全選択へ戻る。これは設定肥大化
    回避のためのトレードオフであり、空選択を意図的に維持したいユースケース
    は想定していない (Q3「全 Strategy アクティブ」前提)。
    """
    known = known_strategies()
    s = (raw or "").strip()
    if not s:
        return list(known)
    tokens = {t.strip() for t in s.split(";") if t.strip()}
    return [k for k in known if k in tokens]


def serialize_build_strategies(strategies: List[str]) -> str:
    """``parse_build_strategies`` の逆変換。

    - 既知 Strategy のみ採用、順序は ``known_strategies()`` に従う
    - 全 Strategy が選択されている場合は空文字列を返す（"未指定 = 全選択"
      規約と一致させてファイル肥大化を抑制）

    **注意 (No.11)**: 空リストが渡された場合も空文字列を返す。これは
    「空選択を永続化できない」仕様による (``parse_build_strategies``
    docstring 参照)。
    """
    known = known_strategies()
    selected_set = {s for s in strategies or [] if s in known}
    if len(selected_set) == len(known):
        return ""
    return ";".join(k for k in known if k in selected_set)


def get_mdq_build_strategies(
    repo_root: Path,
    *,
    settings: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[str]:
    s = settings if settings is not None else load(repo_root)
    raw = s.get("mdq", {}).get("build_strategies", "")
    return parse_build_strategies(str(raw))


# ---------------------------------------------------------------------------
# semantic_paragraph helpers (Q3=A 単一プロファイル).
# ---------------------------------------------------------------------------
def get_semantic_runtime_config(
    repo_root: Path,
    *,
    settings: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Return kwargs for ``mdq.strategies_semantic.set_runtime_config``.

    0 / "" / None / False の値は **省略** し、コード側の SEMANTIC_* 既定値が
    使われるようにする。``set_runtime_config`` は ``v is not None`` の値のみ
    保持するため、本関数では「未指定」を None に正規化する。

    Q11=B (contextualize 既定 ON) は **明示的に True/False を渡す** ことで、
    GUI からユーザーが OFF にした場合に確実に反映できるようにする。
    """
    s = settings if settings is not None else load(repo_root)
    mdq = s.get("mdq", {})
    out: Dict[str, Any] = {}
    # 数値系: 0 はコード既定にフォールバックさせるため None に正規化。
    for src, dst in (
        ("semantic_max_chunk_chars", "max_chars"),
        ("semantic_min_chars", "min_chars"),
    ):
        try:
            v = int(mdq.get(src, 0) or 0)
        except (TypeError, ValueError):
            v = 0
        if v > 0:
            out[dst] = v
    for src, dst in (
        ("semantic_breakpoint_percentile_lo", "percentile_lo"),
        ("semantic_breakpoint_percentile_hi", "percentile_hi"),
    ):
        try:
            v = float(mdq.get(src, 0.0) or 0.0)
        except (TypeError, ValueError):
            v = 0.0
        if v > 0:
            out[dst] = v
    # 文字列系: 空は env / コード既定にフォールバック。
    for src, dst in (
        ("semantic_embed_provider", "embed_provider"),
        ("semantic_embed_model", "embed_model"),
    ):
        raw = str(mdq.get(src, "") or "").strip()
        if raw:
            out[dst] = raw
    # bool 系: 明示的に渡す（既定値を上書きするケースが正常パス）。
    out["contextualize"] = bool(mdq.get("semantic_contextualize", True))
    out["late_chunking"] = bool(mdq.get("semantic_late_chunking", False))
    return out


def get_semantic_fusion_alpha(
    repo_root: Path,
    *,
    settings: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Optional[float]:
    """Return the fusion_alpha setting, or None when invalid.

    検索時に ``mdq.search.search(..., fusion_alpha=...)`` へ渡す。
    範囲外 (0.0〜1.0 を超える) は ``None`` で fusion を無効化する。
    """
    s = settings if settings is not None else load(repo_root)
    raw = s.get("mdq", {}).get("semantic_fusion_alpha", 0.5)
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    if not (0.0 <= v <= 1.0):
        return None
    return v


# ---------------------------------------------------------------------------
# Type coercion helpers.
# ---------------------------------------------------------------------------
def _to_str(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    return str(value)


def _coerce(raw: str, default: Any) -> Any:
    if isinstance(default, bool):
        return raw.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(default, int) and not isinstance(default, bool):
        try:
            return int(raw)
        except ValueError:
            return default
    if isinstance(default, float):
        try:
            return float(raw)
        except ValueError:
            return default
    return raw
