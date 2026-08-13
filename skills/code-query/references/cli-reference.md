# `cq` CLI リファレンス

すべてのサブコマンドに共通のオプション:

| オプション | 既定 | 説明 |
|---|---|---|
| `--profile` | `hve` | `cq.toml` の `[profiles.<name>]` を選ぶ。索引 DB は `.cq/index-<profile>.sqlite` |
| `--repo-root` | カレント | リポジトリルート |
| `--db` | profile から導出 | 索引 DB のパスを直接指定する |

出力は原則 **JSONL**（1 行 1 レコード）で `jq` や逐次読みに向く。例外は `cq map --format text`（既定）と `cq get`（`# path:start-end` ヘッダ + 生の本文）。

---

## `cq index`

```sh
python -m cq index --profile hve            # 増分（SHA-1 + mtime 一致はスキップ）
python -m cq index --profile hve --rebuild  # 全再構築
```

- ファイル列挙は `git ls-files --cached --others --exclude-standard`。未追跡ファイルも索引するが `.gitignore` は尊重する。
- 除外は `cq.toml` の `exclude` と組み込み既定（`**/vendor/**`, `**/node_modules/**`, `*.min.js`, `.env*`, `*.pem`, `*.key`, `*.pfx`, `*.p12`, `id_rsa*`）。
- 2 MB 超のファイルはスキップ（`[index].max_file_bytes`）。
- ディスク上に無くなったファイルの行は削除される（prune）。
- 出力サマリ: `indexed` / `skipped` / `pruned` / `degraded` / `errors` / `symbols` / `chunks`。

## `cq stats`

```sh
python -m cq stats --profile app
```

`files` / `symbols` / `chunks` / `refs` / `imports` / `traces` / `by_parser` / `by_lang` / `schema_version` / `db` を返す。
`by_parser` に `lite` が多い場合、その言語は定義行しか取れていない。
`by_lang` は言語ごとの `files` / `symbols` / `chunks` / `by_parser`。**1 つのパーサ名を複数の言語が共有する**
（C# と Java はどちらも `tree-sitter`）ため、`by_parser` だけではどの言語のフィデリティが落ちたか判別できない。

## `cq search`

```sh
python -m cq search --profile hve --q "run_journal"
python -m cq search --profile hve --q "how does the orchestrator decide fan out"   # 自然文
python -m cq search --profile hve --q "test_search_recall"                          # パスで引く
python -m cq search --profile app --re "async\s+Task<\w+>" --paths "src/api/*"
```

| オプション | 既定 | 説明 |
|---|---|---|
| `--q` | — | 検索語 |
| `--re` | — | 正規表現（指定すると regex 経路に固定） |
| `--mode` | `auto` | `auto` / `trace` / `symbol` / `substr` / `regex` / `bm25` |
| `--top-k` | 5 | 返す件数 |
| `--max-tokens` | 800 | 応答全体のトークン上限。**1 ヒットごとの返却 JSON 全体**（抜粋 + メタデータ）の文字数 ÷ 4 で見積もる。超えた分は打ち切る（先頭 1 件は必ず返す） |
| `--snippet-radius` | 2 | ヒット行の前後何行を含めるか（`--return-unit line` のときのみ効く） |
| `--return-unit {line\|chunk}` | `line` | 抜粋の単位。`line` はヒット行 ±`--snippet-radius` 行、`chunk` はヒットを含む構造チャンク（関数・クラス等）の本文全体。`chunk` では `lines` も当該チャンクの行範囲へ広がる。単位を変えても `route` / `score` / 順位は変わらないが、抜粋が長い分だけ同じ `--max-tokens` で返る件数は減る |
| `--regex-max-candidates` | 500 | trigram 前段で絞った候補の上限。超えたら打ち切って報告する |
| `--paths` | — | リポジトリパスへの GLOB フィルタ |
| `--auto-reindex-limit` | 50 | 応答前に再索引する差分ファイル数の上限。`-1` で鮮度チェック自体を無効化 |

各ヒットの主なフィールド: `path` / `lines` / `qualname` / `kind` / `signature` / `snippet` / `parser` / `route` / `score` / `match` / `chunk_id`。
どの経路で引けたかは各ヒットの `route` フィールドで判別する（経路をまとめた要約行は出力されない）。

**0 件時の fallback は 2 段階**。

1. 自然文（`bm25`）が 0 件のとき、語の連言を選言へ 1 回だけ緩和して再試行する。緩和ヒットは `route` が `bm25` のままで
   `match` が **`or-fallback`** になる。全語を含むヒットではないので確度は低い。語が 1 つのときと
   CJK（仮名・漢字・ハングル）を含むクエリでは緩和しない。
2. すべての経路が 0 件のとき、リポジトリ相対パスの部分一致で引く（`route` が **`path`**）。ファイルごとに
   先頭チャンク 1 件を返す。並びはテストパスを後ろへ回し、次にパス長の昇順。`--mode path` は存在しない（連鎖専用）。

**鮮度**: 差分が `--auto-reindex-limit` を超えると、結果を返した上で最終行に
`{"warning":"stale","changed":N}` を出す。この行が出たら結果は古い可能性がある。
**一度も索引されていない新規ファイルはこの差分突合の対象外**で、警告にも現れない。
新規ファイルを引きたいときは `cq index` か `cq watch` を先に実行すること。

## `cq def`

```sh
python -m cq def --profile hve --symbol "StepRunner.set_fork_index"
python -m cq def --profile hve --symbol "resolve_run_id"
```

定義位置とシグネチャだけを返す（本文を含まない）。
ドット区切りの qualname が一致しない場合は**末尾の名前だけで再検索**し、`match` を `name-fallback`、
`score` を 0.5 にして返す。完全一致は `match` が `qualname`、`score` が 1.0。
`name-fallback` の結果は別ファイルの同名関数である可能性があるため、`qualname` を必ず確認すること。

## `cq get`

```sh
python -m cq get --profile hve --chunk-id 1234
```

`cq search` が返した `chunk_id` のチャンク本文を全文で返す。snippet で足りないときだけ使う。

## `cq refs`

```sh
python -m cq refs --profile hve --symbol "resolve_run_id"
```

そのシンボルを参照している箇所（ファイル・行）を返す。定義行そのものは含まない。

## `cq trace`

```sh
python -m cq trace --profile app --id TEST-SVC-02-001
python -m cq trace --profile app --by-path src/test/api/SVC-02.Tests/Svc02RedTests.cs
```

- `--id`: 要件 ID / テスト ID / `APP-\d{3}` / `SVC-\d{2}` / `UC-\d+` からコード位置を引く。
- `--by-path`: コードから、そのファイルが参照している設計文書のパスとアンカーを引く。

**設計文書の本文は返さない。** 本文が必要なら返ってきたパスとアンカーを
`python -m mdq search` / `python -m mdq get` へ渡す。

## `cq map`

```sh
python -m cq map --profile hve --paths "hve/gui/*" --max-tokens 1200
python -m cq map --profile app --max-tokens 400 --format json
```

被参照数でランク付けした俯瞰マップ。本文は出さず定義行だけを畳み込み表現（`⋮...`）で並べる。
予算を超えた分は下位から落とし、末尾に `# dropped N lower-ranked symbols to fit the token budget` を出す。
**予算は定義行だけでなく、ファイル見出し・折り畳み記号・空行・除外通知を含めた実出力全体**に対して判定する。
その分、同じ予算で掲載される件数は従前より少ない。`--format json` は別形式なので予算の対象外。
ランキングはテストコードを除外し、同名定義数で減衰させる（名前衝突で汎用名が上位を占めるのを防ぐため）。

## `cq watch`

```sh
python -m cq watch --profile hve --debounce-ms 300
```

`watchdog` でファイル変更を監視し、保存から 1 秒以内に索引へ反映する。
起動しなくても `cq search` の鮮度ガードが差分を吸収するので、**必須ではない**。
編集が頻繁で 50 件を超える差分が出やすい作業でだけ使う。

---

## 終了コードとエラー

| 状況 | 挙動 |
|---|---|
| `cq.toml` が無い | エラー終了し、設定例を案内（fail-closed。既定 roots で黙って走らない） |
| 索引が無い | エラー終了し `cq index` を案内（**0 件を返さない**） |
| スキーマ版が古い | エラー終了し `--rebuild` を案内 |
| 解析失敗ファイル | `lite` へ降格して継続。索引全体は失敗させない |
