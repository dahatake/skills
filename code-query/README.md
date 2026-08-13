# `code-query` Skill — 他リポジトリ導入キット

このディレクトリを他のリポジトリへ持ち込むと、`code-query` Skill
（ソースコード専用のローカル検索 CLI `cq`）をそのリポジトリで使えるようになる。

Markdown 側の対応物は `markdown-query` キット。両者は
**索引対象が排他**で、`.md` と CSV / TSV は `markdown-query`、ソースコードは `code-query` が担当する。

| 用途 | 入口 |
|---|---|
| 導入手順・制約・トラブルシューティング | 本ファイル |
| 日常運用のコマンド集 | [`USAGE.md`](./USAGE.md) |
| 索引エンジンの内部構造 | [`docs/skills-code-query.md`](./docs/skills-code-query.md) |
| Skill 定義の正本 | [`skill/SKILL.md`](./skill/SKILL.md) |

---

## 1. 前提条件

| 項目 | 要件 | 備考 |
|---|---|---|
| Python | 3.11 以上 | 標準ライブラリのみで動作する |
| git | 必須 | `git ls-files --cached --others --exclude-standard` でファイルを列挙するため |
| 対象リポジトリ | git 初期化済み | 未初期化だと索引時にエラー終了する |
| サードパーティ依存 | CLI は **不要** | 解析フィデリティ・GUI・監視・トークン計数はいずれも任意（§6）。未導入でも索引と検索は成功する |

> `cq` は SQLite（Python 標準の `sqlite3`）だけで索引を作る。埋め込みモデルも文法ファイルも
> ダウンロードしない。オフライン環境でそのまま動く。

---

## 2. クイックスタート

### Windows (PowerShell 7+)

```powershell
# 1) 上流リポジトリ内でエンジンを vendor/ へ展開する
pwsh -NoLogo -NoProfile -File sync-vendor.ps1

# 2) このディレクトリごと対象リポジトリへコピーする（例: tools/code-query/）
Copy-Item -Recurse . D:\work\my-repo\tools\code-query

# 3) 対象リポジトリで初期設定 + 初回索引
cd D:\work\my-repo\tools\code-query
pwsh -NoLogo -NoProfile -File setup.ps1 --repo-root D:\work\my-repo --profile main --build-index

# 4) 使う
.\cq.ps1 search --profile main --q "<探したい語>"
```

### Linux / macOS

```bash
bash sync-vendor.sh
cp -R . /work/my-repo/tools/code-query
cd /work/my-repo/tools/code-query
bash setup.sh --repo-root /work/my-repo --profile main --build-index
./cq.sh search --profile main --q "<探したい語>"
```

引数は **全 OS で共通**（`--repo-root` / `--profile` / `--build-index` 等）。
判断ロジックは `kit/kit_setup.py` の単一実装にあり、`setup.ps1` / `setup.sh` は
ブートストラップ用インタプリタの解決と引数転送だけを行う（FR-KIT-03）。
使えるオプションの全一覧は `setup.ps1 --help` / `setup.sh --help`。

`setup` は次を行う。既存ファイルは `--force` を付けない限り上書きしない。

1. 同梱された `vendor/cq/` の存在確認（欠落時は手順を示して fail-closed）
2. venv の作成と任意依存の導入（`--with-gui` / `--with-watch` / `--with-tokenizer`。`--no-venv` で省略）
3. `init_config.py` で対象リポジトリの `cq.toml` を生成
4. `--install-skill` 指定時に `<repo>/.github/skills/code-query/` を配置
5. `--build-index` 指定時に初回索引

### 独立管理画面

HVE GUI を起動せず、任意の別リポジトリを指定して profile、索引統計、差分更新、
完全再ビルド、DB 削除、リアルタイム更新設定、試し検索を管理できる。

```powershell
pwsh.exe -NoLogo -NoProfile -File setup.ps1 --repo-root D:\work\my-repo --with-gui
pwsh.exe -NoLogo -NoProfile -File launch-gui.ps1 D:\work\my-repo
```

```bash
bash setup.sh --repo-root /work/my-repo --with-gui
bash launch-gui.sh /work/my-repo
```

対象を省略した場合は起動時のカレントディレクトリを使用する。操作対象の絶対パスは
ウィンドウタイトルに表示されるため、別リポジトリを取り違えずに確認できる。

---

## 3. 同梱物

| ファイル / ディレクトリ | 役割 |
|---|---|
| [`setup.ps1`](./setup.ps1) / [`setup.sh`](./setup.sh) | 導入スクリプト（設定生成・任意依存・初回索引・Skill 配置） |
| [`sync-vendor.ps1`](./sync-vendor.ps1) / [`sync-vendor.sh`](./sync-vendor.sh) | 上流 `cq/` から `vendor/cq/` を再生成 |
| [`cq.ps1`](./cq.ps1) / [`cq.sh`](./cq.sh) / [`cq.cmd`](./cq.cmd) | `vendor/` を import パスへ通して `python -m cq` を実行するランチャ |
| [`launch-gui.ps1`](./launch-gui.ps1) / [`launch-gui.sh`](./launch-gui.sh) / [`launch-gui.cmd`](./launch-gui.cmd) | 対象リポジトリを指定して独立管理画面を起動するランチャ |
| [`launch.py`](./launch.py) | 配置先のディレクトリ名に依存せず `vendor/cq` の共有 GUI をロードする入口 |
| [`init_config.py`](./init_config.py) | 作業ツリーを走査して `cq.toml` を提案・生成 |
| [`cq.toml.sample`](./cq.toml.sample) | 手書きで調整するときの参照用テンプレート |
| [`skill/`](./skill/) | 対象リポジトリの `.github/skills/code-query/` へ配置する Skill 定義。`.github/skills/code-query/` を正本として `sync-vendor` が生成する（リポジトリ固有の付録は同梱しない） |
| [`vendor/README.md`](./vendor/README.md) | vendor の生成規約と同期手順 |
| `vendor/cq/` | 生成物。手で編集せず、**コミットする** |

### なぜ vendor をコミットするか

配布フォルダをコピーしただけで使えることが要件（FR-KIT-01 / FR-KIT-04）だからである。
エンジン実体を同梱しない場合、コピー先では上流リポジトリがないため `sync-vendor` を実行できず、
GUI も CLI も起動できない。

上流との乖離は
上流リポジトリの `hve/tests/test_cq_vendor_sync.py` が
配布対象の全ファイルを byte 単位で照合して検出する（`markdown_query` と同じ方式）。

---

## 4. `cq.toml` — 唯一の必須設定

`cq` には既定 roots が無い。設定が無ければ推測せずエラー終了する（誤ったツリーを索引して
「自信のある誤答」を返すことを防ぐため）。

```console
error: no cq configuration found under <repo>: declare profiles in one of cq.toml, .cq\config.toml
```

`init_config.py` は `git ls-files` の結果を走査し、索引可能な拡張子を含む
トップレベルディレクトリだけを roots として提案する。

```powershell
# 提案内容だけ見る（書き込まない）
python init_config.py --repo-root D:\work\my-repo --dry-run

# プロファイル名を指定して書き出す
python init_config.py --repo-root D:\work\my-repo --profile main
```

生成例:

```toml
[index]
max_file_bytes = 2097152

[profiles.main]
roots = ["src", "scripts"]
```

- 拡張子の allowlist は `cq.languages.LANGUAGE_BY_SUFFIX` を import して使うため、索引側と乖離しない。
- `node_modules` / `dist` / `build` / `.venv` などは roots 候補から除外する。
- **リポジトリ直下のファイルは索引できない**。roots は実在するサブディレクトリでなければならない。
- 複数のツリーを別々に検索したい場合は profile を分ける（索引 DB も分かれる）。

---

## 5. Agent への配線

導入しただけでは Agent は Skill を選ばない。次の 2 つを対象リポジトリで行う。

1. **Skill 定義を置く**
   `setup` に `--install-skill` を付けると
   `<repo>/.github/skills/code-query/SKILL.md` が `{{PROFILE}}` 置換済みで配置される。
2. **最上位ルールに優先順位を書く**
   `.github/copilot-instructions.md`（Claude Code なら `CLAUDE.md` 等）に次を追記する。

   ```markdown
   - ソースコードの「どこで定義されているか」「何が呼んでいるか」を調べるときは、
     まず `code-query`（`python -m cq search --profile main`）を試す。
     0 ヒット時、または編集対象ファイルが既知の場合に限り grep / ファイル読込へフォールバックする。
   - 対象が `.md` の場合は本 Skill を使わない（索引対象が排他）。
   ```

この 2 点が無いと採用率が上がらない。索引が存在しない場合 `cq` は 0 件ではなく
**エラーで停止**して `cq index` を案内するため、「黙って空振りして諦められる」ことはない。

---

## 6. 任意依存

索引と検索そのものに追加パッケージは要らない。以下は**あれば効く**もので、未導入でもコマンドは
成功する（該当機能だけが縮退する）。

### 6.1 解析フィデリティ

| パッケージ | 何が有効になるか | 未導入時の挙動 |
|---|---|---|
| `tree-sitter` と言語別文法（`tree-sitter-java` / `-go` / `-rust` / `-c` / `-cpp` / `-bash` / `-powershell` / `-batch` / `-scala`） | 対象言語の定義・参照・構造チャンクの抽出 | **その言語のファイルだけ** `lite`（正規表現で定義行のみ）へ降格。索引全体は成功する |
| `sqlglot` | `.sql` の `CREATE` 対象とテーブル参照の抽出、文単位の構造チャンク | `.sql` だけ `lite` へ降格 |
| `tree-sitter-sql` | PostgreSQL の `$tag$ ... $tag$` ルーチン本体に含まれる参照 | 本体が 1 トークン扱いのままになり、内側の参照を拾えない |
| `sqlfluff` | `sqlglot` が構造化できない Oracle PL/SQL・BigQuery スクリプトの本体 | 当該ファイルだけ `lite` へ降格 |

tree-sitter の文法は wheel に同梱されており、`sqlglot` / `sqlfluff` は pure Python である。いずれも
実行時に何もダウンロードしない（オフラインで動く）。
降格したかどうかは応答の `parser` フィールドに現れるので、`lite` の結果を全文解析の結果と
誤認しないこと。

### 6.2 ツール

| パッケージ | 何が有効になるか | 未導入時の挙動 |
|---|---|---|
| `PySide6` | 別リポジトリ対応の独立管理画面 | 起動時に `setup` の `--with-gui` を案内して exit 2 |
| `watchdog` | `cq watch`（保存を即座に索引へ反映） | `error: watching needs the optional 'watchdog' dependency` で exit 2 |
| `tiktoken` | `cq map` の予算計算と `cq.benchmark` の正確なトークン計数 | `chars/4-approx` の近似計数にフォールバックする。同じ `--max-tokens` でも `cq map` が落とす件数が変わる |

`setup` の `--with-gui` / `--with-watch` / `--with-tokenizer` が導入するのは §6.2 だけである。
§6.1 には対応する導入オプションが無いため、`setup` が作った `.venv-cq`（`--no-venv` 運用なら
使用中のインタプリタ）へ直接入れる。

```powershell
.venv-cq\Scripts\python.exe -m pip install "tree-sitter>=0.23" `
  "tree-sitter-java>=0.23" "tree-sitter-go>=0.25" "tree-sitter-rust>=0.24" `
  "tree-sitter-c>=0.24" "tree-sitter-cpp>=0.23" "tree-sitter-bash>=0.25" `
  "tree-sitter-powershell>=0.26" "tree-sitter-batch>=0.11" "tree-sitter-scala>=0.26" `
  "sqlglot>=30" "tree-sitter-sql>=0.3"
```

```bash
.venv-cq/bin/python -m pip install "tree-sitter>=0.23" \
  "tree-sitter-java>=0.23" "tree-sitter-go>=0.25" "tree-sitter-rust>=0.24" \
  "tree-sitter-c>=0.24" "tree-sitter-cpp>=0.23" "tree-sitter-bash>=0.25" \
  "tree-sitter-powershell>=0.26" "tree-sitter-batch>=0.11" "tree-sitter-scala>=0.26" \
  "sqlglot>=30" "tree-sitter-sql>=0.3"
```

入れた文法だけが高フィデリティになる。一部だけ入れても良いが、外した言語は `lite` のままになる。

`sqlfluff` だけは扱いが異なる。`click<8.4.0` を pin するため、`click>=8.4.2` を要求する
パッケージ（`huggingface-hub` 等）と同じ環境に入れると `pip check` が
`huggingface-hub ... has requirement click<9.0.0,>=8.4.2, but you have click 8.3.3.` で失敗する。
専用の `.venv-cq` にはそれらが居ないため通常は衝突しない。Oracle PL/SQL や BigQuery
スクリプトの本体まで構造化したい場合にだけ追加する。

```powershell
.venv-cq\Scripts\python.exe -m pip install "sqlfluff>=4.2"
```

```bash
.venv-cq/bin/python -m pip install "sqlfluff>=4.2"
```

**通常の検索・索引には §6.1 / §6.2 とも不要**。`cq search` は保存後の差分を自前の鮮度ガードで吸収する。

---

## 7. 既知の制約

| 制約 | 内容 | 回避策 |
|---|---|---|
| `--profile` の既定値が `hve` | 上流リポジトリ由来の既定値がそのまま残っている。他リポジトリでは毎回指定が必要 | `cq.ps1` / `cq.sh` を使い `CQ_PROFILE` を設定する。または profile 名を `hve` にする |
| ベンチマークの profile 名が固定 | `cq.benchmark` の `--profile` は `{hve,app}` に、`golden_eval` の検証も同じ 2 値に限定されている | 品質実測を行う場合のみ profile 名を `hve` または `app` にする。通常運用には影響しない |
| リポジトリ直下のファイル | roots はサブディレクトリのみ。直下のファイルは索引されない | 対象ファイルをサブディレクトリへ移すか、索引対象外と割り切る |
| 対応拡張子 | `.py` `.cs` `.js` `.mjs` `.cjs` `.jsx` `.ts` `.tsx` `.java` `.go` `.rs` `.c` `.cc` `.cpp` `.cxx` `.hpp` `.hh` `.h` `.sh` `.bash` `.ps1` `.psm1` `.cmd` `.bat` `.scala` `.sql` のみ | 上表以外は索引されない。`.md` / CSV / TSV は `markdown-query` の担当 |
| 新規ファイルの自動検知 | 検索時の鮮度ガードは索引済みパスしか `stat()` しない | 新規追加後は `cq index` を実行するか `cq watch` を併走させる |
| 構文ベースのチャンク分割 | 構造チャンクを持つのは Python（cAST）と tree-sitter / SQL 系の言語。C# / JS / TS はチャンク境界が行ウィンドウ | シンボル検索（`cq def` / symbol 経路）は全対応言語で利用できる |
| PL/pgSQL の手続き構文 | `IF` / `LOOP` / `PERFORM` は構造化されない。`$tag$` 本体の再パースで拾えるのは埋め込み SQL 文のテーブル参照まで | 手続きロジック自体を追う場合は本文を読む |
| PowerShell の定義数が環境で変わる | 文法の回復ノードが残ったファイルは `pwsh` の公式パーサへエスカレーションするため、`pwsh` の有無で抽出される定義数が変わる。`parser` 値はどちらも `tree-sitter` でエスカレーションの有無を区別できない | 結果を環境間で比較する場合は `pwsh` の有無を揃える |
| Windows batch に関数の概念が無い | 文法上、取れるのはラベル定義と `call` / コマンドの参照だけ | ラベル単位で追う |

---

## 8. トラブルシューティング

実際に出力されるメッセージで引けるようにしてある。

| 症状 | 原因 | 対処 |
|---|---|---|
| `vendor/cq is missing. The kit was copied without its engine` | `vendor/` を含めずにコピーした | キットのディレクトリごとコピーし直す |
| `error: no cq configuration found under <path>` | `cq.toml` が無い | `python init_config.py --repo-root <path> --profile <name>` |
| `error: unknown profile '<name>'` | `cq.toml` に無い profile を指定した | エラーが列挙する profile 名を使う |
| `error: cq index not found: ...\.cq\index-hve.sqlite` | `--profile` を省略して既定 `hve` が使われた | `--profile <name>` を付ける、または `CQ_PROFILE` を設定する |
| `error: cannot enumerate files under <path>: fatal: not a git repository` | 対象が git リポジトリでない | 対象で `git init` する |
| `error: watching needs the optional 'watchdog' dependency` | 任意依存が未導入 | `setup` に `--with-watch` を付けて再実行。`cq watch` は必須ではない |
| `error: cq index not found` が索引後も出る | 別の profile / 別の `--db` を見ている | `cq stats --profile <name>` で DB パスを確認する |
| 検索が 0 件 | クエリ語彙がコード上の識別子と一致していない | `--mode bm25` を明示、`cq map --paths "<dir>/*"` で俯瞰、それでも駄目なら grep へ |

---

## 9. 上流の更新を取り込む

```powershell
# 上流リポジトリ側で cq/ を更新したあと
pwsh -NoLogo -NoProfile -File sync-vendor.ps1
# 生成された vendor/cq/ をコミットし、配布先へコピーし直す
```

配布先のエンジンがどの版かは、同梱された `vendor/cq/` をコミットしている
リポジトリの履歴から特定できる。

索引スキーマが変わった場合は、対象リポジトリで次を実行する。

```powershell
.\cq.ps1 index --profile main --rebuild
```

---

## 10. 検証済みの動作範囲

本キットは、上流リポジトリの外に作った一時 git リポジトリで、
**pip パッケージを一切入れていない素の venv**（Python 3.14.6 / pip のみ）を使って
次のサブコマンドが動作することを実測で確認している。

`index` / `stats` / `search` / `def` / `refs` / `trace --id` / `trace --by-path` / `map`

`watch` のみ `watchdog` が必要で、未導入時は exit 2 で明示的に失敗する。
