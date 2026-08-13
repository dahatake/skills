# code-query — 他リポジトリでの導入手順

ローカル完結でソースコードを横断検索し、定義・参照・小さな snippet だけを返す Skill。
`markdown-query` のソースコード版で、**別パッケージ・別 DB** で動作する
（`.md` は `mdq`、ソースコードは `cq` という排他分担）。

このフォルダは上流リポジトリ（`dahatake/RoyalytyService2ndGen`）の
`tools/skills/code_query/` を `tools/for-other-repo/copy_to_repo.py` でコピーしたもの。
同梱の版情報は [`KIT-VERSION.json`](./KIT-VERSION.json) にある。

---

## 1. セットアップ（OS だけの状態から）

導入先リポジトリの**ルート**で実行する。`install.ps1` / `install.sh` が
Python 3.11+ と git を確認し、無ければ OS のパッケージマネージャで導入してから
venv 作成・依存インストール・`cq.toml` 生成・Skill 配置・初回索引まで行う。

> **git は必須**。`cq` は `git ls-files` の結果からファイルを列挙するため、
> 導入先が git 管理下でないと索引できない（`git init` 済みであること）。

### Windows

```pwsh
pwsh -NoLogo -NoProfile -File <このフォルダ>\install.ps1
```

### macOS / Linux

```bash
bash <このフォルダ>/install.sh
```

### 主なオプション

| オプション | 意味 |
|---|---|
| `-WithGui` / `--with-gui` | 設定 GUI（PySide6）も入れる |
| `-WithWatch` / `--with-watch` | ファイル監視（watchdog）による増分索引を入れる |
| `-WithTokenizer` / `--with-tokenizer` | `tiktoken` を入れてトークン計測を正確にする |
| `-NoIndex` / `--no-index` | 初回索引を省略する |
| `-NoSkill` / `--no-skill` | `.github/skills/code-query/` への配置を省略する |
| `-NoExtras` / `--no-extras` | tree-sitter 文法の導入を省略する（§4 参照） |
| `-Force` / `--force` | 既存の `cq.toml` / Skill 定義を再生成する |
| `-RepoRoot` / `--repo-root` | 導入先リポジトリのルート（既定: カレント） |

導入されるもの:

- `<repo>/cq.toml` — プロファイル（索引対象ルート）の設定。**これが無いと全コマンドが fail-closed で失敗する**
- `<repo>/.github/skills/code-query/` — Skill 定義（Copilot が読む）
- `<repo>/.cq/` — SQLite 索引（初回索引時に生成）
- `<このフォルダ>/.venv-cq/` — 依存を隔離した venv

`cq.toml` は導入先の実ファイル構成を走査して生成される。生成後に内容を確認し、
索引したくないディレクトリを `exclude` へ追加すること。

---

## 2. 使い方

コマンドは **導入先リポジトリのルートで** 実行する。cq はカレントディレクトリを
リポジトリルートとみなす。

```pwsh
# Windows
<kit>\cq.ps1 index
<kit>\cq.ps1 search --q "resolve_run_id"
<kit>\cq.ps1 stats
```

```bash
# macOS / Linux
bash <kit>/cq.sh index
bash <kit>/cq.sh search --q "resolve_run_id"
bash <kit>/cq.sh stats
```

`cq.toml` に profile が 1 つしか無い間はそれが既定になるので、profile の指定は不要。
profile を複数宣言したら `--profile <名>` を付けるか、環境変数で選ぶ。

```pwsh
$env:CQ_PROFILE = "main"
```

```bash
export CQ_PROFILE=main
```

GUI 設定画面:

```pwsh
pwsh -NoLogo -NoProfile -File launch-gui.ps1     # Windows
bash launch-gui.sh                               # macOS / Linux
```

---

## 3. ドキュメント

| ファイル | 内容 |
|---|---|
| [`docs/skills-code-query.md`](./docs/skills-code-query.md) | 技術アーキテクチャ / チャンク分割 / CLI / 対応言語 / トラブルシューティング（`users-guide` から同梱） |
| [`skill/SKILL.md`](./skill/SKILL.md) | Skill 仕様本体（`.github/skills/` へ配置される正本） |
| [`skill/references/`](./skill/references/) | CLI リファレンス・索引内部仕様 |
| [`README.md`](./README.md) | キットの構成 |
| [`USAGE.md`](./USAGE.md) | 日常運用の手引き |

> 同梱ドキュメント内のリンクの一部は上流リポジトリのパスを指す。
> そのリンク先は上流リポジトリでのみ解決する。

---

## 4. 高フィデリティ言語対応（既定で自動導入）

[`install-extras.json`](./install-extras.json) に列挙された tree-sitter 文法と `sqlglot` を
`install.ps1` / `install.sh` が venv へ自動で入れる。対象は Java / Go / Rust / C / C++ /
Bash / PowerShell / Batch / Scala / SQL。

未導入でも索引は成立する（該当言語だけ regex ベースの lite へ降格し、`degraded` に計上される）。
ただし lite では **終了行・doc コメント・参照・構造チャンクを失う**ため、配布先では既定で導入する。

wheel が無い環境では `-NoExtras` / `--no-extras` で省略できる。

---

## 5. 更新（版の同期）

現状確認はこのフォルダだけでできる。

```pwsh
python install.py --kit-dir . --version    # 導入済みの版
python install.py --kit-dir . --verify     # 同梱ファイルの改変・欠落
```

更新は、配布リポジトリ（`dahatake/skills`）を再取得し、**古いキットを削除してから**
コピーし直してインストーラを再実行する。上書きコピーだと古いファイルが残る。

```pwsh
Remove-Item -Recurse -Force <kit>
Copy-Item -Recurse <取得先>\code-query <kit>
pwsh -NoLogo -NoProfile -File <kit>\install.ps1
```

生成済みの `cq.toml` と Skill 定義は温存される（`--force` で再生成）。

---

## 6. 既知の制約

- `git` が必須。git 管理外のディレクトリは索引できない。
- 索引対象は `cq.toml` の `roots` に列挙したパスのみ。既定値は無い（fail-closed）。
- 2 MiB を超えるファイルは生成物とみなして索引しない（`max_file_bytes` で変更可）。
- `.md` は索引しない。Markdown は `markdown-query` の担当。
