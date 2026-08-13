# 索引の内部構造

`cq` は 1 profile につき 1 つの SQLite DB（`.cq/index-<profile>.sqlite`）を持つ。
`mdq` とは **DB を共有しない**。同一コーパスに Markdown とコードを混ぜると IDF が汚染され、
実測で `docs/catalog` の top-1 が 22.2% → 16.7% へ低下したため。

## 4 層構成

| 層 | テーブル | 索引方式 | 用途 |
|---|---|---|---|
| L0 | `symbols` | 通常索引 | 定義の直接引き（`cq def`、symbol 経路） |
| L1 | `chunks_tri` | FTS5 `tokenize='trigram' detail=none` | 部分一致・正規表現の前段絞り込み |
| L2 | `chunks_fts` | FTS5 `unicode61 tokenchars '_$'` `detail=column` | BM25 による自然文検索 |
| L3 | `refs` / `imports` / `traces` | 通常索引 | 呼び出し元・依存・設計文書との対応（`cq refs` / `cq trace` / `cq map`） |

`files` テーブルが全層の親で、`sha1` と `mtime` / `size` による増分判定と `parser`（`ast` / `tree-sitter` / `sql` / `regex` / `lite`）の記録を持つ。

**`chunks_fts` の索引列は `name` / `signature` / `ident_text` / `text` の 4 列だけで、リポジトリ相対パスを含まない。**
そのためファイル名（pytest の失敗出力に現れるテストモジュール名など）は L0〜L2 のどの層でも引けない。
全層が 0 件のときだけ、`files` を `path LIKE` で絞り込んで `chunks` へ join する **path 層** を試す（`route` は `path`）。
絞り込みを `files`（実測 830 行）側で行うのは、`chunks`（実測 14,169 行）を走査すると同じ結果に 6〜17 倍の時間がかかるため。

スキーマは `SCHEMA_VERSION = 3`。旧バージョンの DB を見つけたら fail-closed で拒否し、`python -m cq index --rebuild` を要求する。

## チャンク分割（cAST）

arXiv:2506.15655 の split-then-merge を、**構造チャンカを登録した言語にだけ** 適用する（Python、tree-sitter/tree-sitter 系 13 言語（Java/Go/Rust/C/C++/shell/PowerShell/Windows batch/Scala/C#/JavaScript/TypeScript/tsx）、SQL）。

1. 構文木を辿り、上限文字数を超えるノードは再帰的に分割する。SQL だけは文単位をチャンクの最小単位とする。
2. 上限内に収まる兄弟ノードはマージする。
3. **ただし名前付き定義は必ず新しいチャンクを開始する**。マージすると「どの関数がヒットしたか」が曖昧になるため。
4. 言語モジュールが返した span は core がファイル内へ clamp し重複を除去するので、チャンクは必ず行の分割になる。言語側が重複した span を返すのは想定内で（例: Scala の式本体 `def`）、正規化は core の責任。
5. 解析失敗時（文法が未導入、または解析エラー）は行ウィンドウで分割する。シンボル表は言語別の抽出器が作るため、チャンク境界が行単位でも `cq def` / symbol 経路は利用できる。

各チャンクは `ident_text` 列に識別子を語分割した文字列を持つ（`getUserProfile` → `get user profile`）。
索引肥大を避けるため、**複数語に割れる識別子だけ**を格納する。

## 正規表現検索の 2 段構え

Russ Cox の trigram 方式。

1. パターンから最長のリテラル部分列を取り出し、`chunks_tri` で候補チャンクを絞る。
2. 候補に対してのみ Python の `re` で確定する。
3. 候補が `--regex-max-candidates`（既定 500）を超えたら打ち切り、打ち切ったことを報告する。

リテラルを含まないパターン（`^\w+$` など）は前段が効かないため遅い。避けるか `--paths` で範囲を絞る。

## ランキング

- BM25 は SQLite 内で `ORDER BY rank` により完結させる。Python 側へ全チャンクをロードしない（`mdq` の方式は踏襲しない）。
- **BM25 の既定は語の連言（暗黙 AND）**。語数が増えるほど 0 件になりやすいため、0 件のときだけ
  選言（` OR ` 連結）で 1 回再試行する。緩和ヒットは `match` が `or-fallback` になる。
  選言は連言より遅い（実測: 4 語 24.8 ms / 10 語 117.9 ms、連言は 0.16 ms）が、0 件時しか実行されない。
  CJK（仮名・漢字・ハングル）を含むクエリは対象外。字句検索では緩和しても正しい着地点へ到達できず、
  誤った上位ヒットを増やすだけになるため。
- 実装をテストより上位に出すため、テストパスに 1 のフラグを立てて第 2 ソートキーにする。
  この判定は `cq/discovery.py::test_path_sql()` に**単一定義**され、検索（symbol / trace / substr / path）と俯瞰マップが共有する。
- 俯瞰マップのスコアは「他ファイルからの被参照数 ÷ 同名の定義数」。
  自ファイル内の参照は数えない。名前衝突（`get` / `run` など）で汎用名が上位を占めるのを防ぐため。

## 既知の制約

- `chunks_fts` は `detail=column` のため **フレーズクエリ（引用符）が使えない**。検索層でサニタイズしている。
- trigram 索引は原本の 64%、DB 全体では原本の 372%（profile=hve 実測）。
- 日本語の自然文から英語識別子への意味的な橋渡しは行わない。
