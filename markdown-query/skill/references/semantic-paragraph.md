# `semantic_paragraph` 戦略

Embedding 類似度に基づくチャンキング戦略。設計プラン: `work/semantic-paragraph/plan.md`（上流リポジトリ専用）。

## 1. 概要

| 項目 | 値 |
|---|---|
| 実装 | `mdq/strategies_semantic.py` |
| extras | `pip install -e .[semantic]`（fastembed + nltk + numpy） |
| 既定 provider | `fastembed`（ONNX, CPU。`MDQ_EMBED_PROVIDER` で override） |
| 既定 model | `intfloat/multilingual-e5-large`（多言語・MIT・初回 ~2.2GB DL。`MDQ_EMBED_MODEL` で override） |
| sentence splitter | `nltk.sent_tokenize`（`punkt_tab` を初回自動 DL、失敗時 regex fallback） |
| heading 境界 | **hard boundary**（絶対跨がない） |
| contextualizer | **既定 ON**（`[Context] {path} > {heading_path}\n\n{body}` を prepend、`text_raw` 列に原文保持） |
| late chunking | opt-in (`--late-chunking`、`chunk_embedding` 列に float32 ベクトル保存) |

## 2. アルゴリズム（Kamradt-modified）

1. 既存 heading splitter で heading 境界分割。
2. 各 heading chunk について:
   1. `nltk.sent_tokenize` で文分割（コードフェンス/テーブルは保護）。
   2. `buffer_size`（既定 1）文の sliding window を embed。
   3. 隣接 window のコサイン距離列を計算。
   4. **バイナリサーチ**: `[percentile_lo, percentile_hi]` 区間（既定 `[50, 99]`）で「最大 chunk が `max_chars` 以下となる最小パーセンタイル」を最大 8 反復で探索。
   5. `min_chars`（既定 200）未満の sub-chunk は直前の sub-chunk へ merge。
3. heading 境界は絶対跨がない。
4. contextualize ON のとき `text = TEMPLATE.format(path, heading_path, body)`、`text_raw = body`。

## 3. CLI フラグ

```
python -m mdq index --strategy semantic_paragraph \
    --max-chunk-chars 1000 \
    --breakpoint-percentile-lo 50 \
    --breakpoint-percentile-hi 99 \
    --min-chars 200 \
    --embed-provider fastembed \
    --embed-model intfloat/multilingual-e5-large \
    [--no-semantic-contextualize] \
    [--late-chunking]
```

すべて Q8=A の方針に従い CLI から上書き可能。未指定値は ``mdq/strategies_semantic.py`` の `SEMANTIC_*` 定数が既定。

検索側は late chunking がある index に対し:

```
python -m mdq search --q "..." --strategy semantic_paragraph --fusion-alpha 0.5
```

`final_score = alpha * bm25_norm + (1 - alpha) * cosine_sim`（既定 α=0.5。1.0=BM25 のみ、0.0=cosine のみ）。

## 3.5 GUI から操作する場合

HVE / standalone GUI の「Markdown-Query」セクション → [基本] タブで
`Chunking Strategy = semantic_paragraph` を選ぶと、CLI フラグと 1:1 対応する
設定パネル（`mdq/semantic_options.py`）が下に表示される:

- max_chunk_chars / min_chars / breakpoint percentile lo・hi / embed_provider /
  embed_model / contextualize（既定 ON） / late_chunking / fusion_alpha
- `[semantic]` extra 未インストール時は赤バナーで警告し、フォームを無効化
- fusion_alpha は `late_chunking` ON のときのみ表示（Q9=A）

[インデックス管理] タブのボタン群:

| ボタン | 動作 | CLI 等価 |
|---|---|---|
| インデックスの手動更新 | 増分ビルド（SHA-1 一致でスキップ） | `python -m mdq index` |
| 完全再ビルド | 確認後に rebuild=True | `python -m mdq index --rebuild` |
| DB を削除 | 二重確認後に DB ファイル unlink | `Remove-Item .mdq/index-<lang>-<strategy>.sqlite` |

ビルド進捗はファイル単位（`索引中: 3/12 — docs/foo.md`）でプログレスバーに表示。
試し検索パネル（既定折りたたみ）で top_k=3 のヒットを path / heading_path /
score / snippet 列のテーブルで確認できる。

## 4. 失敗時フォールバック

| 状況 | 挙動 |
|---|---|
| `[semantic]` extra 未インストール | `heading_recursive` へ降格し stderr に警告 |
| multilingual-e5-large モデル DL 失敗 | `EmbeddingsUnavailable` → `heading_recursive` へ降格 |
| nltk `punkt_tab` DL 失敗 | regex 文分割で続行（出力品質はやや低下） |
| 1 heading chunk が `max_chars` 以下 | embedding 計算スキップで heading そのまま |

## 5. ストレージ（SCHEMA v5）

`mdq/store.py` の `SCHEMA_VERSION=5` で導入:

| 列 | 型 | 用途 |
|---|---|---|
| `text_raw` | TEXT NULL | 原文（contextualize ON のとき有意義。OFF/その他戦略では NULL） |
| `chunk_embedding` | BLOB NULL | float32 ベクトルバイト列（late-chunking ON のときのみ） |

両列は ADD COLUMN マイグレーションで追加（破壊なし）。

## 6. 文献

- Greg Kamradt, "5 Levels of Text Splitting"（[notebook](https://github.com/FullStackRetrieval-com/RetrievalTutorials/blob/main/tutorials/LevelsOfTextSplitting/5_Levels_Of_Text_Splitting.ipynb)）— breakpoint percentile semantic chunking の元論
- LlamaIndex `SemanticSplitterNodeParser`（[doc](https://developers.llamaindex.ai/python/examples/node_parsers/semantic_chunking/)）— buffer_size + breakpoint_percentile_threshold の意味論
- Chroma "Evaluating Chunking Strategies for Retrieval" (Smith & Troynikov, 2024、[URL](https://www.trychroma.com/research/evaluating-chunking)) — Kamradt-modified / token-level IoU 評価フレーム
- Anthropic "Contextual Retrieval"（[URL](https://www.anthropic.com/news/contextual-retrieval)）— chunk への文脈前置で top-20 retrieval failure を 35〜67% 削減
- Jina AI "Late Chunking"（[arXiv:2409.04701](https://arxiv.org/abs/2409.04701)）— 長 context embedding → boundary pooling
