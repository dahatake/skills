# `markdown-query` Skill — Standalone GUI + Benchmark

このディレクトリは Skill `markdown-query` のスタンドアロン GUI と
ベンチマークの **両方** を含みます。フォルダごと他リポジトリへ
コピーすれば、`setup` → `launch-gui` だけで GUI 設定画面が起動します。

| 用途 | 入口 |
|---|---|
| GUI 設定画面（言語 / Strategy / Overlap / 対象フォルダー / 索引統計 / 利用統計） | [`GETTING-STARTED.md`](./GETTING-STARTED.md), [`USAGE.md`](./USAGE.md) |
| トークン削減率ベンチマーク | 本ファイル §2 以降（旧 README 内容） |

スクリーンショット例（基本タブ）:

![基本タブ](./docs/images/screenshot-basic.png)

---

# `markdown-query` Skill ベンチマーク

このディレクトリは Skill `markdown-query` の **撤去判断・チューニング用ベンチマーク**
を置く場所です。Skill 本体（CLI・索引・検索ロジック）はここにはありません。

- Skill 本体定義: [`skill/SKILL.md`](./skill/SKILL.md)
- CLI 詳細リファレンス: [`skill/references/cli-reference.md`](./skill/references/cli-reference.md)
- 実装: [`vendor/mdq/`](./vendor/mdq/)

---

## 1. `markdown-query` とは

`markdown-query`（CLI 名 `python -m mdq`）は、ローカル完結で Markdown 群を
横断検索し、ヒット箇所の **小さな snippet（既定 ±2 行）** だけを返す Skill です。
Copilot / Custom Agent の **Context Window 消費を最小化** することを唯一の目的に
持ち、外部 API は呼び出しません。索引対象既定や Non-goals は SKILL.md を参照
してください。

---

## 2. このディレクトリに用意されているもの

| ファイル / ディレクトリ | 役割 |
|---|---|
| [`benchmark.py`](./benchmark.py) | `baseline_full` / `mdq_bm25` / `mdq_grep` の 3 シナリオを同一プロセス・同一クエリ集合で計測 |
| [`queries.sample.txt`](./queries.sample.txt) | 動作確認用サンプル 5 クエリ（1 行 1 クエリ、`#` はコメント） |
| `results/` | ベンチ結果出力先（リポジトリの `.gitignore` で除外済・コミット禁止） |
| `README.md` | 本ファイル |

---

## 3. いつ使うか（Why）

`markdown-query` Skill は Context Window 削減のためだけに存在します。次のような
判断を **数値で根拠づけたい** ときに本ベンチを使います。

- 別の retrieval 手段（ネイティブ検索、埋め込みベース RAG 等）が利用可能になった
  際に、`markdown-query` を撤去してよいかの A/B 比較。
- 索引対象ルートの追加・除外、`--max-chunk-chars` 変更など **索引パラメータ変更
  前後** の Context 投入量・検索 latency 比較。
- `--top-k` / `--max-tokens` / `--snippet-radius` のチューニング前後比較。

> 本ツールは **撤去判断の閾値を提示しません**。数値を見て利用者が判断します。

---

## 4. 前提条件

- `[mdq]` extras 導入済（`hve/setup-hve.ps1` / `hve/setup-hve.sh` を `-Minimal` /
  `--minimal` 無しで実行していれば既定で導入）。
- 索引が存在する（`python -m mdq index` を実行済）か、ベンチ実行時に
  `--ensure-index` を付ける。
- トークナイザ:
  - `tiktoken` 導入済 → `cl100k_base`（既定）。
  - 未導入 → `fallback(chars/4)` で動作。レポートの `env.tokenizer` で必ず確認。
  - `--require-tiktoken` を指定すると、未導入時に exit code `2` で終了。

---

## 5. クイックスタート（3 ステップ）

```bash
# 1) 索引（初回 or .md を変更したあと）
python -m mdq index

# 2) サンプルクエリでベンチ実行
python <kit>/benchmark.py \
  --queries-file <kit>/queries.sample.txt \
  --ensure-index

# 3) 結果を開く
#    <kit>/results/bench-<UTCタイムスタンプ>.md  (人手レビュー用)
#    <kit>/results/bench-<UTCタイムスタンプ>.json (機械可読)
```

---

## 6. 実行例

```bash
# 既定設定でベンチ（top-k=5, max-tokens=800, repeat=3）
python <kit>/benchmark.py \
  --queries-file <kit>/queries.sample.txt \
  --top-k 5 --max-tokens 800 --repeat 3 --ensure-index

# パスを絞って比較（baseline と mdq の両方に同じ glob を適用）
python <kit>/benchmark.py \
  --queries-file <kit>/queries.sample.txt \
  --paths "docs/*" "users-guide/*" --repeat 5

# BM25 のみ計測（grep / baseline を除外）
python <kit>/benchmark.py \
  --queries-file <kit>/queries.sample.txt \
  --scenarios mdq_bm25 --top-k 3

# coverage_proxy を出すための期待パス付き JSON
python <kit>/benchmark.py \
  --queries-json <kit>/queries.json --repeat 3
```

### `--queries-json` の書式

```json
[
  {"q": "業務要件 概要", "expected_paths": ["docs/business-requirement.md"]},
  {"q": "ARD",          "expected_paths": ["users-guide/01-business-requirement.md"]}
]
```

`expected_paths` 未指定の要素は coverage 計測対象外（`coverage_proxy: null`）。
`queries.json` はリポジトリに同梱していません。必要なときに作成してください。

### 主な引数（`benchmark.py --help` の抜粋）

| 引数 | 既定 | 役割 |
|---|---|---|
| `--q` | なし | クエリを直接指定（繰り返し可） |
| `--queries-file` | なし | 1 行 1 クエリのテキストファイル |
| `--queries-json` | なし | `{"q","expected_paths"}` 配列 JSON |
| `--top-k` | `5` | mdq シナリオの返却件数 |
| `--max-tokens` | `800` | 全 snippet 合計トークン上限 |
| `--repeat` | `3` | クエリあたり計測回数（warmup 除く） |
| `--paths` | なし | baseline / mdq 双方に適用する path glob |
| `--root` | カレント | 索引対象ルート（繰り返し可） |
| `--scenarios` | 全シナリオ | `baseline_full,mdq_bm25,mdq_grep` のサブセット |
| `--ensure-index` | off | 計測前に増分索引を実行 |
| `--db` | `.mdq/index.sqlite` | 索引 DB パス |
| `--out-dir` | `./results` | レポート出力ディレクトリ |
| `--no-markdown-report` | off | Markdown サマリ出力を抑止 |
| `--require-tiktoken` | off | tiktoken 未導入時に exit 2 |

---

## 7. 出力されるもの

実行ごとに `--out-dir`（既定 `<kit>/results/`）配下に
`bench-<UTCタイムスタンプ>.json` と `bench-<UTCタイムスタンプ>.md` の 2 ファイルが
生成されます。

### 7.1 JSON レポート構造

| キー | 内容 |
|---|---|
| `env.tokenizer` | `tiktoken/cl100k_base` または `fallback(chars/4)` |
| `env.python` / `env.platform` / `env.commit` | 再現性メタデータ |
| `params.*` | コマンドライン引数のスナップショット |
| `index` | `--ensure-index` 指定時の索引作成 summary（`index_ms` 含む） |
| `baseline_full` | `files` / `chars` / `tokens`（全文投入時の Context 量） |
| `scenarios.<name>.avg_response_tokens` | クエリ平均応答トークン |
| `scenarios.<name>.avg_vs_baseline_savings_pct` | ベースライン比削減率 (%) |
| `scenarios.<name>.latency_ms_all` | 全クエリ × `--repeat` の `mean/p50/p95/min/max` |
| `scenarios.<name>.per_query[]` | クエリごとの hits / tokens / 削減率 / latency / coverage_proxy |

### 7.2 Markdown サマリ

人手レビュー用。`--no-markdown-report` で抑止可能。

### 7.3 結果の見方（解釈ガイド）

判断は数値を見て利用者が行います。本ツールは閾値を提示しません。
比較するときに有効な観点のみ示します。

- **Context 投入量の差**: `baseline_full.tokens` と
  `scenarios.mdq_bm25.avg_response_tokens` を比較。`avg_vs_baseline_savings_pct`
  が削減率そのもの。
- **シナリオ間比較**: `mdq_bm25` と `mdq_grep` の `avg_response_tokens` を比較し、
  検索方式の特性差を確認。
- **latency の安定性**: `latency_ms_all.p95` を **同一マシン・同一コミット内** で
  A/B 比較する。絶対値はマシン依存なので別環境とは比較しない。
- **取りこぼしの目安**: `--queries-json` で `expected_paths` を与えた場合のみ、
  `per_query[].coverage_proxy` が `0.0〜1.0` で算出される。recall/precision の
  保証ではなく、期待パスが Hit に含まれた割合のみ。
- **トークナイザ確認**: 別実行と比較する前に `env.tokenizer` が一致している
  ことを必ず確認。`fallback(chars/4)` と `tiktoken/cl100k_base` の絶対値は
  比較不可。

---

## 8. 既知の限界（捏造防止のための明示）

- **LLM API は呼ばない**。end-to-end の RAG 品質評価ではなく、Context Window
  投入量と検索 wall-clock の代理指標に留まる。
- **絶対値はマシン依存**。`latency_ms` は同一マシン・同一コミット内での A/B
  比較にのみ用いる。
- **`coverage_proxy` は最低限の代理指標**。期待パスが Hit に含まれる割合のみ
  で、recall/precision を保証しない。
- **トークナイザは tiktoken `cl100k_base` 固定**。他トークナイザでの絶対値
  比較は不可。fallback 時は `env.tokenizer` を必ず確認すること。
- **撤去判断の閾値は本ツールでは提示しない**。数値を見て利用者が判断する。

---

## 9. トラブルシューティング

| 症状 | 原因 / 対処 |
|---|---|
| `no such table` 等の索引エラー | `python -m mdq index` を実行、または `benchmark.py` に `--ensure-index` を付ける |
| `env.tokenizer` が `fallback(chars/4)` になる | `tiktoken` 未導入。`pip install tiktoken` で導入。導入を強制したい場合は `--require-tiktoken` で exit 2 にして CI で検知 |
| `results/` に書き込めない | `--out-dir <path>` で書き込み可能なディレクトリを指定 |
| `unknown scenarios:` で exit 2 | `--scenarios` の値を `baseline_full,mdq_bm25,mdq_grep` のサブセットに修正 |
| ヒットが 0 ばかり | `--paths` の glob を見直す、または索引対象ルートが正しいか `python -m mdq stats` で確認 |

---

## 10. 関連 Skill

- `knowledge-lookup`: `knowledge/D01〜D21` の参照ルール（こちらが優先。上流リポジトリ固有）
- `knowledge-management`: `knowledge/` への書き込み（上流リポジトリ固有）
- `repo-onboarding-fast`: 初見リポジトリでのファイル探索補助（上流リポジトリ固有）

---

## 履歴

- 旧 `tools/measure_mdq_tokens.py` は本ツールに統合・削除されました。
