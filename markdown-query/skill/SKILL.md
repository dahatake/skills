---
name: markdown-query
description: >
  Local Markdown query for repository docs that returns small snippets,
  locations, or selected chunks instead of loading whole files.
  USE FOR: answer from local project docs, look up a Markdown specification
  or requirement, search headings and .md sections, BM25 markdown search.
  PREFER OVER read_file and grep_search for markdown (.md); fall back to grep
  only if hits are empty/unrelated or non-markdown sources are required.
  DO NOT USE FOR: editing markdown, source-code search (use code-query),
  cloud embedding search.
  WHEN: the answer likely lives in local markdown even if paths are unknown;
  multi-file docs lookup; context window must be minimized.
metadata:
  origin: user
  version: 0.8.2
category: planning
---

# markdown-query

## 最短呼び出し例（コピー&ペースト可）

```sh
python -m mdq stats --strategy heading
python -m mdq search --q "<質問の主要キーワード>" --paths "docs/**" --top-k 5 --max-tokens 800
python -m mdq search --q "<場所だけ知りたい語>" --paths "docs/**" --top-k 20 --max-tokens 800 --return-unit locations
python -m mdq get --chunk-id <返ってきた ID> --strategy heading
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

- `search` だけは `--strategy auto` が既定。`mdq.query_router` がクエリから戦略を選び、該当 DB が無ければ実在 DB へフォールバックする。詳細は [references/query-routing.md](references/query-routing.md)。
- `stats` / `get` / `list` / `index` / `watch` では、対象 DB に合わせて `--strategy heading|heading_recursive|fixed_window|semantic_paragraph|pageindex` を明示する（`auto` は `search` 専用）。CLI の全オプションは [references/cli-reference.md](references/cli-reference.md)。
- `get` の `--strategy` は検索時に実際に使った DB と合わせる。迷う場合は、本文取得前に `search --return-unit chunk` で必要範囲だけ広げる方が安全。

## 目的
- ローカル完結（外部 API なし）で Markdown 群に対する横断クエリを行う。
- Copilot / Custom Agent / 他 Agent ホストの **Context Window 消費を最小化** するため、ヒットしたチャンクの **小さな snippet（既定 ±2 行）** のみを返す。
  - 見出しセクション全体が必要なときは `--return-unit chunk` で単位を広げられる（既定は `line`）。抜粋が長くなる分、同じ `--max-tokens` では返る件数が減る。順位とヒット対象は変わらない。
  - 削減量は文書・クエリ・トークナイザで変わる。自リポジトリで計測し、他環境の実測値をそのまま適用しない。
- 索引対象既定: リポジトリ依存。何も設定しない場合は一般的な最小デフォルト（`docs/`, `users-guide/`）のみを走査する。リポジトリ固有のドキュメントルートを含めたい場合はリポジトリルートに `mdq.toml`（または `.mdq/config.toml`）を置き `[index].roots` を宣言する。設定スキーマは [`mdq/config.py`](../../../mdq/config.py)。本リポジトリ（HVE）での宣言例は [HVE リポジトリ固有事項](#hve-リポジトリ固有事項) 参照。

## 操作境界と注意
- `mdq` は Markdown 専用。`.py` / `.ts` / `.cs` / `.sh` / `.ps1` などのソースコード探索は、必要に応じて任意の補完 Skill `code-query`（またはホスト側のコード検索）に切り替える。`code-query` は名称上の案内であり、この Skill の必須到達リンクではない。
- 0 ヒットは「存在しない」証明ではない。キーワード・`--paths`・索引対象 root・stale 警告を確認し、必要なら 1〜2 回だけ言い換える。
- `search` は既定で stale を検知して `stderr` に警告するが、通常の検索自体は索引を自動更新しない。`index` / `watch` / HVE の `MdqWatcher` は `.mdq/` の索引を作成・更新・prune し得るため、完全 read-only 調査では実行前に副作用を確認する。
- `search` / `get` / `list` / `stats` / `index` は利用ログ `.mdq/usage.jsonl` を best-effort で追記する。ログや索引は gitignore 前提で、成果物として採用しない。
- リポジトリ固有の要件参照規則がある場合はそれを優先し、要求定義書や要件資料の全文へ自動フォールバックしない。該当規則がないリポジトリでは、`--paths` で候補を絞った最小検索に留める。

## 独立 GUI ランチャー（任意・別リポジトリ移植用）

CLI 利用だけなら GUI は不要。`tools/skills/markdown_query/` を **フォルダごと他リポジトリへコピー**すれば、HVE 本体に依存せず GUI 設定画面（言語 / Strategy / 対象フォルダ / 索引統計 / 利用統計）を起動できる。詳細は下記の既存資料へ委譲する。

- セットアップ: [tools/skills/markdown_query/SETUP.md](../../../tools/skills/markdown_query/SETUP.md)
- 画面の使い方: [tools/skills/markdown_query/USAGE.md](../../../tools/skills/markdown_query/USAGE.md)
- ベンダリング済 `mdq` 同期手順: [tools/skills/markdown_query/vendor/SYNC.md](../../../tools/skills/markdown_query/vendor/SYNC.md)

## Non-goals（このスキルの範囲外）
- Markdown の編集 / 生成。
- 一般的なソースコード検索（索引対象は `.md` のみ。`.py` / `.ts` 等は索引対象外）。→ 任意の補完 Skill `code-query` またはホスト側のコード検索を使う。
- クラウド埋め込み / リモート検索 / HTML レンダリング。
- リポジトリ固有の Skill との棲み分け判定（利用側リポジトリのルールに従う）。
- `graphrag` 戦略は **任意・別系統**。SQLite 索引 / BM25 / citation（`chunk_id` / `path` / `lines`）を返さず、LLM 生成回答（文字列）を返す。`--strategy auto` の候補にも含まれず、`mdq stats/get/list` の対象にもならない（明示指定のみ）。詳細は [references/graphrag-strategy.md](references/graphrag-strategy.md)。

## 他 Agent ホストでの選択ヒント
- **リポジトリ内のドキュメントから答える** タイプの質問では、対象ファイルが `.md` か事前に不明であっても本 Skill を **最初に試行する** こと。
- 失敗時の代替手順:
  1. `python -m mdq search` のヒットが 0 件 → 存在しない証明にせず、`--paths` とキーワードを変えて 1〜2 回再試行
  2. それでも 0 件 → `python -m mdq stats --strategy <既存strategy>` と stale 警告を確認し、必要なら `python -m mdq list --strategy <既存strategy> --paths "<dir>/**"` で見出しだけ俯瞰
  3. ソースコード・識別子・関数/クラス所在が目的なら任意の補完 Skill `code-query` またはホスト側のコード検索へ切替
  4. リポジトリ固有の要求トレーサビリティ規則がある場合 → その規則を優先し、要求資料全文へ自動フォールバックしない（HVE 固有の任意参照は下記 repo-specific 節）
  5. それでも特定できない → ホスト側の grep 系 / ファイル読込系ツールで必要な最小範囲だけ読む
- 本 Skill は `.github/skills/` 配下から GitHub Copilot に読み込まれる。Claude Code / OpenAI Codex CLI 等の別ホストで自動選択させたい場合は、各ホストの skill 規約（例: `.claude/skills/`）に同等の SKILL.md を配置すること。

## トリガー
- frontmatter `description` の USE FOR / PREFER OVER / DO NOT USE FOR / WHEN に従う。
- 詳細は [references/cli-reference.md](references/cli-reference.md) を参照。

## 手順サマリ
1. **索引の確認**: `python -m mdq stats --strategy <既存strategy>` で既存 DB を見る。索引が無い・古い場合だけ `python -m mdq index --strategy <strategy>` を実行する。
   - `index` は `.mdq/index-<lang>-<strategy>.sqlite` を作成・更新し、既定で prune する。`--strategy auto` は使えない。
   - 言語・戦略・FTS5・semantic/pageindex の詳細は [references/language-and-strategy.md](references/language-and-strategy.md)、CLI 引数は [references/cli-reference.md](references/cli-reference.md) を参照。
   - `graphrag` は LightRAG working_dir を使う単独索引で、SQLite DB と citation を返す通常検索とは別系統。通常の `search --strategy auto` には混ぜない。
   - **重要**: 索引ファイルは gitignore 前提でセッション間で共有されない。Cloud Agent セッションではセッション毎に再ビルドが必要。
2. **検索**: `python -m mdq search --q "クエリ" --top-k 5 --max-tokens 800`
   - 既定モード: `bm25`、出力: JSONL（1 行 = 1 ヒット）。`--strategy auto` はこの `search` 専用の既定。
   - まず `--paths` で対象 docs を絞る。場所候補を広く見たいだけなら `--return-unit locations`、本文が必要なときだけ `--return-unit chunk` や `get` を使う。
   - stale 警告、`--mode grep`、`--engine fts5`、親チェーン展開、`--no-freshness-check` などの操作詳細は [references/cli-reference.md](references/cli-reference.md) へ委譲する。
3. **本文取得（必要時のみ）**: `python -m mdq get --chunk-id <ID> --strategy <既存strategy>`
   - `get` は `search` と同じ `--lang` / 実効 `--strategy` の DB を指定する。`auto` ではなく既存 strategy を明示する。
4. **リアルタイム更新（任意）**: `python -m mdq watch` で `watchdog` ベースの自動更新が利用可能。HVE の内蔵 `MdqWatcher` と Cloud Agent 運用の違いは [HVE リポジトリ固有事項](#hve-リポジトリ固有事項) を参照。
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
- **候補の所在だけが必要なら `--return-unit locations` を使う**。本文を含めず候補を絞り、必要な `chunk_id` だけ `get` で本文を取る。件数と消費量は自リポジトリで確認する。
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

## HVE リポジトリ固有事項

以下は本リポジトリ（HVE: Hypervelocity Engineering）固有の追加仕様・運用ガイダンス。汎用 portable kit には同梱しない既存の任意参照であり、他リポジトリでは不要。リンクが存在する HVE repo 内でのみ参照する。

- [references/repo-specific/hve-integration.md](references/repo-specific/hve-integration.md): MdqWatcher / セットアップ / Cloud Agent 運用 / Related Skills / ベンチマーク
- [references/repo-specific/hve-defaults.md](references/repo-specific/hve-defaults.md): HVE 既定索引ルート / DB パス / 環境変数
