# `code-query` — 日常運用ガイド

導入手順は [`README.md`](./README.md)。本ファイルは導入後の使い方だけを扱う。

## 独立管理画面

別リポジトリを GUI で管理する場合は、セットアップ時に GUI 依存を導入してから
対象リポジトリを引数で渡す。

```powershell
pwsh.exe -NoLogo -NoProfile -File setup.ps1 --repo-root D:\work\my-repo --with-gui
pwsh.exe -NoLogo -NoProfile -File launch-gui.ps1 D:\work\my-repo
```

```bash
bash setup.sh --repo-root /work/my-repo --with-gui
bash launch-gui.sh /work/my-repo
```

HVE リポジトリでは `hve/.settings.txt`、それ以外では対象リポジトリの
`.cq-gui-settings.txt` に GUI 設定を保存する。`cq.toml` は索引対象の唯一の情報源で、
管理画面から roots / exclude / max file size は書き換えない。
`.cq-gui-settings.txt` は利用者ローカルの状態なので、対象リポジトリの `.gitignore` へ追加する。

以下の例は `CQ_PROFILE` を設定済みとして `--profile` を省略する。

```powershell
$env:CQ_PROFILE = "main"      # PowerShell
```

```bash
export CQ_PROFILE=main        # bash
```

`cq.ps1` / `cq.sh` は `--profile` が指定されていないときだけ `CQ_PROFILE` を補う。
`python -m cq` を直接叩く場合は補完されないので、毎回 `--profile` を書くこと。

---

## 1. まずこれだけ

```powershell
.\cq.ps1 stats                       # 索引はあるか、規模はどれくらいか
.\cq.ps1 index                       # 増分索引（初回・新規ファイル追加後）
.\cq.ps1 search --q "<探したい語>"     # 既定 --mode auto / --top-k 5 / --max-tokens 800
```

`stats` の `by_parser` に `lite` が多い言語は、定義行しか取れていない（正規表現ベースの降格パーサ）。

---

## 2. 目的別コマンド

| やりたいこと | コマンド |
|---|---|
| 定義の場所を知りたい | `.\cq.ps1 def --symbol "Class.method"` |
| 呼び出し元を知りたい | `.\cq.ps1 refs --symbol "<name>"` |
| snippet では足りず本文が要る | `.\cq.ps1 get --chunk-id <search が返した ID>` |
| 正規表現で探す | `.\cq.ps1 search --re "async\s+Task<\w+>" --paths "src/api/*"` |
| 要件 ID / テスト ID からコードへ | `.\cq.ps1 trace --id FR-XXX-01` |
| コードから参照している設計文書へ | `.\cq.ps1 trace --by-path src/api/Foo.cs` |
| 知らないモジュールを俯瞰する | `.\cq.ps1 map --paths "src/*" --max-tokens 1200` |
| 編集を即座に索引へ反映する | `.\cq.ps1 watch`（`watchdog` 必須） |

---

## 3. 検索モードの選び方

既定 `--mode auto` が以下を判定する。通常は指定不要。

| 優先 | 条件 | 経路 |
|---|---|---|
| 1 | `--re` を指定した | `regex` |
| 2 | `FR-…` / `NFR-…` / `UT-…` / `TEST-…` / `APP-\d{3}` / `SVC-\d{2}` / `UC-\d+` に完全一致 | `trace` |
| 3 | 識別子または `Module.Class.method` 形式に完全一致 | `symbol` |
| 4 | 記号を含む 3 文字以上の文字列 | `substr` |
| 5 | 上記以外（自然文・複数語） | `bm25` |

0 件のときは自動でフォールバックする（`--mode` を明示した場合はしない）。
どの経路が採用されたかは各ヒットの `route` フィールドで分かる。

`IT-` / `E2E-` / `C-` 形式の ID は `auto` の trace 判定に含まれないため `--mode trace` を明示する。

---

## 4. 出力の読み方

```json
{"path": "src/lib/greeter.py", "lines": [5, 7], "route": "symbol", "score": 1.0,
 "snippet": "...", "parser": "ast", "chunk_id": "3f18d4e0...",
 "qualname": "Greeter.greet_user", "kind": "method",
 "signature": "def greet_user(self, name: str) -> str", "match": "qualname"}
```

| フィールド | 見るべき理由 |
|---|---|
| `route` | 期待した経路で引けたか。`bm25` に落ちていれば語彙がずれている |
| `parser` | `lite` なら定義行しか無い。全文解析済みと誤認しない |
| `match` | `name-fallback` は qualname 不一致での再探索結果（`score` 0.5）。別ファイルの同名関数の可能性がある |
| `chunk_id` | `cq get` に渡して本文を取るためのキー |
| `truncated` | regex 経路で候補数が `--regex-max-candidates` を超えて打ち切られた印 |

出力は原則 JSONL（1 行 1 レコード）。例外は `cq map --format text`（既定）と
`cq get`（`# path:start-end` ヘッダ + 生の本文）。

最終行に次が出たら結果は古い可能性がある。

```json
{"warning":"stale","changed":123,"hint":"run `python -m cq index` to refresh the index"}
```

---

## 5. 索引の鮮度

- `cq search` は応答前に、索引済みファイルの `size` / `mtime` だけを突合する。
- 差分が `--auto-reindex-limit`（既定 50）以下なら、その場で該当ファイルだけ再索引してから答える。
- 超過したら結果を返しつつ `stale` 警告を出す。`cq index` を実行する。
- **新規追加ファイルはこの経路では検知されない**（索引済みパスしか見ないため）。
  新しいファイルを作ったら `cq index` を実行するか `cq watch` を併走させる。
- `--auto-reindex-limit -1` で鮮度チェック自体を無効化できる（大量編集中の一時的な高速化用）。

---

## 6. Context を絞るオプション

| オプション | 既定 | 効果 |
|---|---|---|
| `--top-k` | 5 | 返すヒット数 |
| `--max-tokens` | 800 | 応答全体の上限。超える分は打ち切る（先頭 1 件は必ず返す） |
| `--snippet-radius` | 2 | ヒット行の前後行数 |
| `--paths` | — | リポジトリパスへの GLOB フィルタ |
| `--regex-max-candidates` | 500 | regex 前段の候補上限 |

トークン見積りは snippet 長 ÷ 4 の概算で、`tiktoken` は使わない。

---

## 7. 索引の場所と後片付け

| 対象 | パス | 扱い |
|---|---|---|
| 索引 DB | `<repo>/.cq/index-<profile>.sqlite` | 再生成可能。`.gitignore` に `.cq/` を追加する |
| 任意依存の venv | `tools/code-query/.venv-cq/` | 再生成可能 |
| vendor エンジン | `tools/code-query/vendor/cq/` | 生成物。上流から `sync-vendor` で再取得する |

DB が壊れた場合や索引スキーマが変わった場合:

```powershell
.\cq.ps1 index --rebuild
```

profile を増やしたい場合は `cq.toml` に `[profiles.<name>]` を追加し、その profile で `index` する。
既存の DB には影響しない。

---

## 8. 迷ったときの判断表

| 状況 | 使うもの |
|---|---|
| `.md` / ドキュメントを探している | `markdown-query`（`cq` は `.md` を索引しない） |
| 編集対象のファイルパスが既に分かっている | 普通にファイルを開く。`cq` を経由する必要はない |
| 大量に一致しそうな汎用名（`get` / `run` 等）を探している | `--paths` で範囲を絞る。`cq map` で被参照数上位から当たる |
| 日本語の説明文でコードを探したい | コード中に現れる英語の識別子を必ず混ぜる（意味的な言い換えは行わない） |
| 索引が壊れている気がする | `cq stats` で DB パスと規模を確認 → `cq index --rebuild` |
