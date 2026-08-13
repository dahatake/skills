# 言語とチャンキング戦略の選択ガイド

`mdq` は **言語（`--lang`）** と **チャンキング戦略（`--strategy`）** の組合せごとに別 DB ファイル `.mdq/index-<lang>-<strategy>.sqlite` を作成する。本書はそれぞれの選び方と実装上の挙動を説明する。

---

## 1. 言語選択（`--lang`）

実装位置: `mdq/tokenize.py`

| 値 | 既定 | FTS5 トークナイザ | フォールバック | 想定対象 |
|---|---|---|---|---|
| `ja-jp` | ✅ | `trigram`（SQLite 3.34+、`detail=none`） | 未対応ビルドでは `unicode61` に silent fallback | 日本語（ひらがな・カタカナ・漢字）を含む文書 |
| `en-us` | | `unicode61`（`detail` 既定） | なし | 英語のみ、または ASCII 中心の文書 |

### 挙動の要点
- **`--lang` が実効的に検索挙動を変えるのは FTS5 利用時のみ**。in-memory BM25 経路（既定 `--engine auto` で FTS5 未起動時）のスコアリングは言語を問わず `mdq/tokenize.py::scoring_terms` で行う（CJK 連続部は **隣接バイグラム**、ASCII 連続部は非分割、形態素解析は行わない）。
- BM25 モードでの `--lang` の役割は実質的に **DB ファイル名の分離のみ**（`.mdq/index-<lang>-<strategy>.sqlite`）。
- FTS5 利用時は `ja-jp` で `trigram` が選択される。`detail=none` のためフレーズクエリは使えないので、`mdq/search.py::_build_trigram_query` がクエリの各セグメントをトリグラムへ分解して AND 結合し、SQL の `LIKE` で確定照合する（トリグラム AND だけでは非連続に含む文書が偽陽性として残るため）。
- **3 文字未満のセグメントは trigram 索引で表現できない**。全セグメントが 3 文字未満の場合は FTS5 を断念して in-memory BM25 へフォールバックする（0 件を返さない）。
- **長いセグメントと短いセグメントが混在する場合、短い方は条件から落ちる**。例: `検証 マーカー書式` → `検証` は無視され `マーカー書式` だけが条件になる。短い語を必須にしたい場合は `--engine bm25` を使う。
- **セグメントは連続一致を要求する**。空白を含まない日本語クエリは全体がそのまま現れる文書しかヒットしない。in-memory BM25（CJK を隣接バイグラムへ分解して OR 相当）より **precision は高く recall は低い**。幅広く拾いたい場合は語を空白で区切るか `--engine bm25` を使う。
- 確定照合の `LIKE` は `%` / `_` をエスケープしており、クエリ中のこれらの文字はワイルドカードとして扱わない（literal として一致させる）。
- `en-us` の `unicode61` は設計上 CJK 連続部を分割しない（SQLite FTS5 公式仕様）。こちらは従来どおりトークンを OR 結合する。
- `--lang ja-jp` の DB に英語文書を索引することも可能（逆も同じ）。FTS5 トークナイザが言語別に最適化されているため、対象文書の主言語に合わせるのが推奨。

### 言語混在ドキュメントの扱い
- 主言語に合わせて 1 つを選ぶ（既定 `ja-jp` で日本語混在の文書も検索可能）。
- 厳密に言語別検索を分けたい場合は **両方の DB を作る**: `mdq index --lang ja-jp` と `mdq index --lang en-us` を別々に実行する。

---

## 2. チャンキング戦略選択（`--strategy`）

実装位置: `mdq/strategies.py`

| 戦略 | 既定 | チャンク境界 | 1 チャンクサイズ | 推奨ユースケース |
|---|---|---|---|---|
| `heading` | ✅ (index) | 見出し H1〜H6 | 文書ごとに変動 | 構造化された設計文書・仕様書。Context 効率最優先 |
| `heading_recursive` | | 見出し → 2000 文字超は段落 / 行 / 文字で再分割。**段落単位 overlap 対応**（既定 1 段落） | ≤ 2000 文字 + overlap | 長文の章を含む文書。チャンクサイズ上限と前後文脈の両立 |
| `fixed_window` | | 見出し無視のスライディングウィンドウ（1000 chars / overlap 200 文字） | 最大 1000 文字（末尾ウィンドウは可変） | RAG・順序保証重視。見出しが少ない / 整っていない文書 |
| `semantic_paragraph` | | 見出し（hard boundary）→ 文単位 embedding 類似度の breakpoint で再分割。Kamradt-modified バイナリサーチで MAX_CHARS を満たす最小パーセンタイル閾値を採用。コンテキスト化テンプレを既定で prepend | 200〜1000 文字（CLI で上書き可） | 設計書 NL 質問・長文章節をまたぐ意味的検索。`[semantic]` extra が必要 |
| `pageindex` | | 見出し H1～H6（`heading` と同境界）+ ノードごとの `summary` カラムを生成。LLM 不要。SCHEMA v6 `chunks.summary` に保存 | 文書ごとに変動（`heading` と同じ） | PageIndex 型の目次的ナビゲーション。`search --pageindex-tree-depth N` で `expansion.tree_path` にルート→ヒットの summary 連鎖を返す |
| `auto` | ✅ (search) | （戦略名ではない）クエリ内容から上記 5 つのいずれかを選択 | 採用戦略に依存 | **既定の `search` 挙動**。クエリ I/F を統一したい場合 |
| `graphrag` | | **別系統**: LightRAG が文書を chunk → entity / relation 抽出 → knowledge graph 構築（LLM 必須） | mdq の `GraphRAGConfig` 既定 1200 / 100（`chunk_token_size` / `chunk_overlap_token_size`、LightRAG default と同値だが `mdq/strategies_graphrag.py` が `LightRAG()` 構築時に明示再指定） | エンティティ関係を辿る問いに対する LLM 生成回答（任意・LLM コスト発生） |

> ⚠️ `graphrag` は SQLite 索引（`.mdq/index-<lang>-<strategy>.sqlite`）を使わず、**LightRAG 専用の working_dir**（既定 `.mdq/graphrag-<lang>/`）にグラフと埋め込みを保存する。citation（`chunk_id` / `path` / `lines`）を返さず、文字列の回答を返す。`auto` の候補にも含まれず、`--strategy graphrag` を明示した時のみ起動する。詳細は [graphrag-strategy.md](graphrag-strategy.md)。

### 共通仕様
- フェンスドコードブロック（`` ``` `` / `~~~`）は **不可分**（`heading` / `heading_recursive` の双方で保護される）。
- `chunk_id` は `SHA1(path \0 heading_path \0 occurrence_index \0 part_index)` で計算され、**行番号シフトに対して安定**（ファイル先頭追記等で ID が変わらない）。
- `fixed_window` は `heading_path="(window)"` / `level=0` 固定。

### サイズ調整パラメータ
- `--strategy heading_recursive --max-chunk-chars N` で 2 次分割閾値を上書き（既定 2000）。
- `--strategy heading_recursive --overlap-paragraphs N` で隣接サブチャンクへ前から N 段落を引き継ぐ（既定 1。0 で無効化、最大 5 を想定）。コードフェンスを跨ぐ overlap は禁止（Recall を上げつつ BM25 スコアの過大評価を抑制）。
- `fixed_window` のウィンドウ幅 / overlap は実装定数（`FIXED_WINDOW_CHARS=1000` / `FIXED_WINDOW_OVERLAP=200`）。CLI からは変更不可（変更が必要な場合は `mdq/strategies.py` を編集）。
- `pageindex` は `--pageindex-summary-chars N`（既定 200）と `--pageindex-summary-mode head|first_paragraph`（既定 `head`）でノードサマリの長さと抽出方式を調整。検索時は `--pageindex-tree-depth N` で `expansion.tree_path` としてルート→ヒットの summary 連鎖を返す（0 で無効）。

### Parent / 階層展開
- 各チャンクは索引時に `parent_chunk_id` 列（SCHEMA v4）に **直近上位見出しチャンクの chunk_id** を持つ（top-level / preface / fixed_window では NULL）。
- 検索時 `--include-parent`（深さ 1） / `--with-parent-depth N`（N ≥ 1）で親チェーンを `expansion.parent` に返す。
- `--with-parent-depth 1` は単一 dict、`>=2` は root 方向への dict 配列（先頭=直近親、末尾=最上位先祖）。

### 戦略選択のフローチャート
1. 既定では **クエリ I/F は `--strategy auto`** を用い、Skill 側 `mdq.query_router` がクエリ内容から自動選択する（[query-routing.md](query-routing.md)）。
2. 手動指定したい場合:
   - 文書が見出しで適切に分割されているか？
     - **Yes** → `heading`
     - **No** → `fixed_window`
   - `heading` で 1 つの章が 2000 文字を大きく超える場合があるか？
     - **Yes** → `heading_recursive`（必要に応じ `--overlap-paragraphs` を増やす）
   - 「概要」「アーキテクチャ」系の目次ナビゲーションを重視したいか？
     - **Yes** → `pageindex`（`--pageindex-tree-depth N` で `expansion.tree_path` を返す）

---

## 3. 検索エンジン選択（`--engine`）

実装位置: `mdq/search.py` / `mdq/store.py`

| 値 | 既定 | 挙動 |
|---|---|---|
| `auto` | ✅ | 環境変数 `MDQ_FTS5`（旧名 `HVE_MDQ_FTS5` も deprecated alias として有効）が `1`/`true`/`yes`/`on` のいずれかで、かつ DB が FTS5 をサポートしている場合のみ FTS5。それ以外は in-memory BM25 |
| `bm25` | | 強制 in-memory BM25（`rank_bm25` 未導入時は同等の `_MiniBM25` フォールバック） |
| `fts5` | | 強制 FTS5（未対応 DB / ビルド、またはクエリが trigram 索引で表現できない場合は silent fallback で BM25） |

### in-memory BM25 vs FTS5 のトレードオフ
- **in-memory BM25**（既定）: クエリ時に全チャンクをメモリへロード。小〜中規模コーパスで十分実用。stdlib のみで動作する。CJK は隣接バイグラムへ分解するため 2 文字単位の連続性までは見るが、それより長い語の連続性は見ない。
- **FTS5**: SQLite の全文検索インデックスを使うため大規模コーパスで高速。`bm25(chunks_fts)` ランキングを使用。`chunks_fts` ミラーは `chunks` テーブルへのトリガで同期されるため `mdq index` 実行時に自動構築される（SCHEMA v3 以降）。日本語では `LIKE` の確定照合を伴うため、語の連続性を見る分 in-memory BM25 より precision が高い。

### いつ FTS5 へ切り替えるか
- `mdq stats` で `chunks` が概ね数千件を超え、`search` のレイテンシが体感できるようになったとき。
- benchmark スクリプト `<kit>/benchmark.py` で実測比較する（捏造禁止のため数値は実測値を採用）。

---

## 4. DB ファイルの実体

組合せごとのファイル名:

| --lang | --strategy | DB ファイル名 |
|---|---|---|
| `ja-jp` | `heading` | `.mdq/index-ja-jp-heading.sqlite` |
| `ja-jp` | `heading_recursive` | `.mdq/index-ja-jp-heading_recursive.sqlite` |
| `ja-jp` | `fixed_window` | `.mdq/index-ja-jp-fixed_window.sqlite` |
| `ja-jp` | `semantic_paragraph` | `.mdq/index-ja-jp-semantic_paragraph.sqlite` |
| `ja-jp` | `pageindex` | `.mdq/index-ja-jp-pageindex.sqlite` |
| `en-us` | `heading` | `.mdq/index-en-us-heading.sqlite` |
| `en-us` | `heading_recursive` | `.mdq/index-en-us-heading_recursive.sqlite` |
| `en-us` | `fixed_window` | `.mdq/index-en-us-fixed_window.sqlite` |
| `en-us` | `semantic_paragraph` | `.mdq/index-en-us-semantic_paragraph.sqlite` |
| `en-us` | `pageindex` | `.mdq/index-en-us-pageindex.sqlite` |

複数組合せを使う場合、それぞれに `mdq index` の実行が必要（増分キャッシュは DB ごとに独立）。`--db PATH` を明示すると上記命名規則を上書きできる。

---

## 5. SCHEMA バージョンとマイグレーション

実装位置: `mdq/store.py`（`SCHEMA_VERSION = 7`）

| 世代 | 内容 | 移行挙動 |
|---|---|---|
| v1 | `part_index` / `part_total` カラム導入 | ADD COLUMN（破壊的再構築なし） |
| v2 | `chunk_id` 導出式変更（`start_line` → `occurrence_index`） | `chunks` 行を削除し `files.sha1` を空に。次回 index 実行で全件再構築 |
| v3 | `chunks_fts` FTS5 ミラー + 同期トリガ追加 | FTS5 対応ビルドで best-effort。未対応ビルドはスキップ |
| v4 | `parent_chunk_id` カラム追加（直近上位見出しチャンク参照） + `idx_chunks_parent` インデックス | ADD COLUMN（NULL 初期値）。後方互換: 列が NULL の場合は `_resolve_parent` が heading_path rsplit へフォールバック |
| v5 | `text_raw` + `chunk_embedding` カラム追加（`semantic_paragraph` 用） | ADD COLUMN（両列 NULL 可）。既存行は破壊されない |
| v6 | `summary` カラム追加（`pageindex` 用） | ADD COLUMN（NULL 可）。`pageindex` 以外の戦略では NULL |
| v7 | `trigram` ミラーを `detail=none` へ変更（索引サイズ削減） | 旧ミラーを DROP して rebuild。**`VACUUM` は行わない**ため既存ファイルのサイズはそのまま残る |

`open_store()` 呼び出し時に旧 DB を検出して自動マイグレーションされる。再作成判定は仮想表の引数列全体（`tokenize` と `detail`）を比較する。
既存ファイルを縮めたい場合は `mdq index --rebuild` で作り直すか、`VACUUM` を 1 度実行する（実測: 164.9MB → 30.2MB）。
