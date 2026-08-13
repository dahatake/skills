"""HVE Tool Search — 実行時統計の収集と集約（FR-TS-09）。

`ToolSearchContext.on_event` が発火するイベントを追記専用の JSONL へ流し、
ダッシュボード（FR-TS-10）が読む指標へ畳み込む。

**記録しないもの**: `additional_search_text`（検索専用語彙）、ツール定義本文、
プロンプト・会話内容。記録するのはクエリ文字列と発見結果の名前・スコアだけ。

収集は best-effort。書き込みや集計が失敗しても Step を落とさない
（`ToolSearchContext.emit` が例外を握り潰す層と二重に守る）。
"""

from __future__ import annotations

import json
import os
import threading
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .metatool import NO_DEFERRED_TOOLS_WARNING
from .usage import (
    DEFAULT_TOP_N,
    DEFAULT_WARMUP_SESSIONS,
    DEFAULT_WINDOW_SESSIONS,
    LOG_DIRNAME,
    UsageRecord,
    auto_pins,
)

SCHEMA_VERSION = 1

EVENT_CATALOG = "toolsearch.catalog"
EVENT_QUERY = "toolsearch.query"
EVENT_MISS = "toolsearch.miss"

RECORDED_EVENTS: frozenset[str] = frozenset((EVENT_CATALOG, EVENT_QUERY, EVENT_MISS))

# 上位一覧の既定件数。
DEFAULT_TOP = 10

# 1 セッションでメモリに保持するイベントの上限。
# これを超えた分は古い方から落とす（ファイルへは全件残る）。
MAX_LIVE_EVENTS = 5000


def default_events_path(repo_root: Path | str | None = None) -> Path:
    """既定はリポジトリスコープのイベントログ。``HVE_TOOLSEARCH_EVENTS`` で差し替えられる。"""
    override = os.environ.get("HVE_TOOLSEARCH_EVENTS")
    if override:
        return Path(override)
    base = Path(repo_root) if repo_root is not None else Path.cwd()
    return base / LOG_DIRNAME / "events.jsonl"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class StatsCollector:
    """`ToolSearchContext.on_event` としてそのまま渡せる収集シンク。

    呼び出しごとに 1 行を追記し、同時に軽量なインメモリ集計を更新する
    （実行中のプロセスが自分の統計を即座に読めるようにするため）。
    """

    def __init__(
        self,
        *,
        path: Path | str | None = None,
        run_id: str = "",
        workflow_id: str | None = None,
        step_id: str | None = None,
    ) -> None:
        self.path = Path(path) if path is not None else default_events_path()
        self.run_id = run_id
        self.workflow_id = workflow_id
        self.step_id = step_id
        self._lock = threading.Lock()
        self._events: deque[dict[str, Any]] = deque(maxlen=MAX_LIVE_EVENTS)

    def __call__(self, event: str, payload: Mapping[str, Any]) -> None:
        if event not in RECORDED_EVENTS:
            return
        record: dict[str, Any] = {
            "ts": _utc_now_iso(),
            "schema_version": SCHEMA_VERSION,
            "kind": event,
            "run_id": self.run_id,
        }
        record.update(payload)
        record.setdefault("workflow_id", self.workflow_id)
        record.setdefault("step_id", self.step_id)
        with self._lock:
            self._events.append(record)
        self._append(record)

    def _append(self, record: Mapping[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except (OSError, TypeError, ValueError):
            # 収集の失敗で Step を落とさない。
            pass

    def snapshot(self, usage_records: Sequence[UsageRecord] = ()) -> "DashboardSnapshot":
        """このプロセスが記録した分だけを集計する（ファイルを読み直さない）。"""
        with self._lock:
            events = tuple(self._events)
        return aggregate(events, usage_records=usage_records)


def load_events(path: Path | str | None = None) -> tuple[dict[str, Any], ...]:
    """イベントログを読み込む。壊れた行は黙って捨てる（収集は best-effort）。"""
    target = Path(path) if path is not None else default_events_path()
    try:
        text = target.read_text(encoding="utf-8")
    except OSError:
        return ()
    events: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(raw, dict) and raw.get("kind") in RECORDED_EVENTS:
            events.append(raw)
    return tuple(events)


# ---------------------------------------------------------------------------
# 集約
# ---------------------------------------------------------------------------


def _percentile(values: Sequence[float], pct: float) -> float | None:
    """昇順ソート + 線形補間。値なしなら None（0 で埋めない）。"""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return float(ordered[0])
    position = (len(ordered) - 1) * pct
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    if low == high:
        return float(ordered[low])
    return float(ordered[low] + (ordered[high] - ordered[low]) * (position - low))


@dataclass(frozen=True)
class CatalogShape:
    """直近のカタログ構成。イベントが 1 件も無ければ ``total`` は 0 のままとなる。"""

    total: int = 0
    pinned: int = 0
    searchable: int = 0
    dropped: int = 0
    deferred: int = 0
    mcp: int = 0
    native: int = 0
    skill: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "total": self.total,
            "pinned": self.pinned,
            "searchable": self.searchable,
            "dropped": self.dropped,
            "deferred": self.deferred,
            "mcp": self.mcp,
            "native": self.native,
            "skill": self.skill,
        }


@dataclass(frozen=True)
class AutoPinProgress:
    """FR-TS-07 のウォームアップ進捗（workflow × step 単位）。"""

    scope: str
    sessions: int
    warmup_sessions: int
    promoted: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "sessions": self.sessions,
            "warmup_sessions": self.warmup_sessions,
            "promoted": list(self.promoted),
        }


@dataclass(frozen=True)
class DashboardSnapshot:
    """ダッシュボードが描画する全指標。

    算出不能な指標は ``None`` のままにする（0 や推定値で埋めない）。
    """

    generated_at: str = ""
    first_event_at: str | None = None
    last_event_at: str | None = None

    queries: int = 0
    misses: int = 0
    hit_rate: float | None = None
    sessions: int = 0
    runs: int = 0

    avg_hits: float | None = None
    avg_top_score: float | None = None

    latency_p50_ms: float | None = None
    latency_p95_ms: float | None = None
    latency_max_ms: float | None = None

    catalog: CatalogShape = field(default_factory=CatalogShape)
    deferral_inactive_rate: float | None = None

    baseline_tokens: int | None = None
    exposed_tokens: int | None = None
    token_reduction: float | None = None

    adoption_rate: float | None = None
    never_hit_tools: tuple[str, ...] = ()

    top_queries: tuple[tuple[str, int], ...] = ()
    top_hit_tools: tuple[tuple[str, int], ...] = ()
    top_miss_queries: tuple[tuple[str, int], ...] = ()
    top_called_tools: tuple[tuple[str, int], ...] = ()
    queries_by_scope: tuple[tuple[str, int], ...] = ()
    autopin_progress: tuple[AutoPinProgress, ...] = ()
    warnings: tuple[tuple[str, int], ...] = ()

    @property
    def token_reduction_valid(self) -> bool:
        """遅延公開が一度も発火していないとき、削減率は成立しない。"""
        return self.deferral_inactive_rate is None or self.deferral_inactive_rate < 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "first_event_at": self.first_event_at,
            "last_event_at": self.last_event_at,
            "queries": self.queries,
            "misses": self.misses,
            "hit_rate": self.hit_rate,
            "sessions": self.sessions,
            "runs": self.runs,
            "avg_hits": self.avg_hits,
            "avg_top_score": self.avg_top_score,
            "latency_p50_ms": self.latency_p50_ms,
            "latency_p95_ms": self.latency_p95_ms,
            "latency_max_ms": self.latency_max_ms,
            "catalog": self.catalog.to_dict(),
            "deferral_inactive_rate": self.deferral_inactive_rate,
            "baseline_tokens": self.baseline_tokens,
            "exposed_tokens": self.exposed_tokens,
            "token_reduction": self.token_reduction,
            "token_reduction_valid": self.token_reduction_valid,
            "adoption_rate": self.adoption_rate,
            "never_hit_tools": list(self.never_hit_tools),
            "top_queries": [list(item) for item in self.top_queries],
            "top_hit_tools": [list(item) for item in self.top_hit_tools],
            "top_miss_queries": [list(item) for item in self.top_miss_queries],
            "top_called_tools": [list(item) for item in self.top_called_tools],
            "queries_by_scope": [list(item) for item in self.queries_by_scope],
            "autopin_progress": [item.to_dict() for item in self.autopin_progress],
            "warnings": [list(item) for item in self.warnings],
        }


def _ranked(counter: Counter, top: int) -> tuple[tuple[str, int], ...]:
    """件数降順 → キー昇順で決定的に並べる。"""
    return tuple(sorted(counter.items(), key=lambda item: (-item[1], item[0]))[:top])


def _mean(values: Sequence[float]) -> float | None:
    return sum(values) / len(values) if values else None


def aggregate(
    events: Sequence[Mapping[str, Any]],
    *,
    usage_records: Sequence[UsageRecord] = (),
    since: str | None = None,
    top: int = DEFAULT_TOP,
    warmup_sessions: int = DEFAULT_WARMUP_SESSIONS,
) -> DashboardSnapshot:
    """イベントと利用履歴だけから指標を算出する（決定的・ネットワーク不使用）。

    ``since`` は ISO8601 文字列の辞書順比較で絞り込む（すべて UTC の ``Z`` 表記のため）。
    """
    if since:
        events = [e for e in events if str(e.get("ts", "")) >= since]

    queries = [e for e in events if e.get("kind") == EVENT_QUERY]
    misses = [e for e in events if e.get("kind") == EVENT_MISS]
    catalogs = [e for e in events if e.get("kind") == EVENT_CATALOG]

    timestamps = sorted(str(e.get("ts", "")) for e in events if e.get("ts"))

    latencies = [float(e["latency_ms"]) for e in queries if isinstance(e.get("latency_ms"), (int, float))]
    hit_counts = [len(e.get("hits") or ()) for e in queries]
    top_scores = [
        float(max(e["scores"])) for e in queries if isinstance(e.get("scores"), list) and e["scores"]
    ]

    query_counter: Counter = Counter()
    hit_counter: Counter = Counter()
    scope_counter: Counter = Counter()
    warning_counter: Counter = Counter()
    inactive = 0
    for event in queries:
        if event.get("query"):
            query_counter[str(event["query"])] += 1
        for name in event.get("hits") or ():
            hit_counter[str(name)] += 1
        workflow_id = event.get("workflow_id")
        step_id = event.get("step_id")
        if workflow_id and step_id:
            scope_counter[f"{workflow_id}:{step_id}"] += 1
        event_warnings = event.get("warnings") or ()
        for warning in event_warnings:
            warning_counter[str(warning)] += 1
            # FR-TS-08 の警告だけを「不活性」と数える。
            # カタログ未取得等の別の警告を混ぜると指標の意味が壊れる。
            if str(warning) == NO_DEFERRED_TOOLS_WARNING:
                inactive += 1

    miss_counter: Counter = Counter(str(e.get("query", "")) for e in misses if e.get("query"))

    catalog = CatalogShape()
    for event in catalogs + queries:
        raw = event.get("catalog")
        if isinstance(raw, Mapping):
            catalog = CatalogShape(
                total=int(raw.get("total", 0)),
                pinned=int(raw.get("pinned", 0)),
                searchable=int(raw.get("searchable", 0)),
                dropped=int(raw.get("dropped", 0)),
                deferred=int(raw.get("deferred", 0)),
                mcp=int(raw.get("mcp", 0)),
                native=int(raw.get("native", 0)),
                skill=int(raw.get("skill", 0)),
            )

    baseline_tokens: int | None = None
    exposed_tokens: int | None = None
    for event in queries:
        tokens = event.get("tokens")
        if isinstance(tokens, Mapping) and tokens.get("baseline"):
            baseline_tokens = int(tokens["baseline"])
            exposed_tokens = int(tokens.get("exposed", 0))
    token_reduction = (
        1.0 - (exposed_tokens / baseline_tokens)
        if baseline_tokens and exposed_tokens is not None
        else None
    )

    # カタログ全体の名前は `toolsearch.catalog` イベントにしか無い。
    # 無いときは「一度も出ていないツール」も「採用率」も算出できない（推測しない）。
    known_names: dict[str, str] = {}
    for event in catalogs:
        names = event.get("names")
        if isinstance(names, Mapping):
            known_names.update({str(k): str(v) for k, v in names.items()})

    never_hit: tuple[str, ...] = ()
    adoption_rate: float | None = None
    if known_names:
        never_hit = tuple(sorted(n for n in known_names.values() if n not in hit_counter))
        # 窓を切ったら「呼ばれた側」も同じ窓で見る。時刻を持たない旧レコードは
        # 窓の内側だと証明できないので数えない（推測で採用率を上げない）。
        scoped_usage = (
            [r for r in usage_records if r.ts and r.ts >= since] if since else list(usage_records)
        )
        called_ids = {r.tool_id for r in scoped_usage}
        surfaced_ids = {
            entry_id for entry_id, name in known_names.items() if name in hit_counter
        }
        if surfaced_ids:
            adoption_rate = len(surfaced_ids & called_ids) / len(surfaced_ids)

    autopin: list[AutoPinProgress] = []
    scopes = sorted({(r.workflow_id, r.step_id) for r in usage_records})
    for workflow_id, step_id in scopes:
        scoped = [r for r in usage_records if r.workflow_id == workflow_id and r.step_id == step_id]
        autopin.append(
            AutoPinProgress(
                scope=f"{workflow_id}:{step_id}",
                sessions=len({r.session_id for r in scoped}),
                warmup_sessions=warmup_sessions,
                promoted=auto_pins(
                    usage_records,
                    workflow_id,
                    step_id,
                    warmup_sessions=warmup_sessions,
                    top_n=DEFAULT_TOP_N,
                    window_sessions=DEFAULT_WINDOW_SESSIONS,
                ),
            )
        )

    scoped_events = [e for e in events if e.get("run_id") or e.get("step_id")]
    return DashboardSnapshot(
        generated_at=_utc_now_iso(),
        first_event_at=timestamps[0] if timestamps else None,
        last_event_at=timestamps[-1] if timestamps else None,
        queries=len(queries),
        misses=len(misses),
        hit_rate=(1.0 - len(misses) / len(queries)) if queries else None,
        sessions=len({f"{e.get('run_id')}:{e.get('step_id')}" for e in scoped_events}),
        runs=len({str(e.get("run_id", "")) for e in events if e.get("run_id")}),
        avg_hits=_mean(hit_counts),
        avg_top_score=_mean(top_scores),
        latency_p50_ms=_percentile(latencies, 0.5),
        latency_p95_ms=_percentile(latencies, 0.95),
        latency_max_ms=max(latencies) if latencies else None,
        catalog=catalog,
        deferral_inactive_rate=(inactive / len(queries)) if queries else None,
        baseline_tokens=baseline_tokens,
        exposed_tokens=exposed_tokens,
        token_reduction=token_reduction,
        adoption_rate=adoption_rate,
        never_hit_tools=never_hit,
        top_queries=_ranked(query_counter, top),
        top_hit_tools=_ranked(hit_counter, top),
        top_miss_queries=_ranked(miss_counter, top),
        top_called_tools=_ranked(Counter(r.tool_id for r in usage_records), top),
        queries_by_scope=_ranked(scope_counter, top),
        autopin_progress=tuple(autopin),
        warnings=_ranked(warning_counter, top),
    )
