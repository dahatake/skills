"""mdq CLI - minimal interface intended for Skill/Agent invocation.

Usage:
    python -m mdq index   [--root PATH ...] [--rebuild]
    python -m mdq watch   [--root PATH ...] [--debounce-ms 500]
                          [--burst-threshold 100] [--burst-window-s 1.0]
                          [--initial-index]
    python -m mdq search  --q "..." [--paths GLOB ...] [--tags t1 t2]
                          [--top-k 5] [--max-tokens 800]
                          [--mode bm25|grep]
                          [--format jsonl|compact]
    python -m mdq get     --chunk-id ID
    python -m mdq list    [--paths GLOB ...] [--heading-level N]
    python -m mdq stats
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import indexer, search as searcher, store

# Default index root. The indexer walks the current working directory
# recursively, pruning a small set of well-known dependency / build directories
# (see ``indexer.DEFAULT_EXCLUDE_DIRS``). Pass ``--root`` to override.
DEFAULT_ROOTS = ["."]


def _add_db_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("--db", default=str(store.DEFAULT_DB_PATH),
                   help="SQLite store path (default: .mdq/index.sqlite)")


def cmd_index(args: argparse.Namespace) -> int:
    roots = args.root or DEFAULT_ROOTS
    repo_root = Path.cwd()
    conn = store.open_store(args.db)
    summary = indexer.build_index(
        repo_root, roots, conn,
        rebuild=args.rebuild,
        prune=not args.no_prune,
        max_chunk_chars=args.max_chunk_chars,
    )
    summary["roots"] = roots
    print(json.dumps(summary, ensure_ascii=False))
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    conn = store.open_store(args.db)
    hits = searcher.search(
        conn, args.q,
        mode=args.mode,
        top_k=args.top_k,
        max_tokens=args.max_tokens,
        path_globs=args.paths or None,
        tags=args.tags or None,
        snippet_radius=args.snippet_radius,
        include_parent=args.include_parent,
        expand_neighbors=args.expand_neighbors,
        merge_parts=args.merge_parts,
        engine=args.engine,
    )
    if args.format == "compact":
        for h in hits:
            print(f"{h.path}:{h.start_line}-{h.end_line}  "
                  f"[{h.heading_path}]  score={h.score:.2f}")
            for ln in h.snippet.splitlines():
                print(f"  | {ln}")
    else:
        for h in hits:
            print(json.dumps(h.to_dict(), ensure_ascii=False))
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    conn = store.open_store(args.db)
    chunk = searcher.get_chunk(conn, args.chunk_id)
    if not chunk:
        print(json.dumps({"error": "not_found"}), file=sys.stderr)
        return 1
    print(json.dumps(chunk, ensure_ascii=False))
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    conn = store.open_store(args.db)
    items = searcher.list_chunks(
        conn,
        path_globs=args.paths or None,
        heading_level=args.heading_level,
        limit=args.limit,
    )
    for it in items:
        print(json.dumps(it, ensure_ascii=False))
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    conn = store.open_store(args.db)
    print(json.dumps(store.stats(conn), ensure_ascii=False))
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    """`python -m mdq watch` - フォアグラウンドでリアルタイム索引更新。

    既存の ``index`` サブコマンドはそのまま残しており、ユーザーが手動で索引を
    更新する選択肢は維持されている。本コマンドは開発時のスタンドアロン
    動作確認用、または別プロセスで watcher だけを動かしたい場合のためのもの。
    Ctrl+C で停止する。
    """
    import logging
    import signal
    import time as _time

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    try:
        from . import watcher as _watcher_mod
    except ImportError as exc:  # pragma: no cover - defensive
        print(json.dumps({"error": f"watcher import failed: {exc}"}),
              file=sys.stderr)
        return 1

    roots = args.root or DEFAULT_ROOTS
    repo_root = Path.cwd()
    db_path = Path(args.db)

    # 初回索引は明示的に要求された場合のみ実行する（ユーザーが既存 `index`
    # を直前に流していれば二重実行を避けたい）。
    if args.initial_index:
        conn = store.open_store(db_path)
        summary = indexer.build_index(repo_root, roots, conn,
                                      rebuild=False, prune=True)
        summary["roots"] = roots
        print(json.dumps({"initial_index": summary}, ensure_ascii=False))
        try:
            conn.close()
        except Exception:
            pass

    w = _watcher_mod.MdqWatcher(
        repo_root=repo_root,
        roots=roots,
        db_path=db_path,
        debounce_ms=args.debounce_ms,
        burst_threshold=args.burst_threshold,
        burst_window_s=args.burst_window_s,
    )
    ok = w.start()
    if not ok:
        print(json.dumps({"error": "watcher start failed (watchdog 未導入か)"}),
              file=sys.stderr)
        return 1

    print(json.dumps({
        "status": "watching",
        "roots": roots,
        "db": str(db_path),
        "debounce_ms": args.debounce_ms,
    }, ensure_ascii=False))

    stop = threading_event_or_none()

    def _handle_sigint(signum, frame):  # type: ignore[no-untyped-def]
        if stop is not None:
            stop.set()

    try:
        signal.signal(signal.SIGINT, _handle_sigint)
    except Exception:
        pass

    try:
        if stop is not None:
            stop.wait()
        else:  # pragma: no cover - fallback
            while True:
                _time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        w.stop()
    return 0


def threading_event_or_none():
    """threading.Event を返す（テスト容易性のため関数化）。"""
    try:
        import threading
        return threading.Event()
    except Exception:  # pragma: no cover
        return None


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mdq",
                                description="Local-only Markdown query toolkit")
    sub = p.add_subparsers(dest="command", required=True)

    p_idx = sub.add_parser("index", help="Build or update the index")
    _add_db_arg(p_idx)
    p_idx.add_argument("--root", action="append",
                       help=f"Index root (repeatable, default: {DEFAULT_ROOTS})")
    p_idx.add_argument("--rebuild", action="store_true",
                       help="Rebuild even if file hashes match")
    p_idx.add_argument("--no-prune", action="store_true",
                       help="Do not remove store entries for files that no "
                            "longer exist on disk (default: prune enabled)")
    p_idx.add_argument("--max-chunk-chars", type=int, default=0,
                       help="If >0, sub-split chunks larger than this many "
                            "chars at paragraph/line boundaries (code fences "
                            "stay intact). 0 disables (default).")
    p_idx.set_defaults(func=cmd_index)

    p_s = sub.add_parser("search", help="Search the index")
    _add_db_arg(p_s)
    p_s.add_argument("--q", required=True, help="Query string")
    p_s.add_argument("--mode", choices=["bm25", "grep"], default="bm25")
    p_s.add_argument("--top-k", type=int, default=5)
    p_s.add_argument("--max-tokens", type=int, default=800,
                     help="Approximate snippet-token budget across all hits")
    p_s.add_argument("--paths", nargs="*", help="Path glob filters (fnmatch)")
    p_s.add_argument("--tags", nargs="*", help="Frontmatter tag filters (AND)")
    p_s.add_argument("--snippet-radius", type=int, default=2)
    p_s.add_argument("--include-parent", action="store_true",
                     help="Include the parent heading's chunk in expansion.")
    p_s.add_argument("--expand-neighbors", type=int, default=0, metavar="N",
                     help="Include N adjacent chunks (before/after) per hit.")
    p_s.add_argument("--merge-parts", action="store_true",
                     help="Include sibling parts (part_total>1) of the hit.")
    p_s.add_argument("--engine", choices=["auto", "bm25", "fts5"],
                     default="auto",
                     help="Search engine. 'auto' uses FTS5 when MDQ_FTS5 "
                          "env is set and supported, otherwise in-memory "
                          "BM25 (default).")
    p_s.add_argument("--format", choices=["jsonl", "compact"], default="jsonl")
    p_s.set_defaults(func=cmd_search)

    p_g = sub.add_parser("get", help="Fetch a single chunk by chunk_id")
    _add_db_arg(p_g)
    p_g.add_argument("--chunk-id", required=True)
    p_g.set_defaults(func=cmd_get)

    p_l = sub.add_parser("list", help="List indexed chunks (headings)")
    _add_db_arg(p_l)
    p_l.add_argument("--paths", nargs="*")
    p_l.add_argument("--heading-level", type=int, default=None)
    p_l.add_argument("--limit", type=int, default=200)
    p_l.set_defaults(func=cmd_list)

    p_st = sub.add_parser("stats", help="Show index statistics")
    _add_db_arg(p_st)
    p_st.set_defaults(func=cmd_stats)

    p_w = sub.add_parser(
        "watch",
        help="Realtime index updates via watchdog",
    )
    _add_db_arg(p_w)
    p_w.add_argument("--root", action="append",
                     help=f"Watch root (repeatable, default: {DEFAULT_ROOTS})")
    p_w.add_argument("--debounce-ms", type=int, default=500,
                     help="同一ファイルへの連続イベントを抑制するデバウンス時間 (ms, 既定 500)")
    p_w.add_argument("--burst-threshold", type=int, default=100,
                     help="バースト検出閾値（イベント件数 / burst-window-s）。"
                          "超過時は build_index で全 root を再走査する (既定 100)")
    p_w.add_argument("--burst-window-s", type=float, default=1.0,
                     help="バースト検出ウィンドウ秒数 (既定 1.0)")
    p_w.add_argument("--initial-index", action="store_true",
                     help="watch 開始前に build_index を 1 回実行する")
    p_w.set_defaults(func=cmd_watch)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
