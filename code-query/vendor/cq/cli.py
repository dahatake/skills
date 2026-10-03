"""Command line interface for cq (FR-CQ-06).

Only the standard library and cq's own stdlib-backed modules are imported here:
`python -m cq search` must not pay for optional dependencies (NFR-CQ-01).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Sequence

from cq import config, discovery, indexer, search, store, traces, usage_log
from cq.search import get_chunk

# `hve` は上流リポジトリの profile 名。他リポジトリへコピーした場合は
# 存在しないため、環境変数で上書きできるようにする（FR-KIT-04）。
DEFAULT_PROFILE_ENV: str = "CQ_PROFILE"
_FALLBACK_PROFILE: str = "hve"


def default_profile() -> str:
    return os.environ.get(DEFAULT_PROFILE_ENV) or _FALLBACK_PROFILE


def resolve_default_profile(repo_root: Path, *, require_config: bool = False) -> str:
    """``--profile`` 省略時の profile を決める（FR-KIT-04）。

    宣言された profile が 1 つだけならそれを使う。他リポジトリでは上流の
    profile 名を知りえないためで、複数宣言されている場合は推測せず従来の
    fallback のままにする。環境変数の明示指定は常に優先する。

    ``require_config`` のときは設定不備をそのまま伝える。伝えないと、存在しない
    上流 profile の DB を探しにいき「索引が無い」という別の原因を報告してしまう。
    """
    override = os.environ.get(DEFAULT_PROFILE_ENV)
    if override:
        return override
    try:
        declared = config.resolve_profiles(repo_root)
    except config.ConfigError:
        if require_config:
            raise
        return _FALLBACK_PROFILE
    if len(declared) == 1:
        return next(iter(declared))
    return _FALLBACK_PROFILE


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--profile", default=None)
    parser.add_argument("--repo-root", default=".")
    parser.add_argument("--db", default=None, help="override the index path")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cq", description="local source-code search")
    sub = parser.add_subparsers(dest="command", required=True)

    index = sub.add_parser("index", help="build or update the index")
    _add_common(index)
    index.add_argument("--rebuild", action="store_true")
    index.add_argument("--embed", action="store_true",
                       help="also build the vector side-index for --semantic (FR-CQ-17)")

    stats = sub.add_parser("stats", help="report index contents")
    _add_common(stats)

    find = sub.add_parser("search", help="search the index")
    _add_common(find)
    find.add_argument("--q", dest="query")
    find.add_argument("--re", dest="regex")
    find.add_argument("--mode", default="auto", choices=("auto", *search.ROUTES))
    find.add_argument("--top-k", type=int, default=search.DEFAULT_TOP_K)
    find.add_argument("--max-tokens", type=int, default=search.DEFAULT_MAX_TOKENS)
    find.add_argument("--snippet-radius", type=int, default=search.DEFAULT_SNIPPET_RADIUS)
    find.add_argument("--regex-max-candidates", type=int,
                      default=search.DEFAULT_REGEX_MAX_CANDIDATES)
    find.add_argument("--paths", default=None, help="GLOB filter over repository paths")
    find.add_argument("--semantic", action="store_true",
                      help="add the vector route and merge the ranked lists (FR-CQ-17)")
    find.add_argument("--explain", action="store_true",
                      help="append the per-route execution record as the last line")
    find.add_argument(
        "--return-unit", default="line", choices=("line", "chunk", "symbol"),
        help="excerpt granularity: 'line' keeps the --snippet-radius window;"
             " 'chunk' returns the whole matching chunk and widens 'lines' to"
             " match, which fills --max-tokens with fewer hits",
    )
    find.add_argument(
        "--auto-reindex-limit", type=int, default=search.DEFAULT_AUTO_REINDEX_LIMIT,
        help="re-index up to N drifted files before answering (-1 disables the check)",
    )

    definition = sub.add_parser("def", help="locate a definition without its body")
    _add_common(definition)
    definition.add_argument("--symbol", required=True)
    definition.add_argument("--top-k", type=int, default=search.DEFAULT_TOP_K)

    get = sub.add_parser("get", help="print one chunk in full")
    _add_common(get)
    get.add_argument("--chunk-id", required=True)

    refs = sub.add_parser("refs", help="list references to a symbol")
    _add_common(refs)
    refs.add_argument("--symbol", required=True)
    refs.add_argument("--top-k", type=int, default=20)

    trace = sub.add_parser("trace", help="resolve traceability between code and docs")
    _add_common(trace)
    group = trace.add_mutually_exclusive_group(required=True)
    group.add_argument("--id", dest="trace_id")
    group.add_argument("--by-path", dest="by_path")
    trace.add_argument("--top-k", type=int, default=search.DEFAULT_TOP_K)

    watch = sub.add_parser("watch", help="keep the index current while sources change")
    _add_common(watch)
    watch.add_argument("--debounce-ms", type=int, default=None)

    overview = sub.add_parser("map", help="token-budgeted overview of the codebase")
    _add_common(overview)
    overview.add_argument("--paths", default=None, help="GLOB filter over repository paths")
    overview.add_argument("--max-tokens", type=int, default=None)
    overview.add_argument("--format", default="text", choices=("text", "json"))

    return parser


def _db_path(args: argparse.Namespace, repo_root: Path) -> Path:
    return Path(args.db) if args.db else repo_root / store.db_path_for(args.profile)


def _force_utf8_output() -> None:
    """Windows の既定 cp932 では日本語 snippet や折り畳み記号が出力できない。

    リダイレクト時は `sys.stdout.encoding` がコンソールではなくロケール依存になるため、
    出力側で明示的に UTF-8 へ切り替える。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def main(argv: Sequence[str] | None = None) -> int:
    _force_utf8_output()
    args = build_parser().parse_args(argv)
    repo_root = Path(args.repo_root).resolve()
    result: dict[str, object] = {}
    started = time.perf_counter()
    try:
        if getattr(args, "profile", None) is None:
            args.profile = resolve_default_profile(
                repo_root, require_config=not getattr(args, "db", None)
            )
        code = _dispatch(args, repo_root, result)
    except (config.ConfigError, store.StoreError, search.SearchError,
            discovery.DiscoveryError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        result["error"] = type(exc).__name__
        code = 2
    _record_usage(args, repo_root, result, started, code)
    return code


def _record_usage(
    args: argparse.Namespace,
    repo_root: Path,
    result: dict[str, object],
    started: float,
    exit_code: int,
) -> None:
    """FR-CQ-14: 1 サブコマンド = 1 レコードを `.cq/usage.jsonl` へ追記する。"""
    # `watch` は長時間常駐するため記録対象外（FR-CQ-14）。
    if args.command == "watch":
        return
    usage_log.append_record(
        command=args.command,
        args={key: value for key, value in vars(args).items() if key != "command"},
        elapsed_ms=int((time.perf_counter() - started) * 1000),
        result=result,
        exit_code=exit_code,
        repo_root=repo_root,
    )


def _dispatch(
    args: argparse.Namespace, repo_root: Path, result: dict[str, object]
) -> int:
    if args.command == "index":
        profile = config.resolve_profile(repo_root, args.profile)
        report = indexer.build_index(
            repo_root, profile, db_path=_db_path(args, repo_root), rebuild=args.rebuild
        )
        payload = report.to_dict()
        if args.embed:
            from cq import embeddings, semantic_index

            try:
                provider = embeddings.get_provider()
            except embeddings.EmbeddingsUnavailable as exc:
                print(f"error: {exc}", file=sys.stderr)
                return 2
            payload["vectors"] = semantic_index.build(
                repo_root, profile.name, provider, db_path=_db_path(args, repo_root)
            )
        result.update(payload)
        _emit(payload)
        return 0

    if args.command == "stats":
        stats_report = store.index_stats(_db_path(args, repo_root))
        result.update(stats_report)
        _emit(stats_report)
        return 0

    if args.command == "search":
        hits = search.search(
            repo_root, args.profile, query=args.query, regex=args.regex, mode=args.mode,
            top_k=args.top_k, max_tokens=args.max_tokens,
            snippet_radius=args.snippet_radius,
            regex_max_candidates=args.regex_max_candidates,
            paths=args.paths, return_unit=args.return_unit,
            db_path=_db_path(args, repo_root),
            auto_reindex_limit=args.auto_reindex_limit,
            semantic=args.semantic,
        )
        for hit in hits:
            _emit_line(hit.to_dict())
        staleness = search.last_staleness()
        if staleness is not None:
            _emit_line(staleness)
        activity = search.last_activity()
        if args.explain and activity is not None:
            _emit_line(activity)
        result["hit_count"] = len(hits)
        return 0

    if args.command == "def":
        hits = search.search(
            repo_root, args.profile, query=args.symbol, mode="symbol",
            top_k=args.top_k, db_path=_db_path(args, repo_root),
        )
        for hit in hits:
            payload = hit.to_dict()
            payload.pop("snippet", None)
            _emit_line(payload)
        result["hit_count"] = len(hits)
        return 0

    if args.command == "get":
        chunk = get_chunk(_db_path(args, repo_root), args.chunk_id)
        if chunk is None:
            result["found"] = False
            print(f"error: unknown chunk id: {args.chunk_id}", file=sys.stderr)
            return 2
        start_line, end_line = chunk["lines"]
        print(f"# {chunk['path']}:{start_line}-{end_line}")
        print(chunk["text"])
        result["found"] = True
        result["body_chars"] = len(chunk["text"])
        return 0

    if args.command == "refs":
        rows = list(traces.references(
            _db_path(args, repo_root), args.symbol, top_k=args.top_k
        ))
        for row in rows:
            _emit_line(row)
        result["count"] = len(rows)
        return 0

    if args.command == "trace":
        if args.by_path:
            rows = list(traces.for_path(_db_path(args, repo_root), args.by_path))
            for row in rows:
                _emit_line(row)
            result["count"] = len(rows)
            return 0
        hits = search.search(
            repo_root, args.profile, query=args.trace_id, mode="trace",
            top_k=args.top_k, db_path=_db_path(args, repo_root),
        )
        for hit in hits:
            _emit_line(hit.to_dict())
        result["hit_count"] = len(hits)
        return 0

    if args.command == "watch":
        from cq.watcher import DEFAULT_DEBOUNCE_MS, CqWatcher

        profile = config.resolve_profile(repo_root, args.profile)
        watcher = CqWatcher(
            repo_root, profile, db_path=_db_path(args, repo_root),
            debounce_ms=args.debounce_ms or DEFAULT_DEBOUNCE_MS,
        )
        if not watcher.start():
            print("error: watching needs the optional 'watchdog' dependency", file=sys.stderr)
            return 2
        print(f"watching {args.profile} (Ctrl+C to stop)", file=sys.stderr)
        try:
            while True:
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        finally:
            watcher.stop()
        return 0

    if args.command == "map":
        from cq import repomap

        built = repomap.build(
            _db_path(args, repo_root),
            max_tokens=args.max_tokens or repomap.DEFAULT_MAX_TOKENS,
            paths=args.paths,
        )
        result["entries"] = len(built.entries)
        result["dropped"] = built.dropped
        result["tokens"] = built.tokens
        if args.format == "json":
            _emit(repomap.to_dict(built))
        else:
            print(repomap.render(built))
        return 0

    raise AssertionError(f"unhandled command: {args.command}")


def _emit(payload: object) -> None:
    # ensure_ascii=True: cp932 など UTF-8 以外のコンソールでも文字化けせずに読めるようにする。
    json.dump(payload, sys.stdout, ensure_ascii=True, indent=2)
    sys.stdout.write("\n")


def _emit_line(payload: object) -> None:
    json.dump(payload, sys.stdout, ensure_ascii=True)
    sys.stdout.write("\n")
