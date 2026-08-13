"""Realtime Markdown index watcher for HVE CLI Orchestrator.

ファイル追加・更新・削除を ``watchdog`` で検知し、``.mdq/index.sqlite`` を
逐次更新する。HVE CLI Orchestrator 実行プロセスの中で **デーモンスレッド**
として動作し、プロセス終了とともに自動的に停止する。

設計のポイント:
- ``watchdog`` 未導入時は ``MdqWatcher.start`` が ``False`` を返し、CLI の
  起動を妨げない（オプション機能、ハードフェイル禁止）。
- 全イベントは内部キューに直列化し、watcher 専用 SQLite 接続 1 本で書き込む
  （SQLite のスレッド競合を回避）。
- デバウンス（既定 500ms）で同一ファイルの連打を抑制し、同一ファイルへの
  複数イベントは最後の状態だけを反映する。
- スコープ外（既定 11 root 以外）のイベントは無視。
- バーストイベント（一定間隔内に閾値を超える件数）は最後にまとめて
  ``build_index`` で全 root の prune 付き再走査を行いフォールバック。

公開 API:
- ``MdqWatcher(repo_root, roots, db_path, debounce_ms=500, burst_threshold=100, burst_window_s=1.0)``
- ``watcher.start() -> bool``  # True=起動成功 / False=未導入 or 起動失敗
- ``watcher.stop() -> None``

このモジュールは Cloud Agent / GitHub Actions では使用しない（ユーザー指示）。
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Optional

from . import config as _config
from . import indexer as _indexer
from . import store as _store

logger = logging.getLogger(__name__)

# 既定値（設計値、実測ではない）
DEFAULT_DEBOUNCE_MS = 500
DEFAULT_BURST_THRESHOLD = 100   # 1 ウィンドウあたりのイベント件数
DEFAULT_BURST_WINDOW_S = 1.0    # ウィンドウ長（秒）


def _is_markdown(path: Path) -> bool:
    return path.suffix.lower() == ".md"


def _within_roots(rel_posix: str, roots: Iterable[str]) -> bool:
    """``rel_posix`` が ``roots`` のいずれか配下にあれば True。"""
    for r in roots:
        r = r.rstrip("/")
        if rel_posix == r or rel_posix.startswith(r + "/"):
            return True
    return False


def _excluded(rel_posix: str, roots: Iterable[str], patterns: Iterable[str]) -> bool:
    """索引ビルドと同じ除外規則。これが無いと watch だけが除外を無視する。"""
    depth = 0
    for r in roots:
        cleaned = str(r).strip("/")
        if cleaned and (rel_posix == cleaned or rel_posix.startswith(cleaned + "/")):
            depth = max(depth, len(PurePosixPath(cleaned).parts))
    if _indexer._in_excluded_dir(PurePosixPath(rel_posix), skip=depth):
        return True
    return any(_indexer._matches_glob(rel_posix, pattern) for pattern in patterns)


class MdqWatcher:
    """Markdown ファイル変更を OS イベントで検知して mdq 索引を逐次更新する。

    使用例:
        watcher = MdqWatcher(repo_root=Path.cwd(), roots=DEFAULT_ROOTS,
                             db_path=Path(".mdq/index.sqlite"))
        if watcher.start():
            try:
                ...  # メイン処理
            finally:
                watcher.stop()
    """

    def __init__(
        self,
        repo_root: Path,
        roots: Iterable[str],
        db_path: Path,
        *,
        debounce_ms: int = DEFAULT_DEBOUNCE_MS,
        burst_threshold: int = DEFAULT_BURST_THRESHOLD,
        burst_window_s: float = DEFAULT_BURST_WINDOW_S,
        lang: str = "ja-jp",
        strategy: str = "heading",
    ) -> None:
        self.repo_root = Path(repo_root).resolve()
        self.roots = [r.rstrip("/") for r in roots]
        # Read once: the watcher must apply the same [index].exclude as a build.
        self._excludes = _config.resolve_excludes(self.repo_root)
        # Worker threadの実行中にprocess CWDが変わっても、relative DB pathを
        # 別workflow/testの作業ディレクトリへ作成しないよう起動元repoへ固定する。
        self.db_path = (self.repo_root / Path(db_path)).resolve()
        self.debounce_ms = max(0, int(debounce_ms))
        self.burst_threshold = max(1, int(burst_threshold))
        self.burst_window_s = max(0.1, float(burst_window_s))
        self.lang = lang
        self.strategy = strategy

        self._observer: Optional[Any] = None
        self._worker: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        # 保留中: rel_path -> ("update"|"delete", earliest_ts)
        # earliest_ts はバッチ flush 判定用（最初のイベント時刻）
        self._pending: dict[str, tuple[str, float]] = {}
        self._pending_lock = threading.Lock()
        # バースト検出（直近イベントタイムスタンプの単純カウンタ）
        self._recent_events: list[float] = []
        self._burst_lock = threading.Lock()
        self._fallback_pending = False

    # ─────────────────────────────────────────────────────────
    # 起動・停止
    # ─────────────────────────────────────────────────────────

    def start(self) -> bool:
        """watcher を開始する。

        Returns:
            True  : 起動成功（バックグラウンドで稼働中）
            False : watchdog 未導入 or 起動失敗（CLI は通常通り継続可）
        """
        try:
            from watchdog.observers import Observer  # type: ignore
            from watchdog.events import (  # type: ignore
                FileSystemEventHandler,
                FileCreatedEvent,
                FileModifiedEvent,
                FileDeletedEvent,
                FileMovedEvent,
            )
        except ImportError:
            logger.warning(
                "mdq watcher: 'watchdog' 未導入のためリアルタイム更新を無効化します。"
                " `pip install -e .[mdq-watch]` で有効化できます。"
            )
            return False
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("mdq watcher: watchdog 読み込みに失敗 (%s)。無効化します。", exc)
            return False

        # watchdog 必須: FileSystemEventHandler を継承した動的サブクラスを生成
        # （未導入環境でも本モジュールがインポート可能なように遅延継承）
        _Base = FileSystemEventHandler
        _Handler = type("_MdqEventHandlerBound", (_MdqEventHandler, _Base), {})
        handler = _Handler(self)
        observer = Observer()
        try:
            for r in self.roots:
                base = (self.repo_root / r)
                if not base.exists():
                    continue
                observer.schedule(handler, str(base), recursive=True)
            observer.daemon = True
            observer.start()
        except Exception as exc:
            logger.warning("mdq watcher: Observer 起動に失敗 (%s)。無効化します。", exc)
            return False

        self._observer = observer
        self._stop_event.clear()
        self._worker = threading.Thread(
            target=self._worker_loop, name="mdq-watcher", daemon=True,
        )
        self._worker.start()
        logger.info(
            "mdq watcher: 起動しました (roots=%d, debounce=%dms)",
            len(self.roots), self.debounce_ms,
        )
        return True

    def stop(self) -> None:
        """watcher を停止する（既に停止済みなら no-op）。"""
        if self._observer is not None:
            try:
                self._observer.stop()
                self._observer.join(timeout=2.0)
            except Exception:
                pass
            self._observer = None
        self._stop_event.set()
        if self._worker is not None:
            try:
                self._worker.join(timeout=2.0)
            except Exception:
                pass
            self._worker = None
        # 残っている保留分を最後に 1 回 flush する
        try:
            self._flush_once(final=True)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("mdq watcher: 終了時 flush で例外 (%s)", exc)

    # ─────────────────────────────────────────────────────────
    # 内部: イベント受領（watchdog ハンドラから呼ばれる）
    # ─────────────────────────────────────────────────────────

    def _enqueue(self, rel_path: str, action: str) -> None:
        """``action`` ∈ {"update", "delete"}。ロック内で保留 dict を更新する。"""
        if not _within_roots(rel_path, self.roots):
            return
        if _excluded(rel_path, self.roots, self._excludes):
            return
        now = time.monotonic()
        with self._pending_lock:
            existing = self._pending.get(rel_path)
            if existing is None:
                self._pending[rel_path] = (action, now)
            else:
                # delete > update（後勝ち。delete 後の再作成は update が後で上書き）
                self._pending[rel_path] = (action, existing[1])
        self._note_event(now)

    def _note_event(self, ts: float) -> None:
        with self._burst_lock:
            self._recent_events.append(ts)
            cutoff = ts - self.burst_window_s
            self._recent_events = [t for t in self._recent_events if t >= cutoff]
            if len(self._recent_events) >= self.burst_threshold:
                self._fallback_pending = True

    # ─────────────────────────────────────────────────────────
    # 内部: バックグラウンドワーカー
    # ─────────────────────────────────────────────────────────

    def _worker_loop(self) -> None:
        """``debounce_ms`` 間隔で保留分を flush する単純ループ。"""
        interval = max(0.05, self.debounce_ms / 1000.0)
        while not self._stop_event.wait(interval):
            try:
                self._flush_once(final=False)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("mdq watcher: flush で例外 (%s)", exc)

    def _flush_once(self, *, final: bool) -> None:
        """保留分を 1 回まとめて反映する。"""
        # バースト検出されていたら全体再走査にフォールバック
        with self._burst_lock:
            burst = self._fallback_pending
            if burst:
                self._fallback_pending = False
                self._recent_events.clear()
        if burst and not final:
            self._fallback_reindex()
            return

        with self._pending_lock:
            if not self._pending:
                return
            now = time.monotonic()
            ready: dict[str, str] = {}
            keep: dict[str, tuple[str, float]] = {}
            cutoff = self.debounce_ms / 1000.0
            for rel, (action, ts) in self._pending.items():
                if final or (now - ts) >= cutoff:
                    ready[rel] = action
                else:
                    keep[rel] = (action, ts)
            self._pending = keep

        if not ready:
            return

        # 専用接続で書き込む（worker スレッド専用）
        conn = _store.open_store(self.db_path, lang=self.lang)
        try:
            indexed = 0
            deleted = 0
            for rel, action in ready.items():
                if action == "delete":
                    res = _indexer.delete_one_file(rel, conn)
                    if res["action"] == "deleted":
                        deleted += 1
                else:
                    abs_path = self.repo_root / rel
                    res = _indexer.index_one_file(
                        self.repo_root, abs_path, conn,
                        strategy=self.strategy,
                    )
                    if res["action"] == "indexed":
                        indexed += 1
                    elif res["action"] == "missing":
                        # 連続的な create→delete の競合: 削除側で掃除
                        _indexer.delete_one_file(rel, conn)
            conn.commit()
            if indexed or deleted:
                logger.info(
                    "mdq watcher: flush indexed=%d deleted=%d", indexed, deleted,
                )
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _fallback_reindex(self) -> None:
        """バースト発生時の安全網: ``build_index`` で全 root を再走査する。"""
        logger.info(
            "mdq watcher: バースト検出 (>= %d events / %.1fs) → 全 root 再走査",
            self.burst_threshold, self.burst_window_s,
        )
        with self._pending_lock:
            self._pending.clear()
        conn = _store.open_store(self.db_path, lang=self.lang)
        try:
            _indexer.build_index(
                self.repo_root, self.roots, conn, prune=True,
                strategy=self.strategy,
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("mdq watcher: フォールバック再走査で例外 (%s)", exc)
        finally:
            try:
                conn.close()
            except Exception:
                pass


class _MdqEventHandler:
    """watchdog FileSystemEventHandler 相当（ダックタイピング）。

    watchdog 未導入環境でもインポート可能にするため、基底クラスを継承せず
    ``on_*`` メソッドだけを実装する（watchdog 側はメソッド名で dispatch する）。
    """

    def __init__(self, watcher: MdqWatcher) -> None:
        self._w = watcher

    def _rel(self, src: str) -> Optional[str]:
        try:
            p = Path(src).resolve()
            rel = p.relative_to(self._w.repo_root).as_posix()
            return rel
        except Exception:
            return None

    def on_created(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.is_directory:
            return
        rel = self._rel(event.src_path)
        if rel is None or not rel.lower().endswith(".md"):
            return
        self._w._enqueue(rel, "update")

    def on_modified(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.is_directory:
            return
        rel = self._rel(event.src_path)
        if rel is None or not rel.lower().endswith(".md"):
            return
        self._w._enqueue(rel, "update")

    def on_deleted(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.is_directory:
            return
        rel = self._rel(event.src_path)
        if rel is None or not rel.lower().endswith(".md"):
            return
        self._w._enqueue(rel, "delete")

    def on_moved(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.is_directory:
            return
        src = self._rel(event.src_path)
        dst = self._rel(getattr(event, "dest_path", ""))
        if src and src.lower().endswith(".md"):
            self._w._enqueue(src, "delete")
        if dst and dst.lower().endswith(".md"):
            self._w._enqueue(dst, "update")
