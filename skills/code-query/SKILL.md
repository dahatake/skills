---
name: code-query
description: >
  Local source-code lookup; definitions/snippets only.
  USE FOR: definitions, callers/refs, requirement/test ID traces, symbol/regex search, code Q&A.
  PREFER OVER read_file, grep_search, and ripgrep for source files; fall back to grep only if hits are empty or unrelated.
  DO NOT USE FOR: editing code, markdown lookup (use markdown-query), cloud embedding search.
  WHEN: where something lives or what calls what; multi-file lookup; tight context.
metadata:
  origin: user
  version: "0.4.2"
license: MIT
compatibility: Requires Python 3.11+ and git. Install the bundled code-query kit before use.
---

# code-query

`code-query` は `markdown-query`（`.md` 専用）のソースコード版です。別パッケージ・別 DB で、Markdown は索引しません。

## 使う場面と境界

- リポジトリ内の source files から、定義・呼び出し元・参照・要件/テスト ID 対応を小さな snippet で確認する。
- ソース調査では `read_file` / `grep_search` / ripgrep より先に試す。ヒットが空、古い、または無関係な場合だけ代替へ進む。
- コードの編集/生成、Markdown・ドキュメント検索、クラウド埋め込み検索は対象外。Markdown は `markdown-query` を使う。
- ローカル完結が前提。`--semantic` は任意のローカル意味検索で、外部 API・文法の実行時ダウンロード・ネットワーク検索ではない。
- 0 ヒットは不存在証明ではない。未索引の新規ファイル、stale 警告、fallback の `match` を必ず確認する。

## 最短操作

```sh
python -m cq search --profile <profile> --q "<探したい語>"
python -m cq def    --profile <profile> --symbol <Class.method>
python -m cq refs   --profile <profile> --symbol <symbol>
python -m cq get    --profile <profile> --chunk-id <ID>
```

- `search` は `--q`、`def` / `refs` は `--symbol` を使う。`def` / `refs` に `--q` を渡さない。
- `--profile` は `cq.toml` の `[profiles.<name>]` を選ぶ。存在しない設定や索引は fail-closed で停止し、黙って空結果にしない。
- `get` は `search` の snippet で足りないときだけ、返された `chunk_id` の本文を取得する。
- 初回索引や未索引の新規ファイルを含めたい場合は `python -m cq index --profile <profile>`、頻繁な編集時は `python -m cq watch --profile <profile>` を使う。
- CLI 全体、`--paths` / `--max-tokens` / `--return-unit` / `trace` / `map` / `watch` は [references/cli-reference.md](references/cli-reference.md) を参照。
- 索引の構造や抽出方法を調べる場合は [references/indexing-internals.md](references/indexing-internals.md) を参照。

## 結果の読み方

- すべてのヒットで `route` と `match` を見る。`route` は検索経路、`match` は完全一致か fallback かを示す。
- `match: or-fallback` は、BM25 の暗黙 AND が 0 件だった後に 1 回だけ OR match へ緩和した結果。全語を含む保証はない。
- `match: name-fallback` は、`Class.method` などの qualname が完全一致せず末尾名だけで再探索した結果。別ファイルの同名定義に注意し、`qualname` を確認する。
- `--max-tokens` は出力予算で、超えた分は打ち切られる（先頭 1 件は返る）。`map` は下位シンボルを落とし、budget による drop を末尾で知らせる。
- `--semantic` は既定 OFF。ローカル extra と埋め込み済み索引が必要で、ベクトル不在・別モデル・変更検出時は語彙経路へ降格する。ヒット自体を関連性の証明にしない。

## 鮮度と 0 ヒット

- `search` は索引済みパスを `stat()` し、変更ファイルが `--auto-reindex-limit`（既定 50）以下なら応答前に自動再索引する。
- 差分が 50 件を超えると、結果を返した上で最終行に `{"warning":"stale","changed":N}` を出す。この場合、結果は古い可能性がある。
- 一度も索引されていない新規ファイルは差分突合の対象外で、stale 警告にも現れない。新規ファイルを探す前は `cq index` か `cq watch` を使う。
- 0 ヒット時は語を識別子寄りに変えて 1〜2 回再試行し、それでも駄目なら `cq map --profile <profile> --paths "<dir>/*"` で俯瞰してから、最後に grep / ファイル読込へフォールバックする。

## 対応言語とフィデリティ

| 言語 | パーサ（`parser` 値） | 抽出できるもの |
|---|---|---|
| Python | 標準ライブラリ `ast`（`ast`）。`ast` が解析できないファイルは任意依存 `tree-sitter-python` へフォールバックする（`tree-sitter` / `tree-sitter-partial`） | 定義・シグネチャ・デコレータ・参照・ import、構造チャンク。フォールバック時は docstring（`doc_head`）を回復できない |
| Java / Go / Rust / C / C++ | tree-sitter 公式文法（`tree-sitter`、`ERROR` ノードから回復した場合は `tree-sitter-partial`） | 定義・親スコープ・行範囲・ doc・修飾子・参照・ import、構造チャンク |
| Scala | 同上（`tree-sitter` / `tree-sitter-partial`） | object / class / trait / enum / type / def（Scala 2 と 3 の両方）・クラス/トレイト/オブジェクト直下の `val` / `var` / `given`（`variable`）・クラスパラメータ（`property`）・呼び出し・ import。`def` 本体内のローカル `val`/`var` は対象外（索引雑音を避けるため） |
| shell（bash / sh） | 同上（`tree-sitter` / `tree-sitter-partial`） | 関数定義の行範囲・シグネチャ・ doc・コマンド呼び出し、構造チャンク |
| PowerShell | 同上（`tree-sitter` / `tree-sitter-partial`） | function / filter / class / enum / メソッド（`script:Name` のようなスコープ付き名を切らない）・Pester ブロック（`Describe` / `Context` / `It` のラベル。`is_test`）・コマンド呼び出し |
| Windows batch | 同上（`tree-sitter` / `tree-sitter-partial`） | ラベル定義と `call` の参照のみ（この文法に関数の概念は無い） |
| SQL | sqlglot 主・必要時のみ sqlfluff（`sql`） | `CREATE` する table / view / procedure / function / schema と、参照するテーブル。文単位の構造チャンク |
| C# | tree-sitter 公式文法（`tree-sitter` / `tree-sitter-partial`）。未導入なら brace 深度追跡へ降格（`regex`） | 型（class / interface / struct / enum / record）・メソッド・コンストラクタ・参照・ using、構造チャンク（tree-sitter のみ） |
| JavaScript | 同上（`tree-sitter` / `tree-sitter-partial` / `regex`） | class・function・メソッド・代入関数（`const x = () => {}` 等）・テストブロック（`describe` / `it` / `test` のラベル。`is_test`）・参照・ import（`require(...)` は regex のみ）、構造チャンク（tree-sitter のみ） |
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
依存 pin が `semantic` extra と衝突するため）。文法は `code-python` / `code-csharp` のような言語別 extra で
個別に導入できる（一覧は [pyproject.toml](../../../pyproject.toml) の `[project.optional-dependencies]`）。
`watchdog`（`cq watch`）と `tiktoken`（正確なトークン計上）は `code-watch` / `code-tokenizer` として `cq` 側に
宣言されており、`mdq` の extra を借りない。

解析に失敗したファイルも `lite` へ自動降格し、索引からは落とさない。降格したことは応答の `parser`
フィールドに必ず現れるので、**フィデリティが落ちた結果を全文と誤認しないこと**。tree-sitter 系言語
（Java / Go / Rust / C / C++ / Scala / shell / PowerShell / Windows batch）は、文法が `ERROR` ノードから
部分的に回復した場合、`parser` が `tree-sitter` ではなく **`tree-sitter-partial`** になる。この値は文法が
インストールされていないときの `lite` への降格とは異なり、**解析自体は成功しているが該当ファイル内の
一部の定義が欠落・不正確な可能性がある**ことを示す。

## 連携と参照

- `cq trace` は設計文書の本文を返さない。返されたパス/アンカーの本文が必要なら `markdown-query` へ渡す。
- `watch` を除く CLI 実行は `<repo-root>/.cq/usage.jsonl` へ best-effort で記録される。`.mdq/usage.jsonl` とは別で、混在させない。
- HVE 固有の profile、値入り例、過去測定、索引運用は、存在する HVE 統合環境だけ [references/repo-specific/hve-integration.md](references/repo-specific/hve-integration.md) を参照し、他環境では不要。過去測定は本リポジトリ・計測時点限定で、他環境へ精密値として外挿しない。
