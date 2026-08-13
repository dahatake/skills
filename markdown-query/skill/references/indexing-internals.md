# 索引内部仕様

本書は実装（`mdq/indexer.py` / `mdq/store.py` / `mdq/strategies.py` / `mdq/watcher.py`）と一致させた内部仕様。言語・戦略の利用者向け選択指針は [language-and-strategy.md](language-and-strategy.md) を参照。

## ストア
- SQLite（stdlib `sqlite3`）
- DB パス: `.mdq/index-<lang>-<strategy>.sqlite`（`mdq/store.py::db_path_for`）。`--db` 明示時はそのパスを使用。
- テーブル:
  - `files`(path PK, sha1, mtime, size_bytes, frontmatter JSON)
  - `chunks`(chunk_id PK, path, heading_path, level, start_line, end_line, token_est, text, tags JSON, part_index, part_total)
  - `chunks_fts`（FTS5 ミラー、`content='chunks'` の external content / 同期トリガ 3 種）— FTS5 利用不可な SQLite ビルドでは作成スキップ
    - `ja-jp`（`trigram`）は **`detail=none`** で作成する。位置情報を持たない分だけ索引が小さい（実測: FTS5 領域 139.6MB → 8.7MB）。代償としてフレーズクエリが使えない
    - `en-us`（`unicode61`）は `detail` 未指定（既定）のまま
- `PRAGMA user_version` でスキーマ世代を管理。**現行 `SCHEMA_VERSION = 7`**（`mdq/store.py`）。
  - v1: `part_index` / `part_total` カラム導入
  - v2: `chunk_id` 由来式変更（`start_line` → `occurrence_index`、行シフト耐性）。旧 v1 行は削除し再構築誘導
  - v3: `chunks_fts` ミラー + トリガ追加（best-effort、未対応ビルドは BM25 フォールバック）
  - v4: `parent_chunk_id` カラム追加（NULL 可）
  - v5: `text_raw` / `chunk_embedding` カラム追加（NULL 可、`semantic_paragraph` 用）
  - v6: `summary` カラム追加（NULL 可、`pageindex` 用）
  - v7: `trigram` ミラーを `detail=none` で作り直す。旧ミラーは DROP して rebuild される
- 旧スキーマ DB は `open_store()` 時に `PRAGMA table_info(chunks)` を確認し、不足カラムを `ALTER TABLE ... ADD COLUMN` で追加（軽量マイグレーション）。
- FTS5 ミラーの再作成判定は仮想表の引数列全体（`tokenize` と `detail`）を比較する。トークナイザ名だけを見ると `detail` 変更を見逃す。
- **既存 DB は `VACUUM` されない**ため、v6 からの移行後もファイルサイズはそのまま残る（解放ページは以後再利用される）。縮めたい場合は `mdq index --rebuild` か `VACUUM` を 1 度実行する。

## ファイル検知
### バッチ走査（`mdq index`）
- `iter_markdown(root, roots)` が各 root を `Path.rglob("*.md")` で再帰列挙（`mdq/indexer.py`）。
- 増分判定: ファイル単位の SHA-1 + mtime 一致でスキップ。
- prune: ディスクから消えたファイルに対応する `chunks` 行を削除（`--no-prune` で無効化）。

### リアルタイム検知（`mdq watch` / HVE 同梱 `MdqWatcher`）
- 実装: `mdq/watcher.py::MdqWatcher`、`watchdog.observers.Observer` を使用し各 root を `recursive=True` で監視。
- フィルタ: `Path.suffix.lower() == ".md"`。
- デバウンス: 既定 500ms、同一ファイルの連続イベントを最後の状態に集約。
- バースト検知: `burst_window_s` 秒間に `burst_threshold` 件超のイベントが発生した場合、全 root の `build_index` フォールバックへ切替。
- watchdog 未導入時: `start()` が `False` を返し CLI は通常継続（ハードフェイル禁止）。
- 対象外環境: GitHub Actions / Copilot Cloud Agent（ファイルシステム揮発のため）— セッション毎に `mdq index` を明示実行する運用。

## チャンク化（3 戦略）
戦略は `mdq/strategies.py::scan_file_for_strategy` で分岐。索引 DB は **戦略ごとに別ファイル** として作成されるため、同一クエリ内では 1 戦略のみが評価対象。

### `heading`（既定）
- Markdown を **見出し単位** で分割（H1〜H6）。
- 各チャンクは「見出し行 → 次の同等以上レベル見出し直前」までの本文。
- 先頭見出し前の本文は `heading_path="(preface)"` で個別チャンク化。
- フェンスドコードブロック（` ``` ` または `~~~`）内の `#` は見出しとして扱わない（`mdq/indexer.py::_segment_by_fence`）。

### `heading_recursive`
- `heading` 分割後、本文長が `HEADING_RECURSIVE_MAX_CHARS = 2000` 文字を超えるチャンクのみ `_subdivide` で再分割。
- 再分割アルゴリズム:
  1. `_segment_by_fence` でフェンス領域とテキスト領域に分解。**フェンスは不可分**。
  2. テキスト領域は空行で段落分割し、予算内にまとめてサブチャンク化。
  3. 1 段落が予算超過なら改行単位、1 行が予算超過なら文字数ハードカット。
- 派生サブチャンクは `heading_path` / `level` を共有し、`part_index`（0 始まり）/ `part_total` のみ異なる。
- `start_line` / `end_line` はサブチャンクごとに実際の行範囲に再計算。

### `fixed_window`
- 見出し構造を完全に無視。本文（frontmatter 除去後）を `FIXED_WINDOW_CHARS = 1000` / overlap `FIXED_WINDOW_OVERLAP = 200` のスライディングウィンドウで分割。
- `heading_path = "(window)"`、`level = 0` 固定。
- 文字オフセット → 行番号変換は cumulative line-length テーブルによる線形走査（`mdq/strategies.py::_scan_fixed_window`）。

## frontmatter
- ファイル先頭の `---` ブロックを PyYAML で解析（プロジェクト依存）。
- `tags`（list または str）をチャンクの `tags` にも複製し、`--tags` フィルタの照合対象とする。

## chunk_id（行シフト耐性）
- 実装: `mdq/indexer.py::Chunk.chunk_id` プロパティ
- 式: `chunk_id = SHA1(path \0 heading_path \0 occurrence_index \0 part_index)`
  - `occurrence_index`: 同一 `heading_path` がファイル内で何回目に出現したか（0 始まり）。`index_one_file` 内で重複防止のため決定的に割当。
  - `part_index`: 2 次分割で生じたサブチャンクの 0 始まり順序。
- **行番号には依存しない**ため、ファイル冒頭への追記等で `start_line` がずれても `chunk_id` は安定。

## 言語とトークナイザ
言語ごとの DB 分離と FTS5 トークナイザ選択の詳細は [language-and-strategy.md](language-and-strategy.md) を参照。本書では実装位置のみ記載:

- 言語正規化: `mdq/tokenize.py::normalize`（`ja-jp` / `en-us`、既定 `ja-jp`）
- FTS5 トークナイザ解決: `mdq/tokenize.py::resolved_fts5_tokenizer`（`ja-jp` → `trigram`、未対応時 `unicode61`、`en-us` → `unicode61`）
- BM25 スコアリングのトークナイザ: `mdq/tokenize.py::scoring_terms`（CJK 連続部は隣接バイグラムへ分解、隣に CJK が無い 1 文字はそのまま、ASCII 連続部は小文字化するだけで分割しない。形態素解析なし）
- BM25 のスコアリング対象テキスト: `mdq/search.py::_scoring_text` が `path` + `heading_path`（`HEADING_WEIGHT = 3` 回繰り返す）+ 本文を連結する。見出しやパスに現れる語は本文だけに現れる語より順位が上がる
- 抜粋（snippet）の中心行選択にだけ使う正規表現トークナイザ: `mdq/search.py::_TOKEN_RE = [A-Za-z0-9_]+ | [\u3040-\u30ff\u4e00-\u9fff]`（CJK は 1 文字 1 トークン）。順位付けには関与しない

## 検索エンジン
- `--engine auto`（既定）: 環境変数 `MDQ_FTS5`（旧名 `HVE_MDQ_FTS5` も deprecated alias）が truthy（`1`/`true`/`yes`/`on`）かつ DB が FTS5 をサポートする場合に FTS5、それ以外は in-memory BM25。
- `--engine bm25`: 強制 in-memory BM25。
- `--engine fts5`: 強制 FTS5（未対応時は silent fallback で BM25）。
- BM25 ライブラリ: `rank_bm25` が import 可能なら使用、未導入なら同等の `_MiniBM25`（stdlib のみ）にフォールバック。
- スコア > 0 のチャンクのみ返却。`grep` モードは `re.escape` した完全一致の出現数をスコアとする。
- 小コーパス特性: BM25-Okapi の IDF は `df ≈ N/2` で 0 に近づくため、頻出語のみのクエリはヒットしないことがある。

## snippet と expansion
- snippet: マッチした query token を最も多く含む 1 行を中心に `--snippet-radius` 行を抽出（`mdq/search.py::_make_snippet`、`max_chars=400` で末尾切り詰め）。`--return-unit locations` のときは `snippet` 自体を生成せず、JSON 出力からもキーごと落ちる。
- `Hit.expansion`（任意フィールド）: `--include-parent` / `--expand-neighbors N` / `--merge-parts` 指定時のみ付与される dict。
  - `parent`: 親見出しチャンク（`heading_path` から末尾セグメントを除いたパスに一致するチャンク）
  - `neighbors`: 同一ファイル内で `start_line` が直前/直後の N 件
  - `parts`: 同一 `(path, heading_path)` の他 part（`part_total > 1` の場合のみ）
- いずれも JSON 出力では `{chunk_id, path, heading_path, lines, text}` の最小ブリーフ表現。ただし `--return-unit locations` のときは `text` を除いた所在情報のみとなる。

## 既知の制約（捏造禁止）
- BM25 in-memory 経路はクエリ時にチャンク全件をロードする（小〜中規模向け）。大規模化時は `--engine fts5` を検討。
- 日本語は FTS5 では trigram、BM25 では隣接バイグラム単位で扱い、形態素解析は行わない。固有表現の精度は限定的。
- `expansion` の snippet 長は `--max-tokens` の予算には算入されない（本体ヒットのみ予算対象）。
- 索引 DB は言語 × 戦略の組み合わせごとに別ファイルとして作成される。複数組み合わせを使う場合はそれぞれ `mdq index` の実行が必要。
- 索引と作業ツリーの乖離は `mdq search` が検知して stderr へ警告を出すが、**自動で再索引はしない**（再索引は検索レイテンシと桁が違うため）。検知はサイズと更新時刻だけを見るので、内容を戻す編集は見逃すことがある。
