---
name: markdown-query
description: >
  Answer questions from local repo docs by retrieving small, relevant chunks
  instead of reading whole files. Local-only.
  USE FOR: answer from project docs, look up a specification or requirement,
  search markdown across the repo, find a heading, BM25 markdown search,
  knowledge base lookup.
  PREFER OVER read_file and grep_search for markdown (.md); fall back to grep
  only if hits are empty/unrelated or non-markdown sources are required.
  DO NOT USE FOR: editing markdown, source-code search (use code-query),
  cloud embedding search.
  WHEN: the answer likely lives in local markdown even if paths are unknown;
  multi-file docs lookup; context window must be minimized.
metadata:
  origin: user
  version: 0.7.0
category: planning
---

# markdown-query

## 最短呼び出し例（コピー&ペースト可）

```sh
python -m mdq stats                                                    # 索引存在を確認
python -m mdq index                                                    # 未作成 or 古ければ実行（増分）
python -m mdq search --q "<質問の主要キーワード>" --top-k 5 --max-tokens 800
# search は --strategy auto が既定。クエリ内容から chunking strategy を自動選択し、
# 実在する DB へフォールバックする。手動選択したい場合は --strategy heading 等を明示。
# 見出しセクション単位で本文が欲しいときは:
python -m mdq search --q "<キーワード>" --top-k 3 --return-unit chunk
# snippet で不足する場合のみ:
python -m mdq get --chunk-id <返ってきた ID>
```

> **`python -m mdq` が `No module named mdq` で失敗する場合**（配布キットで導入したリポジトリ）
> エンジンはキット同梱の `vendor/` にあり、システムの Python からは見えない。
> 同梱ランチャを使う。サブコマンドと引数は同一で、出力も同じ。
>
> ```pwsh
> <kit>\mdq.ps1 search --q "<キーワード>"       # Windows
> ```
>
> ```sh
> bash <kit>/mdq.sh search --q "<キーワード>"   # macOS / Linux
> ```
>
> `<kit>` は `markdown-query` キットを配置したディレクトリ（例: `tools/kits/markdown-query`）。
> 見つからない場合はこのリポジトリに未導入。リポジトリの README を参照すること。

## 目的
- ローカル完結（外部 API なし）で Markdown 群に対する横断クエリを行う。
- Copilot / Custom Agent / 他 Agent ホストの **Context Window 消費を最小化** するため、ヒットしたチャンクの **小さな snippet（既定 ±2 行）** のみを返す。
  - 見出しセクション全体が必要なときは `--return-unit chunk` で単位を広げられる（既定は `line`）。抜粋が長くなる分、同じ `--max-tokens` では返る件数が減る。順位とヒット対象は変わらない。
  - Context 削減率はリポジトリの Markdown 量とクエリに左右される。自リポジトリでの実測はキット同梱の `benchmark.py` で取る。
- 索引対象既定: リポジトリ依存。何も設定しない場合は一般的な最小デフォルト（`docs/`, `users-guide/`）のみを走査する。リポジトリ固有のドキュメントルートを含めたい場合はリポジトリルートに `mdq.toml`（または `.mdq/config.toml`）を置き `[index].roots` を宣言する。`[index].exclude` で除外 glob も指定できる。

## 独立 GUI ランチャー（任意）

配布キットには GUI 設定画面（言語 / Strategy / 対象フォルダー / 索引統計 / 利用統計）が同梱されている。CLI 利用には不要。キット直下の `launch-gui.ps1` / `launch-gui.sh` で起動し、操作はキットの `USAGE.md` にある（`install` 時に `--with-gui` を付けた場合のみ動作する）。

## Non-goals（このスキルの範囲外）
- Markdown の編集 / 生成。
- 一般的なソースコード検索（索引対象は `.md` のみ。`.py` / `.ts` 等は索引対象外）。→ [`code-query`](../code-query/SKILL.md) を使う。
- クラウド埋め込み / リモート検索 / HTML レンダリング。
- リポジトリ固有の Skill との棲み分け判定（利用側リポジトリのルールに従う）。
- `graphrag` 戦略は **任意・別系統**。SQLite 索引 / BM25 / citation（`chunk_id` / `path` / `lines`）を返さず、LLM 生成回答（文字列）を返す。`--strategy auto` の候補にも含まれない（明示指定のみ）。詳細は [references/graphrag-strategy.md](references/graphrag-strategy.md)。

## 他 Agent ホストでの選択ヒント
- **リポジトリ内のドキュメントから答える** タイプの質問では、対象ファイルが `.md` か事前に不明であっても本 Skill を **最初に試行する** こと。
- 失敗時の代替手順:
  1. `python -m mdq search` のヒットが 0 件 → 異なるキーワードで 1〜2 回再試行
  2. それでも 0 件 → `python -m mdq list` で対象ディレクトリの見出し俯瞰
  3. それでも特定できない → ホスト側の grep 系 / ファイル読込系ツールで生ファイルへフォールバック
- 本 Skill は `.github/skills/` 配下から GitHub Copilot に読み込まれる。Claude Code / OpenAI Codex CLI 等の別ホストで自動選択させたい場合は、各ホストの skill 規約（例: `.claude/skills/`）に同等の SKILL.md を配置すること。

## トリガー
- frontmatter `description` の USE FOR / PREFER OVER / DO NOT USE FOR / WHEN に従う。
- 詳細は [references/cli-reference.md](references/cli-reference.md) を参照。

## 手順サマリ
1. **索引の確認と作成**: `python -m mdq stats` → 0 件または古ければ `python -m mdq index`
   - 省略時は `mdq.toml`（または `.mdq/config.toml`）の `[index].roots` を参照。設定ファイルがない場合は最小デフォルト（`docs/`, `users-guide/`）を走査。存在しないフォルダは自動スキップ。
   - 増分更新（SHA-1 + mtime 一致ファイルはスキップ）。既定で自動 prune（ディスク上に存在しないファイルのチャンクを削除、`--no-prune` で無効化可）。
   - 言語・戦略を切替える場合は `--lang ja-jp|en-us` / `--strategy heading|heading_recursive|fixed_window|semantic_paragraph|pageindex` を指定（既定 `ja-jp` / `heading`）。`semantic_paragraph` は `[semantic]` extra (fastembed + nltk + numpy) が必要。`pageindex` は見出しベースのツリー索引とノード単位のサマリ (`chunks.summary`) を生成し LLM 不要。任意で `--strategy graphrag`（LightRAG ベースの knowledge-graph 検索、LLM 必須・別系統。`auto` 対象外）も指定可。詳細は [references/language-and-strategy.md](references/language-and-strategy.md) / [references/graphrag-strategy.md](references/graphrag-strategy.md) を参照。`semantic_paragraph` 固有仕様は [references/semantic-paragraph.md](references/semantic-paragraph.md)。
   - **重要**: 索引ファイル（既定 `.mdq/index-<lang>-<strategy>.sqlite`）は gitignore 推奨でセッション間で共有されない前提。Cloud Agent セッションでもセッション毎に再ビルドが必要。
2. **検索**: `python -m mdq search --q "クエリ" --top-k 5 --max-tokens 800`
   - 既定モード: `bm25`、出力: JSONL（1 行 = 1 ヒット）
   - ランキングの照合単位は **CJK が隣接 2 文字（bigram）**、ASCII 語は分割しない。照合対象はチャンク本文に加えて **リポジトリ相対パスと見出し経路**で、見出し経路をより重く扱う。パスや見出しにだけ現れる語でも到達できる（抜粋と行範囲は本文のまま）。`--mode grep` と FTS5 経路は本規定の対象外で、本文だけを照合する。
   - **索引の乖離を自動検知する**。索引済みファイルのサイズと更新時刻だけを見て（実測 162 ファイルで 4.7 ms）、乖離していれば `{"warning":"stale","changed":N,"hint":"..."}` を **stderr** へ出す。この警告が出たら `python -m mdq index` を実行してから再検索する。不要なら `--no-freshness-check`。
   - **`--strategy` 既定は `auto`**。Skill 側 `mdq.query_router` がクエリから最適 strategy を選択し、不在時はフォールバックする（[references/query-routing.md](references/query-routing.md)）。手動で選びたい場合のみ `--strategy heading|heading_recursive|fixed_window|semantic_paragraph|pageindex` を明示。
   - `--paths` / `--tags` / `--snippet-radius` で絞り込み、`--mode grep` で完全一致検索に切替
   - `--return-unit chunk` でヒットを含むチャンクの本文全体を返す（既定 `line` は ±`--snippet-radius` 行）。**「どこに書くか」を探すときは `--return-unit locations`** を使うと本文を返さず所在だけを返すので、同じ予算でより多くの候補を見られる
   - **`index` は `--strategy` を明示する（既定 `heading`）。`auto` は `search` 専用**。深さを上げた親チェーン取得は `--with-parent-depth N` を使う
   - 大規模コーパスは `--engine fts5` または環境変数 `MDQ_FTS5=1`（旧名 `HVE_MDQ_FTS5` も引き続き有効だが deprecated）で FTS5 検索に切替可能。日本語クエリもヒットする（3 文字未満の語は trigram 索引で表現できないため in-memory BM25 へ自動フォールバックする）
3. **本文取得（必要時のみ）**: `python -m mdq get --chunk-id <ID>`
4. **リアルタイム更新（任意）**: `python -m mdq watch` で `watchdog` ベースの自動更新が利用可能（HVE リポジトリでは CLI Orchestrator 配下に同等機能が内包されている。詳細は [Appendix](#appendix-hve-リポジトリ固有事項) を参照）。
5. 結果を **そのまま Agent に渡す**（生 Markdown を読み込まない）。

## 入出力例

### 入力（Agent が発行するコマンド）
```
python -m mdq search --q "業務要件 概要" --paths "docs/*" --top-k 3 --max-tokens 500
```

### 出力（JSONL: 1 行 = 1 ヒット）
```json
{"chunk_id":"<sha1>","path":"docs/business-requirement.md","heading_path":"# 概要 > ## 範囲","lines":[42,71],"score":12.7,"snippet":"...マッチ前後 ±2 行..."}
```

## Context 節約のコツ
- まず `--format compact` で目視確認 → 必要な `chunk_id` だけ `get` で詳細取得。
- `--max-tokens` は **実際に返す JSON 1 行分の実トークン数**で判定される（抜粋だけではなく path / heading_path / score 等の metadata も含む）。日本語文書では **800 tokens で 2〜3 件**、**5 件必要なら 1,600 前後**が目安。
- `--top-k` を 3〜5 に保ち、件数が足りないときは `--top-k` ではなく `--max-tokens` を上げる。
- **候補を多く見たいだけなら、予算を上げるより `--return-unit locations` が安い**。本リポジトリ実測（ゴールデン 60 問）: `--top-k 5 --max-tokens 1600` が平均 4.5〜4.8 件 / 1,234〜1,316 tokens なのに対し、`--top-k 20 --max-tokens 800 --return-unit locations` は平均 6.7〜7.1 件 / 714〜756 tokens で、期待箇所への到達率は 4 スライスすべてで同等以上だった。所在を絞ってから `get` で本文を取る。
- `--paths` でディレクトリを絞ると BM25 精度も向上する。
- 文脈拡張が必要なら `--include-parent` / `--with-parent-depth N` / `--expand-neighbors 1` を併用。

## 詳細ガイド（Progressive Disclosure）
- CLI 詳細: [references/cli-reference.md](references/cli-reference.md)
- 言語 / チャンキング戦略 / 検索エンジンの選択: [references/language-and-strategy.md](references/language-and-strategy.md)
- **Auto Strategy ルーティングルールと統計 H1/H2**: [references/query-routing.md](references/query-routing.md)
- クエリ例パターン集: [references/query-patterns.md](references/query-patterns.md)
- 索引内部仕様: [references/indexing-internals.md](references/indexing-internals.md)
- **GraphRAG 戦略（任意・LLM 必須）**: [references/graphrag-strategy.md](references/graphrag-strategy.md)
- Prompt / Custom Agent 組み込み例: [examples/prompt-snippets.md](examples/prompt-snippets.md)

## Appendix: HVE リポジトリ固有事項

以下は本リポジトリ（HVE: Hypervelocity Engineering）固有の追加仕様・運用ガイダンス。他リポジトリへ本 Skill を移植して利用する場合は参照不要。

- references/repo-specific/hve-integration.md（上流リポジトリ専用のため本キットには同梱していない）: MdqWatcher / セットアップ / Cloud Agent 運用 / Related Skills / ベンチマーク
- references/repo-specific/hve-defaults.md（上流リポジトリ専用のため本キットには同梱していない）: HVE 既定索引ルート 11 個 / DB パス / 環境変数
