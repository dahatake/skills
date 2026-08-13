# code-query

リポジトリのソースコードをローカル完結で横断検索し、定義・参照・小さな snippet だけをエージェントに返す Skill です。`markdown-query` のソースコード版で、**別パッケージ・別 DB** で動作します。

- エンジン: `cq`（Python、SQLite FTS5、任意で tree-sitter）
- 索引の置き場所: 導入先リポジトリの `.cq/`
- 索引対象: `cq.toml` の `roots` に列挙したディレクトリ配下のソースコード

> `.md` は索引しません。Markdown は [markdown-query](markdown-query.md) が担当します。

---

## 1. 前提条件

| 項目 | 要件 |
|---|---|
| Python | 3.11 以上 |
| git | **必須**。`git ls-files --cached --others --exclude-standard` でファイルを列挙するため、導入先が git 管理下（`git init` 済み）である必要がある |
| サードパーティ依存 | CLI 自体には不要。解析フィデリティ・GUI・監視・トークン計数はいずれも任意 |

`cq` は SQLite だけで索引を作ります。埋め込みモデルはダウンロードしません。

## 2. インストール

```powershell
# Windows
pwsh -NoLogo -NoProfile -File tools\kits\code-query\install.ps1
```

```bash
# macOS / Linux
bash tools/kits/code-query/install.sh
```

主なオプション:

| オプション | 意味 |
|---|---|
| `-WithGui` / `--with-gui` | 設定 GUI（PySide6）も導入する |
| `-WithWatch` / `--with-watch` | ファイル監視（watchdog）による増分索引を導入する |
| `-WithTokenizer` / `--with-tokenizer` | `tiktoken` を導入する |
| `-NoIndex` / `--no-index` | 初回索引を省略する |
| `-NoSkill` / `--no-skill` | `.github/skills/code-query/` への配置を省略する |
| `-NoExtras` / `--no-extras` | tree-sitter 文法の導入を省略する（§5 参照） |
| `-Force` / `--force` | 既存の `cq.toml` / Skill 定義を再生成する |
| `-RepoRoot` / `--repo-root` | 導入先リポジトリのルート（既定: カレントディレクトリ） |
| `-SkipPrereq` / `--skip-prereq` | Python / git の自動導入を行わない |

導入されるもの:

| パス | 内容 |
|---|---|
| `<repo>/cq.toml` | プロファイル設定。**これが無いと全コマンドが fail-closed で失敗する** |
| `<repo>/.github/skills/code-query/` | Skill 定義（エージェントが読む） |
| `<repo>/.cq/` | SQLite 索引と利用ログ |
| `<kit>/.venv-cq/` | 依存を隔離した venv |

## 3. 設定（`cq.toml`）

インストーラが実ファイル構成を走査して生成します。

```toml
[index]
max_file_bytes = 2097152

[profiles.main]
roots = ["src"]
exclude = [
    "tools/kits/code-query/**",
    "**/.venv*/**",
    "**/node_modules/**",
]
```

| キー | 意味 |
|---|---|
| `[index].max_file_bytes` | これを超えるファイルは生成物とみなして索引しない（既定 2 MiB） |
| `[profiles.<name>].roots` | 索引するディレクトリ。**既定値は無い**。`cq.toml` が無いと全コマンドが `error: no cq configuration found` で停まる（fail-closed。`--db` を明示した場合を除く） |
| `[profiles.<name>].exclude` | 除外 glob。`**/<dir>/**` は任意段数に一致する |

profile が 1 つだけなら既定として使われるので `--profile` は不要です。複数宣言した場合は `--profile <名>` を付けるか、環境変数で選びます。

```powershell
$env:CQ_PROFILE = "main"
```

```bash
export CQ_PROFILE=main
```

## 4. 使い方

`<kit>` はキットを配置したディレクトリ（例: `tools/kits/code-query`）です。

```powershell
<kit>\cq.ps1 index                              # 索引の作成・更新（増分）
<kit>\cq.ps1 search --q "resolve_run_id"        # 検索（既定 --mode auto）
<kit>\cq.ps1 def --symbol Class.method          # 定義へ直行
<kit>\cq.ps1 refs --symbol resolve_run_id       # 参照の列挙
<kit>\cq.ps1 trace --id FR-XX-01                # ID からコードへの追跡
<kit>\cq.ps1 get --chunk-id <ID>                # snippet で足りないときだけ本文を取る
<kit>\cq.ps1 map                                # リポジトリの俯瞰マップ
<kit>\cq.ps1 stats                              # 索引の規模・言語別フィデリティ
<kit>\cq.ps1 watch                              # 変更を監視して増分索引（--with-watch 必要）
```

```bash
bash <kit>/cq.sh index
bash <kit>/cq.sh search --q "resolve_run_id"
```

### 検索モード

既定は `--mode auto` で、クエリの形から自動判定します。

| 探しているもの | auto の判定 | 明示指定 |
|---|---|---|
| `FR-CQ-06` のような ID | trace | `--mode trace` |
| 関数名・クラス名・`Module.Class.method` | symbol | `--mode symbol` |
| 記号を含む部分文字列 | substr | `--mode substr` |
| 正規表現 | regex | `--re "<pattern>"` |
| 自然文・複数語 | bm25 | `--mode bm25` |

0 件のときは自動でフォールバックします。どの経路で引けたかは各ヒットの `route` フィールドで判別できます。関数・クラス単位で本文が欲しいときは `--return-unit chunk` を使います。

索引は変更されたファイルについて常に最新に保たれます（検索のたびに差分を突合し、50 件以下なら自動で再索引）。ただし **一度も索引されていない新規ファイルはこの対象外** なので、新規作成したファイルを引きたいときは `index` か `watch` が必要です。

## 5. 高フィデリティ言語対応

`install-extras.json` に列挙された tree-sitter 文法と `sqlglot` を、インストーラが既定で venv に導入します。対象は Java / Go / Rust / C / C++ / Bash / PowerShell / Batch / Scala / SQL です。Python は標準ライブラリの `ast` で常に高フィデリティに解析されます。

未導入でも索引は成立しますが、該当言語は regex ベースの `lite` へ降格し、**終了行・doc コメント・参照・構造チャンクを失います**（`stats` の `degraded` に計上）。wheel が無い環境では `-NoExtras` / `--no-extras` で省略できます。

## 6. エージェントから使う

インストーラが `.github/skills/code-query/SKILL.md` を配置します。エージェント共通ルールに次のような優先順位を書くと採用率が上がります。

```markdown
- ソースコードの定義・参照・横断検索は、まず code-query Skill を試す。
  0 ヒットまたは目的が一致しない場合に限り grep へフォールバックする。
- Markdown の検索は markdown-query、コードの編集は本 Skill の対象外。
```

## 7. トラブルシューティング

| 症状 | 原因 | 対処 |
|---|---|---|
| `No module named cq` | エンジンはキットの `vendor/` にあり、システムの Python からは見えない | `<kit>/cq.ps1` / `<kit>/cq.sh` を使う |
| `vendor/cq is missing` | `vendor/` を含めずにキットをコピーした | キットのディレクトリごとコピーし直す |
| `error: no cq configuration found` | `cq.toml` が無い | `python <kit>/init_config.py --repo-root . --profile main` |
| `error: unknown profile '<name>'` | `cq.toml` に無い profile を `index` に渡した | エラーが列挙する profile 名を使う |
| `error: cq index not found: ...` | その profile の索引が未作成 | `index` を実行する。profile 名も `stats` で確認する |
| `fatal: not a git repository` | 導入先が git 管理下でない | 導入先で `git init` する |
| `{"warning":"stale", ...}` が出る | 変更が索引反映の上限を超えた | `index` を実行するか `watch` を並走させる |
| 検索が 0 件 | 新規ファイルが未索引、または `roots` に入っていない | `stats` で規模を確認し、`cq.toml` を見直して `index` |

`.cq/` は `.gitignore` に追加してください。

## 8. さらに詳しく

| ファイル | 内容 |
|---|---|
| [`code-query/GETTING-STARTED.md`](../code-query/GETTING-STARTED.md) | キット単体での導入手順と既知の制約 |
| [`code-query/docs/skills-code-query.md`](../code-query/docs/skills-code-query.md) | 技術アーキテクチャ・チャンク分割・対応言語・詳細なトラブルシューティング |
| [`code-query/skill/SKILL.md`](../code-query/skill/SKILL.md) | Skill 定義の正本 |
| [`code-query/skill/references/`](../code-query/skill/references/) | CLI リファレンス、索引内部仕様 |
| [`code-query/README.md`](../code-query/README.md) | キットの構成 |
| [`code-query/USAGE.md`](../code-query/USAGE.md) | 日常運用の手引き |

> 同梱ドキュメントの一部のリンクは上流リポジトリのパスを指しており、そのリンク先は上流リポジトリでのみ解決します。
