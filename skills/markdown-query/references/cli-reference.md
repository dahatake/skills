# markdown-query CLI リファレンス

すべてローカル実行。`mdq <subcommand>` 形式（`python -m mdq <subcommand>` も可）。

## 共通オプション
- `--db PATH`: SQLite 索引ファイル。既定 `.mdq/index.sqlite`。

## index — 索引作成 / 更新

```
mdq index [--root PATH ...] [options]
```

| オプション | 既定 | 説明 |
|---|---|---|
| `--root` | カレントディレクトリ（再帰） | 索引対象ルート。繰り返し指定可。 |
| `--rebuild` | off | SHA-1 が同一でも強制再索引。 |
| `--no-prune` | off | ディスク上に存在しないファイルのチャンク削除を抑止。 |
| `--max-chunk-chars N` | `0`（無効） | 1 チャンクの上限文字数。超過時は段落／行境界で再分割（コードフェンスは分割しない）。 |

**索引対象拡張子**: `.md`, `.markdown`（固定）

**既定除外ディレクトリ**: `.git`, `node_modules`, `.venv`, `venv`, `__pycache__`, `.mdq`, `dist`, `build`, `.next`, `.cache`（再帰走査時に名前一致でプルーニング）

> 既定除外を上書きするフラグ（`--exclude` / `--no-default-excludes`）や `.gitignore` 尊重、シンボリックリンク追従は現状未実装です。除外を絞りたい場合は `--root` で対象ディレクトリを明示してください。

出力（JSON）: `{"files_indexed": N, "files_skipped": M, "chunks_written": K, "roots": [...]}`

## search — 検索

```
mdq search --q "..." [options]
```

| オプション | 既定 | 説明 |
|---|---|---|
| `--mode` | `bm25` | `bm25` または `grep`（正規表現エスケープした完全一致） |
| `--top-k` | `5` | 返却ヒット件数上限 |
| `--max-tokens` | `800` | 全 snippet 合計の概算トークン上限（超過時打ち切り） |
| `--paths` | なし | `fnmatch` 形式の path glob を複数指定可（例: `docs/**` `**/README.md`） |
| `--tags` | なし | frontmatter `tags` で AND 絞り込み |
| `--snippet-radius` | `2` | マッチ行の前後何行を snippet に含めるか |
| `--format` | `jsonl` | `jsonl` または `compact`（人間可読） |

JSONL 1行スキーマ:
```json
{"chunk_id":"<sha1>","path":"...","heading_path":"...","lines":[start,end],"score":0.0,"snippet":"..."}
```

## get — 単一チャンク取得

```
mdq get --chunk-id <ID>
```

`search` で返った `chunk_id` を渡すと、本文を含む完全なチャンクを返す。

## list — 見出し一覧

```
mdq list [--paths GLOB ...] [--heading-level N] [--limit 200]
```

ファイル / 見出し階層の俯瞰に使用。

## stats — 索引統計

```
mdq stats
```

`{"files": N, "chunks": M}` を返す。

## watch — リアルタイム索引（任意機能）

```
mdq watch [--root PATH ...] [--poll]
```

ファイルシステムイベントで `.md` / `.markdown` の追加・更新・削除を検知して索引を逐次更新する。`watchdog` の導入が必要（`pip install watchdog`）。`--poll` でイベント API を使わずポーリングフォールバックに切替可能。

## 終了コード
- `0`: 正常
- `1`: `get` で `chunk_id` が見つからない等の汎用エラー
- `2`: 索引未作成（`.mdq/index.sqlite` が存在しない状態で `search` / `get` / `list` / `stats` が呼ばれた）
