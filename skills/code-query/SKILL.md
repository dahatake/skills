---
name: code-query
description: >
  Answer questions about source code by retrieving definitions and small
  snippets instead of whole files. Local-only.
  USE FOR: find where a function or class is defined, find callers or
  references, trace a requirement or test ID to code, regex or symbol search
  over source, code Q&A.
  PREFER OVER read_file, grep_search, and ripgrep for source files
  (.py, .cs, .js, .ts, .sh, .ps1); fall back to grep only if hits are empty
  or unrelated.
  DO NOT USE FOR: editing code, markdown lookup (use markdown-query),
  cloud embedding search.
  WHEN: where something lives or what calls what; multi-file code lookup;
  context window must be minimized.
metadata:
  origin: user
  version: 0.3.0
category: planning
---

# code-query

`markdown-query`（`.md` 専用）のソースコード版。**別パッケージ・別 DB**で、Markdown は索引しない。

## 最短呼び出し例（コピー&ペースト可）

```sh
python -m cq stats  --profile <profile>                      # 索引の存在と規模を確認
python -m cq index  --profile <profile>                      # 未作成 or 古ければ実行（増分）
python -m cq search --profile <profile> --q "<探したい語>"     # 既定 --mode auto / --top-k 5 / --max-tokens 800
python -m cq search --profile <profile> --q "<探したい語>" --return-unit chunk  # 関数・クラス単位で本文ごと返す
python -m cq def    --profile <profile> --symbol <Class.method>  # 定義へ直行（qualname）
python -m cq get    --profile <profile> --chunk-id <ID>      # snippet で不足するときだけ本文を取る
```

> **`python -m cq` が `No module named cq` で失敗する場合**（配布キットで導入したリポジトリ）
> エンジンはキット同梱の `vendor/` にあり、システムの Python からは見えない。
> 同梱ランチャを使う。サブコマンドと引数は同一で、出力も同じ。
>
> ```pwsh
> <kit>\cq.ps1 search --q "<探したい語>"       # Windows
> ```
>
> ```sh
> bash <kit>/cq.sh search --q "<探したい語>"   # macOS / Linux
> ```
>
> `<kit>` は `code-query` キットを配置したディレクトリ（例: `tools/kits/code-query`）。
> 見つからない場合はこのリポジトリに未導入。リポジトリの README を参照すること。

`--profile` は対象コードベースの切り替えで、リポジトリルートの `cq.toml` の `[profiles.<name>]` を選ぶ。
毎回書かずに済ませたい場合は環境変数 `CQ_PROFILE`、または配布キットのランチャ
（`cq.ps1` / `cq.sh` / `cq.cmd`）を使う。設定が存在しない場合は fail-closed で停止するので、
初回は `init_config.py` か `cq.toml.sample` で設定を用意する。

## 目的

- ローカル完結（外部 API なし・文法のダウンロードなし）でソースコードを横断検索する。
- Agent の **Context Window 消費を最小化**するため、ヒット箇所の小さな snippet（既定 ±2 行）だけを返す。
  - 関数・クラス単位で本文が欲しいときは `--return-unit chunk` で単位を広げられる（既定は `line`）。cAST が切り出した構造チャンクをそのまま返すため、`get` を追加で呼ばずに完結する。
  - 検索品質の実測値はリポジトリ依存のため references/repo-specific/hve-integration.md（上流リポジトリ専用のため本キットには同梱していない） へ隔離している。
- **索引は変更されたファイルについては常に最新に保たれる**。検索のたびに `stat()` だけで差分を突合し、50 件以下なら自動で再索引してから応答する
  （上限超過時は結果を返しつつ最終行に `{"warning":"stale","changed":N}` を出す）。
  - **ただし「一度も索引されていない新規ファイル」はこの差分突合の対象外**（索引済みパスだけを `stat()` するため）。
    新規作成したファイルを引きたいときは `python -m cq index` か `python -m cq watch` が必要。
    stale 警告は変更件数しか申告しないので、**0 ヒットだからといって存在しないとは限らない**。
  編集が頻繁な場合は `python -m cq watch --profile <profile>` を並走させる。

## 検索モードの選び方

既定は `--mode auto`。以下を自動判定するので、通常は指定しなくてよい。

| 探しているもの | auto の判定 | 明示指定 |
|---|---|---|
| `FR-CQ-06` / `TEST-SVC-02-001` などの ID | trace | `--mode trace` |
| 関数名・クラス名・`Module.Class.method` | symbol | `--mode symbol` |
| 記号を含む部分文字列 / 完全一致したい語 | substr | `--mode substr` |
| 正規表現 | regex | `--re "<pattern>"` |
| 自然文・複数語 | bm25 | `--mode bm25` |

0 件のときは自動でフォールバックする。どの経路で引けたかは各ヒットの `route` フィールドで判別する。
フォールバックには 2 つの段階がある。

1. **自然文（bm25）が 0 件のとき、語の連言を選言へ 1 回だけ緩和して再試行する**。
   BM25 の既定は暗黙 AND なので、語数が増えるほど 0 件になりやすい（実測: 4 語で 5 ヒットだった問いに
   3 語足すと 0 件）。緩和して得たヒットは `match` が `or-fallback` になる。**全語を含むヒットではない**ので
   通常のヒットより確度が低いと扱うこと。語が 1 つのとき、および **CJK を含むクエリでは緩和しない**
   （誤った上位ヒットを返すより 0 件を返す方針の維持）。
2. **すべての経路が 0 件のとき、リポジトリ相対パスの部分一致で引く**（`route` は `path`）。
   索引は本文・名称・シグネチャ・識別子語しか持たないので、**pytest の失敗出力に現れるテストモジュール名**は
   この層でないと引けない。ファイルごとに先頭チャンク 1 件を返す。`--mode path` は無い（連鎖専用）。
**`match` フィールドを必ず見ること**。symbol 経路で `Class.method` が完全一致しない場合、
末尾の名前だけで再探索した結果を返す。このとき `match` は `name-fallback`、`score` は 0.5 になる。
別ファイルの同名関数を掴んでいる可能性があるので、`qualname` を確認すること。

## その他のサブコマンド

```sh
python -m cq refs  --profile <profile> --symbol <symbol>       # 呼び出し元を列挙
python -m cq trace --profile <profile> --id <TRACE-ID>         # トレース ID → コード位置
python -m cq trace --profile <profile> --by-path <file>        # コード → 設計文書のパスとアンカー
python -m cq map   --profile <profile> --paths "<dir>/*" --max-tokens 1200   # 俯瞰マップ
python -m cq watch --profile <profile>                         # 保存を即座に索引へ反映
```

`cq trace` は設計文書の**本文を返さない**。本文が必要なら返ってきたパスとアンカーを `markdown-query` の
`python -m mdq get` へ渡す。これがコード ↔ 設計書の標準的な連携経路。

各サブコマンド（`watch` を除く）の実行は `＜repo-root＞/.cq/usage.jsonl` へ 1 行 1 レコードで自動記録される
（`markdown-query` の `.mdq/usage.jsonl` とは別ファイル）。記録は best-effort で、失敗しても検索結果と
終了コードに影響しない。

## Non-goals（このスキルの範囲外）

- コードの編集 / 生成。
- Markdown・ドキュメントの検索（`.md` は索引対象外）。→ `markdown-query` を使う。
- クラウド埋め込み / リモート検索。
- 日本語の自然文から英語識別子への橋渡し（意味的な言い換えは行わない）。日本語で聞くときはコード中に現れる
  英語の語（関数名・クラス名の一部）を混ぜること。
- 文法を実行時にネットワーク取得する実装（`tree-sitter-language-pack`）の採用。ローカル完結の前提を破る。
  公式の**言語別 tree-sitter 文法**は wheel に文法を同梱しており、ネットワーク遮断下での import と parse を
  実測した上で任意依存（`pip install -e .[code]`）として採用している。

## 対応言語とフィデリティ

| 言語 | パーサ（`parser` 値） | 抽出できるもの |
|---|---|---|
| Python | 標準ライブラリ `ast`（`ast`）。`ast` が解析できないファイルは任意依存 `tree-sitter-python` へフォールバックする（`tree-sitter` / `tree-sitter-partial`） | 定義・シグネチャ・デコレータ・参照・ import、構造チャンク。フォールバック時は docstring（`doc_head`）を回復できない |
| Java / Go / Rust / C / C++ | tree-sitter 公式文法（`tree-sitter`、`ERROR` ノードから回復した場合は `tree-sitter-partial`） | 定義・親スコープ・行範囲・ doc・修飾子・参照・ import、構造チャンク |
| Scala | 同上（`tree-sitter` / `tree-sitter-partial`） | object / class / trait / enum / type / def（Scala 2 と 3 の両方）・クラス/トレイト/オブジェクト直下の `val` / `var` / `given`（`variable`）・クラスパラメータ（`property`）・呼び出し・ import。`def` 本体内のローカル `val`/`var` は対象外（索引雑音を避けるため） |
| shell（bash / sh） | 同上（`tree-sitter` / `tree-sitter-partial`） | 関数定義の行範囲・シグネチャ・ doc・コマンド呼び出し、構造チャンク |
| PowerShell | 同上（`tree-sitter` / `tree-sitter-partial`） | function / filter / class / enum / メソッド（`script:Name` のようなスコープ付き名を切らない）・コマンド呼び出し |
| Windows batch | 同上（`tree-sitter` / `tree-sitter-partial`） | ラベル定義と `call` の参照のみ（この文法に関数の概念は無い） |
| SQL | sqlglot 主・必要時のみ sqlfluff（`sql`） | `CREATE` する table / view / procedure / function / schema と、参照するテーブル。文単位の構造チャンク |
| C# | tree-sitter 公式文法（`tree-sitter` / `tree-sitter-partial`）。未導入なら brace 深度追跡へ降格（`regex`） | 型（class / interface / struct / enum / record）・メソッド・コンストラクタ・参照・ using、構造チャンク（tree-sitter のみ） |
| JavaScript | 同上（`tree-sitter` / `tree-sitter-partial` / `regex`） | class・function・メソッド・代入関数（`const x = () => {}` 等）・参照・ import（`require(...)` は regex のみ）、構造チャンク（tree-sitter のみ） |
| TypeScript / `.tsx`（別言語 `tsx` として登録） | 同上 | JavaScript に加え interface / type / enum / abstract class / 戻り型付きメソッド。`.tsx` は `tree-sitter-typescript` の `language_tsx()` を使う |
| 未登録の言語・解析失敗 | `lite`（正規表現） | 定義行のみ |

`.h` は拡張子だけでは C / C++ を判別できないため、内容を parse して C++ 固有ノード型の有無で振り分ける。

SQL の方言（T-SQL / Oracle / PostgreSQL / BigQuery / Spark / MySQL / SQLite / Snowflake / DuckDB）は
固定順で試し、全文を構造化できた最初の方言を採用する（順序が固定なので結果は決定的）。すべての方言が
構造化できない場合だけ、最後に方言を指定しない解析（全方言のスーパーセット）を 1 回試す。`GO` は
T-SQL のバッチ区切りとして扱う。
PostgreSQL の `$tag$ ... $tag$` ルーチン本体はどちらのエンジンでも 1 トークンになるため、tree-sitter の
SQL 文法で本体だけを再パースして参照を拾う。

PowerShell は文法の回復ノードが残ったファイルに限り、`pwsh` の公式パーサ（`Parser.ParseInput`）へ
エスカレーションする。ソースは stdin からデータとして渡すだけでスクリプトは実行しない。`pwsh` が
無い環境では tree-sitter の結果をそのまま使うため、**同じファイルでも環境によって定義数が変わる**。
公式パーサへのエスカレーションが成功した場合は `parser` が `tree-sitter` のままになる（回復ノードの
影響を受けていないため）。エスカレーションが起きない、または `pwsh` 不在で tree-sitter の回復ノード
付き結果をそのまま使った場合は、他の tree-sitter 言語と同じく `tree-sitter-partial` になる。

Python は標準ライブラリ `ast` が主で、常に最優先で試す。`ast.parse` が構文エラーで失敗したファイル
（編集中で構文が一時的に不正な場合等）だけ、任意依存の `tree-sitter-python` へフォールバックする。
この文法は Python の docstring（本体先頭の文字列リテラル）を回復できないため、フォールバック時は
`doc_head` が空になる。定義・行範囲・デコレータ・呼び出し参照・import は回復する。

tree-sitter 文法と SQL エンジンは**任意依存**であり、未導入の環境では当該言語だけが `lite` へ降格する。
降格は索引全体を失敗させない。`sqlfluff` は `code-sql` extra として `code` から分離している（`click` の
依存 pin が `semantic` extra と衝突するため）。

解析に失敗したファイルも `lite` へ自動降格し、索引からは落とさない。降格したことは応答の `parser`
フィールドに必ず現れるので、**フィデリティが落ちた結果を全文と誤認しないこと**。tree-sitter 系言語
（Java / Go / Rust / C / C++ / Scala / shell / PowerShell / Windows batch）は、文法が `ERROR` ノードから
部分的に回復した場合、`parser` が `tree-sitter` ではなく **`tree-sitter-partial`** になる。この値は文法が
インストールされていないときの `lite` への降格とは異なり、**解析自体は成功しているが該当ファイル内の
一部の定義が欠落・不正確な可能性がある**ことを示す。

## 他 Agent ホストでの選択ヒント

- **リポジトリ内のコードから答える**タイプの質問では、対象ファイルの言語やパスが不明でも本 Skill を最初に試す。
- 失敗時の代替手順:
  1. ヒット 0 件 → キーワードを識別子に寄せて 1〜2 回再試行（`--mode bm25` を明示）
  2. それでも 0 件 → `python -m cq map --profile <p> --paths "<dir>/*"` で俯瞰してから絞り込む
  3. それでも特定できない → ホスト側の grep / ファイル読込へフォールバック
- 索引が存在しない場合、`cq` は 0 件を返さず**エラーで停止**して `cq index` を案内する。黙って空振りしない。

## トリガー

- frontmatter `description` の USE FOR / PREFER OVER / DO NOT USE FOR / WHEN に従う。
- 詳細は [references/cli-reference.md](references/cli-reference.md)、
  索引の内部構造は [references/indexing-internals.md](references/indexing-internals.md) を参照。

## Appendix: HVE リポジトリ固有事項

以下は本リポジトリ（HVE: Hypervelocity Engineering）固有の profile 定義・実測値・具体例。
他リポジトリへ本 Skill を移植して利用する場合は参照不要であり、配布キットにも同梱されない。

- references/repo-specific/hve-integration.md（上流リポジトリ専用のため本キットには同梱していない）: profile 一覧 / 値を埋めた呼び出し例 / 検索品質の実測値 / 索引運用
