# dahatake/skills

コーディングエージェントの **Context Window 消費を抑える** ためのツール集です。2 つのツールをそれぞれ独立して、自分のリポジトリへ導入できます。すべてローカル完結で動作し、外部 API を呼びません。

| ツール | 何をするか | 形態 | 詳細 |
|---|---|---|---|
| **markdown-query** | Markdown 群を横断検索し、ヒットした見出し単位の小さな snippet だけを返す | Skill（`.github/skills/` へ配置） | [docs/markdown-query.md](docs/markdown-query.md) |
| **code-query** | ソースコードを横断検索し、定義・参照・小さな snippet だけを返す | Skill（`.github/skills/` へ配置） | [docs/code-query.md](docs/code-query.md) |

`markdown-query` と `code-query` は索引対象が排他です。`.md` と CSV / TSV は `markdown-query`、ソースコードは `code-query` が担当します。

---

## 目次

1. [前提条件](#1-前提条件)
2. [インストール](#2-インストール)
3. [インストール後にやること](#3-インストール後にやること)
4. [動作確認](#4-動作確認)
5. [エージェントから使う](#5-エージェントから使う)
6. [更新と整合性の確認](#6-更新と整合性の確認)
7. [プラグインとしての配布（任意）](#7-プラグインとしての配布任意)
8. [リポジトリ構成](#8-リポジトリ構成)
9. [開発とメンテナンス](#9-開発とメンテナンス)

---

## 1. 前提条件

| 項目 | 要件 | 備考 |
|---|---|---|
| OS | Windows / macOS / Linux | — |
| シェル | PowerShell 7+（`pwsh`）または bash | 本文の Windows 向けコマンドは `pwsh` 前提。Windows PowerShell 5.1 しか無い場合は §2.4 を参照 |
| Python | 3.11 以上 | 未導入ならインストーラが winget / Homebrew / apt などで導入を試みる |
| git | 必須 | `code-query` は `git ls-files` で対象ファイルを列挙するため、導入先が git 管理下である必要がある |

導入先リポジトリは `git init` 済みにしておいてください。`code-query` はさらに **ソースコードが 1 つ以上存在する** ことを前提とします（ドキュメントしか無いリポジトリでは導入不要です）。

## 2. インストール

各ツールは自己完結した「キット」として配布されます。キットのディレクトリを自分のリポジトリへコピーし、その中のインストーラを実行するだけです。**必要なツールだけを入れられます**。

### 2.1 キットを取得する

```powershell
# Windows
git clone https://github.com/dahatake/skills.git $env:TEMP\dahatake-skills
```

```bash
# macOS / Linux
git clone https://github.com/dahatake/skills.git /tmp/dahatake-skills
```

### 2.2 自分のリポジトリへ配置する

キットは 1 つのディレクトリに集約しておくと管理しやすくなります（例では `tools/kits/`）。**必要なものだけ**をコピーしてください。

```powershell
# Windows。以下は 2 つとも入れる例
cd C:\path\to\your-repo
New-Item -ItemType Directory -Force -Path tools\kits | Out-Null
Copy-Item -Recurse "$env:TEMP\dahatake-skills\markdown-query" tools\kits\markdown-query
Copy-Item -Recurse "$env:TEMP\dahatake-skills\code-query"    tools\kits\code-query
```

```bash
# macOS / Linux
cd /path/to/your-repo
mkdir -p tools/kits
cp -r /tmp/dahatake-skills/markdown-query tools/kits/markdown-query
cp -r /tmp/dahatake-skills/code-query     tools/kits/code-query
```

> ディレクトリごとコピーしてください。`vendor/` を含めずにコピーするとエンジンが欠けて動作しません。

### 2.3 `.gitignore` に追記する

索引・venv・ログ・キャッシュはコミットしません。**次の `git add` より前に** 追記してください（`.gitignore` はすでに追跡されているファイルには効きません）。

```gitignore
.mdq/
.cq/
.venv-*/
__pycache__/
```

### 2.4 インストーラを実行する

**導入先リポジトリのルートで** 実行します。入れたいツールの分だけ実行してください。

```powershell
# Windows
pwsh -NoLogo -NoProfile -File tools\kits\markdown-query\install.ps1
pwsh -NoLogo -NoProfile -File tools\kits\code-query\install.ps1
```

```bash
# macOS / Linux
bash tools/kits/markdown-query/install.sh
bash tools/kits/code-query/install.sh
```

`pwsh` が無く Windows PowerShell 5.1 しか使えない場合は、同じスクリプトを次の形で実行します。

```powershell
powershell -NoLogo -NoProfile -ExecutionPolicy Bypass -File tools\kits\markdown-query\install.ps1
```

`code-query` はソースコードが 1 つも見つからないリポジトリではインストールに失敗します（何が足りないかはエラーメッセージに出ます）。ドキュメントだけのリポジトリでは導入不要です。

インストーラは Python 3.11+ と git を確認し（無ければ OS のパッケージマネージャで導入を試み）、キット内に venv を作り、依存を入れ、設定ファイルを生成し、Skill を配置し、初回索引まで行います。

> `code-query` は依存のダウンロード（tree-sitter 文法など数十 MB）を済ませてから設定を生成します。ソースコードが無いリポジトリではその後に失敗するので、先に導入の必要性を確認してください。

主なオプション（2 キット共通）:

| Windows | macOS / Linux | 意味 |
|---|---|---|
| `-RepoRoot <PATH>` | `--repo-root <PATH>` | 導入先リポジトリのルート（既定: カレントディレクトリ） |
| `-SkipPrereq` | `--skip-prereq` | Python / git の自動導入を行わない（すでに入っている環境向け） |
| `-WithGui` | `--with-gui` | 設定 GUI（PySide6）も導入する |
| `-WithWatch` | `--with-watch` | ファイル監視による増分索引を導入する |
| `-WithTokenizer` | `--with-tokenizer` | `tiktoken` を導入し、トークン計測を正確にする |
| `-NoIndex` | `--no-index` | 初回索引を省略する |
| `-NoSkill` | `--no-skill` | `.github/skills/` への配置を省略する |
| `-NoExtras` | `--no-extras` | 追加依存の導入を省略する（`code-query` の tree-sitter 文法） |
| `-Force` | `--force` | 既存の設定ファイル / Skill 定義を再生成する |
| `-NoVenv` | `--no-venv` | venv を作らず、指定の Python をそのまま使う（依存の導入も行わない） |
| `-Python <PATH>` | `--python <PATH>` | 使う Python を明示指定する |
| `-Version` | — | 導入済みの版を表示して終了する |
| `-Verify` | — | 同梱ファイルの改変・欠落を調べて終了する |

Python を自分で用意していて OS への導入を避けたい場合は `-SkipPrereq` / `--skip-prereq` を付けてください。Debian / Ubuntu で `python3-venv` が入っていない環境では、`--no-venv` を使うか `python3-venv` を先に導入してください。

### 2.5 何が作られるか

| パス | 作るツール | 内容 |
|---|---|---|
| `<repo>/mdq.toml` | markdown-query | 索引対象の設定 |
| `<repo>/cq.toml` | code-query | プロファイル（索引対象）の設定 |
| `<repo>/.github/skills/markdown-query/` | markdown-query | Skill 定義（エージェントが読む） |
| `<repo>/.github/skills/code-query/` | code-query | Skill 定義（エージェントが読む） |
| `<repo>/.mdq/` | markdown-query | SQLite 索引と利用ログ |
| `<repo>/.cq/` | code-query | SQLite 索引と利用ログ |
| `<kit>/.venv-*/` | すべて | 依存を隔離した venv |

## 3. インストール後にやること

### 3.1 生成された設定を確認する

インストーラは実ファイル構成を走査して設定を提案します。**索引したくないディレクトリが入っていないか必ず確認してください**。

```powershell
Get-Content mdq.toml
Get-Content cq.toml
```

`mdq.toml` の `[index].roots` / `[index].exclude`、`cq.toml` の `roots` / `exclude` を調整したら、索引を作り直します。

```powershell
tools\kits\markdown-query\mdq.ps1 index
tools\kits\code-query\cq.ps1 index
```

```bash
bash tools/kits/markdown-query/mdq.sh index
bash tools/kits/code-query/cq.sh index
```

> キット自身のディレクトリと venv は、生成される設定の対象外になります（キットが索引ルートの配下にある場合は `exclude` が生成され、そうでなければそもそもルートに入りません）。

## 4. 動作確認

`--q` には **自分のリポジトリに実際にある語** を入れてください。

```powershell
# Windows
tools\kits\markdown-query\mdq.ps1 stats
tools\kits\markdown-query\mdq.ps1 list
tools\kits\markdown-query\mdq.ps1 search --q "<ドキュメントにある語>" --top-k 3
tools\kits\code-query\cq.ps1 stats
tools\kits\code-query\cq.ps1 search --q "<関数名>"
```

```bash
# macOS / Linux
bash tools/kits/markdown-query/mdq.sh stats
bash tools/kits/markdown-query/mdq.sh list
bash tools/kits/markdown-query/mdq.sh search --q "<ドキュメントにある語>" --top-k 3
bash tools/kits/code-query/cq.sh stats
bash tools/kits/code-query/cq.sh search --q "<関数名>"
```

| 症状 | 見るところ |
|---|---|
| `stats` が `{"files": 0}` | 設定の `roots` に対象ディレクトリが入っていない。§3.1 を見直す |
| `stats` は非 0 なのに `search` が空（markdown-query） | 語が存在しないか、文書数が少なすぎて BM25 のスコアが 0 になっている。`mdq.ps1 list` で見出しを確認して語を選び直すか、`--mode grep` を試す |
| `stats` は非 0 なのに `search` が空（code-query） | `cq.ps1 map` で構成を確認し、`--mode substr` や `--mode bm25` を明示する |
| PowerShell がスクリプトを拒否する | 実行ポリシー。`.ps1` の代わりに同梱の `mdq.cmd` / `cq.cmd` を使うか、`powershell -ExecutionPolicy Bypass -File ...` で実行する |

> **素の `python -m mdq` / `python -m cq` は動きません。** エンジンはキットの `vendor/` にあり、システムの Python からは見えないためです。Skill 定義やエンジンのメッセージに `python -m mdq ...` と出てきたら、同じ引数を `mdq.ps1` / `mdq.sh`（`cq` なら `cq.ps1` / `cq.sh`）へ読み替えてください。

## 5. エージェントから使う

`markdown-query` と `code-query` は、インストール時に `.github/skills/<name>/SKILL.md` が配置され、GitHub Copilot がそれを読み込みます。索引さえできていれば、次のように依頼するだけで呼び出されます。

> このリポジトリの仕様書から「ポイント付与」の条件を探して。

> `resolve_run_id` を呼んでいる箇所を全部教えて。

採用率を上げるには、リポジトリ最上位のエージェント共通ルール（`.github/copilot-instructions.md` / `CLAUDE.md` / `AGENTS.md` など）へ優先順位を明記してください。

```markdown
- Markdown ファイル群を対象とした検索・横断クエリは、まず markdown-query Skill を試す。
- ソースコードの定義・参照・横断検索は、まず code-query Skill を試す。
- どちらも 0 ヒットまたは目的が一致しない場合に限り grep / read_file へフォールバックする。
- Markdown やコードの編集・生成は、いずれの Skill の対象外。
```

## 6. 更新と整合性の確認

導入済みキットの版と改変状況は、そのキットだけで確認できます。

```powershell
# Windows
pwsh -NoLogo -NoProfile -File tools\kits\markdown-query\install.ps1 -Version
pwsh -NoLogo -NoProfile -File tools\kits\markdown-query\install.ps1 -Verify
```

```bash
# macOS / Linux
python tools/kits/markdown-query/install.py --kit-dir tools/kits/markdown-query --version
python tools/kits/markdown-query/install.py --kit-dir tools/kits/markdown-query --verify
```

- `-Version` / `--version` は同梱の版・エンジン版・コピー元を表示します。
- `-Verify` / `--verify` は同梱ファイルのハッシュを照合し、改変・欠落を報告します。

更新するときは、このリポジトリを再取得し、**古いキットを削除してから** コピーし直してインストーラを再実行します。上書きコピーだと古いファイルが残り、ディレクトリが入れ子になります。

```powershell
# Windows
Remove-Item -Recurse -Force tools\kits\markdown-query
Copy-Item -Recurse "$env:TEMP\dahatake-skills\markdown-query" tools\kits\markdown-query
pwsh -NoLogo -NoProfile -File tools\kits\markdown-query\install.ps1
```

```bash
# macOS / Linux
rm -rf tools/kits/markdown-query
cp -r /tmp/dahatake-skills/markdown-query tools/kits/markdown-query
bash tools/kits/markdown-query/install.sh
```

生成済みの `mdq.toml` / `cq.toml` / Skill 定義は温存されます（`--force` を付けると再生成されます）。venv はキット内にあるため削除され、再実行時に作り直されます。

## 7. プラグインとしての配布（任意）

`markdown-query` / `code-query` の SKILL.md は、プラグインとしてエージェントへ配布することもできます。

```bash
# APM（複数ハーネス対応）
apm install dahatake/skills
```

```text
# GitHub Copilot CLI / Claude Code（対話プロンプト内）
/plugin marketplace add dahatake/skills
/plugin install dahatake-skills@dahatake-skills
```

```bash
# Gemini CLI
gemini extensions install https://github.com/dahatake/skills
```

> **この経路では SKILL.md しか配布されません。** CLI 本体（`mdq` / `cq`）は含まれないため、§2 のキット導入を別途行わないと動作しません。Gemini CLI 連携は未検証です。

## 8. リポジトリ構成

```
README.md                     本ファイル
docs/                         ツール別のドキュメント
  markdown-query.md
  code-query.md
markdown-query/               配布キット（Markdown 横断検索）
code-query/                   配布キット（ソースコード検索）
skills/                       プラグイン配布用の SKILL.md（生成物）
scripts/                      メンテナンス用スクリプト
sample/                       検索対象のサンプル Markdown
plugin.json                   プラグイン定義（Copilot CLI / 共通）
apm.yml                       APM マーケットプレイス定義
.claude-plugin/               Claude Code 用のプラグイン定義とマーケットプレイス
gemini-extension.json         Gemini CLI 拡張定義
.mcp.json                     MCP サーバー設定（現状は空）
.github/workflows/verify.yml  CI（キット整合性・生成物同期・スモークインストール）
```

各キットの構成は共通です。

```
<kit>/
  install.ps1 / install.sh / install.py   OS 別の入口と共通の導入判断
  kit/                                    2 キットで共有するセットアップ実装
  vendor/                                 エンジン本体（同梱）
  skill/                                  Skill 定義の正本
  docs/                                   技術ドキュメント
  KIT-VERSION.json                        同梱ファイルのハッシュと版情報
  kit.toml                                キット固有の設定（依存・venv 名など）
  <engine>.ps1 / <engine>.sh / <engine>.cmd   エンジン起動ラッパー
```

## 9. 開発とメンテナンス

このリポジトリ自体を変更する場合の手順です。

| 操作 | コマンド |
|---|---|
| キットのファイルを変更した後 | `python scripts/refresh-kit-manifest.py markdown-query code-query` |
| `<kit>/skill/` または `plugin.json` を変更した後 | `python scripts/sync-plugin-assets.py` |
| 生成物が最新かの確認（CI と同じ） | `python scripts/refresh-kit-manifest.py --check markdown-query code-query`<br>`python scripts/sync-plugin-assets.py --check` |

- `install.ps1` / `install.py` / `install.sh` / `kit/` は 2 キットでバイト同一に保ってください。一方を編集したら他方へコピーします。
- キットのファイルを変更したら必ず `KIT-VERSION.json` を再生成してください。しないと `install.py --verify` が「改変」を報告します。
- 各キットは上流リポジトリ（`dahatake/RoyalytyService2ndGen`）からコピーしたものです。本リポジトリで加えた差分は `KIT-VERSION.json` の `local_patches` に記録しています。
- `.gitattributes` は、シェルスクリプトを LF に固定し、ハッシュ検証対象のキットを改行変換の対象外にしています。変更する際はこの 2 点を壊さないでください。

## ライセンス

[MIT](LICENSE)
