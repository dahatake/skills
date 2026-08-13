# vendor/ — Vendored `mdq` package

This directory contains a **vendored copy** of the `mdq` Python package
(the index/search/CLI engine for the `markdown-query` Skill). When this
`<kit>/` directory is copied into another repository
that does not already have `mdq` installed, the launcher scripts
(`launch-gui.{cmd,ps1,sh}`) prepend `vendor/` to `sys.path` so that
`import mdq` resolves to this copy.

## Source of truth

- Upstream: `mdq/` at the root of the HVE source repository
  (https://github.com/dahatake/RoyalytyService2ndGen).

## Vendored modules (current snapshot)

| File | Purpose |
| --- | --- |
| `__init__.py` / `__main__.py` | Package entrypoints |
| `cli.py` | `python -m mdq` argparse entry. `--strategy auto` lives here. |
| `config.py` | Portable config loader (`mdq.toml` / `.mdq/config.toml` resolution, `GENERIC_DEFAULT_ROOTS`, `[index].tabular` globs). |
| `embeddings.py` | Embedding provider abstraction (fastembed / null). |
| `indexer.py` | File walker, chunk dataclass, `parent_chunk_id` assignment, `_subdivide` with `overlap_paragraphs`. Also tabular (CSV / TSV) row-level indexing (FR-MDQ-02). |
| `sentence_splitter.py` | Sentence splitter (nltk / regex fallback) for `semantic_paragraph`. |
| `strategies.py` | Strategy registry + per-strategy scanners. |
| `strategies_semantic.py` | `semantic_paragraph` implementation (embedding-based subdivision). |
| `strategies_pageindex.py` | `pageindex` implementation (heading tree + per-node summary). |
| `strategies_graphrag.py` | `graphrag` strategy adapter (LightRAG; SQLite-independent). |
| `graphrag_runtime.py` | LightRAG runtime wiring for the `graphrag` strategy. |
| `search.py` | BM25 / grep / FTS5 search, parent chain (`with_parent_depth`), pageindex `tree_path`, dedup. |
| `store.py` | SQLite schema (v6) and migrations. |
| `query_router.py` | **Skill-side auto strategy router** invoked when `--strategy auto`. Pure rule-based, no LLM. |
| `golden_eval.py` | Golden-query scoring (FR-MDQ-01). Line-range containment judgement shared by `benchmark.py`. |
| `tokenize.py` | FTS5 tokenizer resolver. |
| `usage_log.py` | JSONL append-only log. |
| `usage_stats.py` | 19-metric aggregation (H1/H2 cover routing). |
| `watcher.py` | watchdog-based realtime updater. |

> New upstream modules are picked up by the sync scripts automatically; this
> table is for humans. The machine check is `hve/tests/test_mdq_vendor_sync.py`.

## Re-syncing (when upgrading from upstream)

Inside the HVE source repository:

```powershell
pwsh -NoLogo -NoProfile -File <kit>/sync-vendor.ps1
```

```bash
bash <kit>/sync-vendor.sh
```

Both scripts copy `mdq/` and then drop what must not ship: `tests/`,
`__pycache__/` and `golden-queries.json` (the golden set pins paths and line
numbers of this repository only).

`vendor/mdq/` is tracked, so commit the result. `hve/tests/test_mdq_vendor_sync.py`
fails when a distributed file is missing, extra, or byte-different, so a
forgotten re-sync cannot ship.

## Do **not** edit files under `vendor/mdq/` directly

Bug fixes and features must be made in the upstream `mdq/` source first,
then synced down via the procedure above. Direct edits will be lost.
