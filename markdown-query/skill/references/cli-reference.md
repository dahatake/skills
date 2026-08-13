# markdown-query CLI リファレンス

すべてローカル実行。`python -m mdq <subcommand>` 形式。

## 共通オプション
- `--db PATH`: SQLite 索引ファイルを明示指定。省略時は `--lang` / `--strategy` から導出される `.mdq/index-<lang>-<strategy>.sqlite`。
- `--lang {ja-jp|en-us}`: トークナイザ言語。既定 `ja-jp`。FTS5 トークナイザの選択と DB ファイル分離を同時に決定する。
- `--strategy {heading|heading_recursive|fixed_window}`: チャンキング戦略。`index` 既定 `heading`、`search` 既定 `auto`。`search` 以外で `auto` は指定不可。選択指針は [language-and-strategy.md](language-and-strategy.md) / [query-routing.md](query-routing.md) を参照。

> **重要**: `index` と `search` / `get` / `list` / `stats` / `watch` は **同一の `--lang` / `--strategy` を指定しないと別の DB ファイルを参照する**。`search --strategy auto` はクエリから戦略を選び、実在する DB へ自動フォールバックする。

## index — 索引作成 / 更新

```
python -m mdq index [--root PATH ...] [--rebuild] [--no-prune]
                    [--max-chunk-chars N] [--overlap-paragraphs N]
                    [--lang ja-jp|en-us]
                    [--strategy heading|heading_recursive|fixed_window|semantic_paragraph]
                    [--breakpoint-percentile-lo F] [--breakpoint-percentile-hi F]
                    [--min-chars N] [--embed-provider NAME] [--embed-model NAME]
                    [--no-semantic-contextualize] [--late-chunking]
```

- `--root`: 索引対象ルート（繰り返し指定可）。省略時は `mdq.toml` / `.mdq/config.toml` の `[index].roots` を参照し、なければ最小デフォルト（`mdq.config.GENERIC_DEFAULT_ROOTS` = `docs`, `users-guide`）。存在しないフォルダは自動スキップ。HVE リポジトリでの宣言例は repo-specific/hve-defaults.md（上流リポジトリ専用のため本キットには同梱していない） を参照。
- `--rebuild`: SHA-1 が同一でも強制再索引。
- `--no-prune`: 索引ストアに残っているがディスク上に存在しないファイルを削除しない。
- `--max-chunk-chars N`: 2 次分割閾値（文字数）。`heading` / `heading_recursive` の双方で機能し、`heading_recursive` 時は既定 `2000` を上書きする。`fixed_window` 戦略には影響しない。`semantic_paragraph` では MAX_CHARS（既定 `1000`）を上書き。`0` = オフ（既定、未分割）。
- `--overlap-paragraphs N`: `heading_recursive` 戦略専用。サブチャンク間で前から N 段落を重ねる（既定 `1`、`0` で無効化）。コードフェンスは overlap されない。
- `--breakpoint-percentile-lo F` / `--breakpoint-percentile-hi F`: `semantic_paragraph` 専用。Kamradt-modified バイナリサーチの探索区間（既定 `50` / `99`）。
- `--min-chars N`: `semantic_paragraph` 専用。最小チャンク文字数（既定 `200`、未満なら直前へ merge）。
- `--embed-provider NAME` / `--embed-model NAME`: `semantic_paragraph` 専用。埋め込み provider と model（既定 `fastembed` / `intfloat/multilingual-e5-large`、env override 可）。
- `--no-semantic-contextualize`: `semantic_paragraph` 専用。既定 ON のテンプレ contextualizer (`[Context] {path} > {heading_path}\n\n{body}`) を無効化。
- `--late-chunking`: `semantic_paragraph` 専用。最終 chunk 本文を再 embed して `chunk_embedding` 列へ float32 ベクトルを保存。検索時 `--fusion-alpha` で線形加重統合。

出力（JSON）: `{"files_indexed": N, "files_skipped": M, "chunks_written": K, "pruned_files": P, "pruned_chunks": Q, "roots": [...]}`

## search — 検索

```
python -m mdq search --q "..." [--lang ...] [--strategy auto|heading|heading_recursive|fixed_window] [options]
```

**`--strategy auto`（既定）**: クエリ内容から `mdq.query_router` が最適な戦略を選択し、該当 DB が存在しない場合は他の利用可能な戦略へフォールバックする。詳細は [query-routing.md](query-routing.md)。クエリ I/F を呼び出し側で分けないための統一エントリポイント。

| オプション | 既定 | 説明 |
|---|---|---|
| `--mode` | `bm25` | `bm25` または `grep`（正規表現エスケープした完全一致） |
| `--top-k` | `5` | 返却ヒット件数上限 |
| `--max-tokens` | `800` | 全 snippet 合計の概算トークン上限（超過時打ち切り） |
| `--paths` | なし | `fnmatch` 形式の path glob（例: `docs/*` `users-guide/**`） |
| `--tags` | なし | frontmatter `tags` で AND 絞り込み |
| `--snippet-radius` | `2` | マッチ行の前後何行を snippet に含めるか（`--return-unit line` のときのみ効く） |
| `--return-unit {line\|chunk\|locations}` | `line` | 抜粋の単位。`line` はマッチ行 ±`--snippet-radius` 行（最大 400 字で切り詰め）、`chunk` はヒットを含むチャンクの本文全体、`locations` は本文を一切返さず所在（`chunk_id` / `path` / `heading_path` / `lines` / `score`）だけを返す。単位を変えても strategy 選定・ヒット対象・順位は変わらないが、抜粋が長い分だけ同じ `--max-tokens` で返る件数は減る（逆に `locations` は増える）。`chunk` の拡張は `expansion` へ適用されないが、`locations` は `expansion` からも本文を除く。`semantic_paragraph` 索引では contextualize 後の本文（`[Context] ...` を前置した `text`）が返る |
| `--include-parent` | off | ヒットの直近親見出しチャンクを `expansion.parent` に追加（`--with-parent-depth 1` と等価） |
| `--with-parent-depth N` | `0` | N 階層上までの祖先見出しチェーンを取得。`expansion.parent` は **常に直近親 1 件の dict**（後方互換）、N≥2 のときのみ `expansion.parents` に祖先列（先頭=直近親、末尾=最上位先祖）を追加。`parent_chunk_id` 列を優先、未設定なら `heading_path` rsplit にフォールバック |
| `--expand-neighbors N` | `0` | 同一ファイル内で `start_line` 前後 N 件を `expansion.neighbors` に追加 |
| `--merge-parts` | off | 2 次分割で生じた同一見出しの他 part を `expansion.parts` に追加 |
| `--engine {auto\|bm25\|fts5}` | `auto` | `auto` は環境変数 `MDQ_FTS5`（旧名 `HVE_MDQ_FTS5` も deprecated alias）が truthy かつ FTS5 サポート時に FTS5、それ以外は in-memory BM25。詳細は [language-and-strategy.md](language-and-strategy.md) |
| `--fusion-alpha F` | なし | 未指定ではベクトル統合を行わない。`chunk_embedding` を持つ index (`--late-chunking`) かつ FTS5 経路でない場合のみ有効（`--engine fts5` では無視）。`final_score = alpha * bm25_norm + (1 - alpha) * cosine_sim`。`1.0` で BM25 単独、`0.0` で cosine 単独 |
| `--format` | `jsonl` | `jsonl` または `compact`（人間可読） |
| `--no-freshness-check` | off（検知は既定で有効） | 索引と作業ツリーの乖離検知を無効化する。検知は索引済みファイルのサイズと更新時刻だけを見る（内容ハッシュは読まない）。乖離を検知すると `{"warning":"stale","changed":N,"hint":"..."}` を **stderr** へ出力する。stdout の JSONL（1 ヒット 1 行）は変わらない |

JSONL 1 行スキーマ:
```json
{"chunk_id":"<sha1>","path":"...","heading_path":"...","lines":[start,end],"score":0.0,"snippet":"...","expansion":{"parent":{...},"neighbors":[...],"parts":[...]}}
```
`expansion` キーは関連オプションが指定され、かつ該当データが存在する場合のみ出力される（後方互換）。

## get — 単一チャンク取得

```
python -m mdq get --chunk-id <ID> [--lang ...] [--strategy ...]
```

`search` で返った `chunk_id` を渡すと、本文を含む完全なチャンクを返す。`--lang` / `--strategy` は `search` 時と同一を指定すること。

## list — 見出し一覧

```
python -m mdq list [--paths GLOB ...] [--heading-level N] [--limit 200]
```

ファイル / 見出し階層の俯瞰に使用。

## stats — 索引統計

```
python -m mdq stats
```

`{"files": N, "chunks": M}` を返す。

## watch — リアルタイム索引更新（スタンドアロン実行）

```
python -m mdq watch [--root PATH ...] [--debounce-ms 500]
                    [--burst-threshold 100] [--burst-window-s 1.0]
                    [--initial-index]
```

- `watchdog` で `.md` の追加 / 更新 / 削除を検知し、`.mdq/index-*.sqlite` を逐次更新する。
- HVE CLI Orchestrator は同名の `MdqWatcher` をデーモンスレッドとして内包しているため、通常は本コマンドを手動起動する必要はない（独立させたい場合 / 開発時の追跡用）。
- `--initial-index`: watch 開始前に `build_index` を 1 回実行。
- Ctrl+C で停止。Cloud Agent / GitHub Actions では使用しない（ファイルシステム揮発）。

## 終了コード
- `0`: 正常
- `1`: `get` で `chunk_id` が見つからない / `watch` 起動失敗 等
