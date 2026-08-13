# markdown-query

リポジトリ内の Markdown 群をローカル完結で横断検索し、ヒットした **見出し単位の小さな snippet だけ** をエージェントに返す Skill です。外部 API を呼びません。

- エンジン: `mdq`（Python、SQLite FTS5）
- 索引の置き場所: 導入先リポジトリの `.mdq/`
- 索引対象: `.md` と、`mdq.toml` の `[index].tabular` に列挙した CSV / TSV

> ソースコードの検索は対象外です。`.py` / `.ts` などは [code-query](code-query.md) が担当します。

---

## 1. なぜ使うのか

関連 Markdown を丸ごと Context に投入すると、Context Window を大量に消費し、コスト・速度・回答品質すべてに影響します。`markdown-query` は該当チャンクだけを返すので、投入トークンが 1〜3% 程度まで圧縮されます（削減率はリポジトリとクエリに依存します。自分のリポジトリでの実測はキット同梱の `benchmark.py` で取得してください）。

## 2. インストール

キットをリポジトリへ配置してからインストーラを実行します。手順はリポジトリルートの [README](../README.md#2-インストール) と共通です。

```powershell
# Windows
pwsh -NoLogo -NoProfile -File tools\kits\markdown-query\install.ps1
```

```bash
# macOS / Linux
bash tools/kits/markdown-query/install.sh
```

主なオプション:

| オプション | 意味 |
|---|---|
| `-WithGui` / `--with-gui` | 設定 GUI（PySide6）も導入する |
| `-WithWatch` / `--with-watch` | ファイル監視（watchdog）による増分索引を導入する |
| `-WithTokenizer` / `--with-tokenizer` | `tiktoken` を導入する（`markdown-query` は既定依存に含むため、このキットでは指定しなくても入る） |
| `-NoIndex` / `--no-index` | 初回索引を省略する |
| `-NoSkill` / `--no-skill` | `.github/skills/markdown-query/` への配置を省略する |
| `-Force` / `--force` | 既存の `mdq.toml` / Skill 定義を再生成する |
| `-RepoRoot` / `--repo-root` | 導入先リポジトリのルート（既定: カレントディレクトリ） |
| `-SkipPrereq` / `--skip-prereq` | Python / git の自動導入を行わない |

導入されるもの:

| パス | 内容 |
|---|---|
| `<repo>/mdq.toml` | 索引対象の設定。既存なら温存され、`--force` で再生成 |
| `<repo>/.github/skills/markdown-query/` | Skill 定義（エージェントが読む） |
| `<repo>/.mdq/` | SQLite 索引と利用ログ |
| `<kit>/.venv-mdq-gui/` | 依存を隔離した venv |

## 3. 設定（`mdq.toml`）

インストーラが実ファイル構成を走査して生成します。生成後は必ず内容を確認してください。

```toml
[index]
roots = [
    "docs",
    "tools",
]
exclude = [
    "tools/kits/markdown-query/**",
]
```

| キー | 意味 |
|---|---|
| `[index].roots` | 走査するディレクトリ（リポジトリルート相対）。ここに無いディレクトリは索引されない |
| `[index].exclude` | 除外する glob。`*` はパス区切りを跨がず、`**` は任意段数に一致する |
| `[index].tabular` | 行単位で索引する CSV / TSV の glob。既定は空 |

`.git` / `node_modules` / `__pycache__` / `dist` / `build` / `.venv*` などの依存・生成物ディレクトリは、設定に関係なく常に除外されます。ただし `roots` で明示したディレクトリ自体は除外されません（`roots = ["build"]` は有効）。

## 4. 使い方

`<kit>` はキットを配置したディレクトリ（例: `tools/kits/markdown-query`）です。

```powershell
<kit>\mdq.ps1 index                                    # 索引の作成・更新（増分）
<kit>\mdq.ps1 search --q "デプロイ手順" --top-k 5      # 検索
<kit>\mdq.ps1 get --chunk-id <ID>                      # snippet で足りないときだけ本文を取る
<kit>\mdq.ps1 list                                     # 見出しの俯瞰
<kit>\mdq.ps1 stats                                    # 索引の規模
<kit>\mdq.ps1 watch                                    # 変更を監視して増分索引（--with-watch 必要）
```

```bash
bash <kit>/mdq.sh index
bash <kit>/mdq.sh search --q "デプロイ手順" --top-k 5
```

`search` の主なオプション:

| オプション | 説明 |
|---|---|
| `--q` | 検索クエリ（必須） |
| `--top-k` | 返すヒット数（推奨 3〜5） |
| `--max-tokens` | 出力の最大トークン数（推奨 400〜800） |
| `--paths` | 対象パスの絞り込み（例: `"docs/**"`） |
| `--tags` | frontmatter のタグで絞り込み |
| `--mode` | `bm25`（既定） / `grep` |
| `--return-unit` | `line`（既定、±2 行の snippet） / `chunk`（見出しセクション全体） / `locations`（位置情報のみ） |
| `--strategy` | Chunking Strategy。`search` は `auto` が既定 |
| `--lang` | `ja-jp`（既定） / `en-us` |

出力は JSONL（1 行 = 1 ヒット）です。

### Chunking Strategy

索引 DB は `(lang, strategy)` の組み合わせごとに `.mdq/index-<lang>-<strategy>.sqlite` として分かれるため、複数 Strategy を並行運用できます。

| 戦略 | 境界 | 任意依存 |
|---|---|---|
| `heading`（`index` の既定） | Markdown 見出しごとに 1 chunk | なし |
| `heading_recursive` | 大きい見出しチャンクを段落／行で再分割 | なし |
| `fixed_window` | 見出しを無視した固定窓スライド | なし |
| `semantic_paragraph` | 文 embedding の類似度で意味境界を決める | `fastembed` ほか |
| `pageindex` | 見出しツリー索引（各ノードにサマリ） | なし |
| `auto`（`search` の既定） | クエリ内容から自動選択し、不在なら実在する DB へフォールバック | — |

`--strategy graphrag` も選択できますが、**別系統** です。SQLite 索引も BM25 も使わず、citation（`chunk_id` / `path` / `lines`）を返さず、LLM 生成の回答文字列を返します。`auto` の候補にも含まれません（明示指定のみ）。詳細はキットの `skill/references/graphrag-strategy.md` を参照してください。

## 5. エージェントから使う

インストーラが `.github/skills/markdown-query/SKILL.md` を配置します。GitHub Copilot はこれを読み込み、Markdown 由来の質問で自動的に本 Skill を選びます。

採用率を上げるには、リポジトリ最上位のエージェント共通ルール（`.github/copilot-instructions.md` / `CLAUDE.md` / `AGENTS.md` など）に優先順位を明記してください。

```markdown
- Markdown ファイル群を対象とした検索・横断クエリは、まず markdown-query Skill を試す。
  0 ヒットまたは目的が一致しない場合に限り grep / read_file へフォールバックする。
- ソースコードの検索は code-query、Markdown の編集・生成は本 Skill の対象外。
```

## 6. トラブルシューティング

| 症状 | 原因 | 対処 |
|---|---|---|
| `No module named mdq` | エンジンはキットの `vendor/` にあり、システムの Python からは見えない | `<kit>/mdq.ps1` / `<kit>/mdq.sh` を使う |
| `vendor/mdq is missing` | `vendor/` を含めずにキットをコピーした | キットのディレクトリごとコピーし直す |
| 検索が 0 件 | 索引が無い、または `roots` に対象が入っていない | `stats` で規模を確認し、`mdq.toml` の `roots` を見直して `index` を再実行 |
| キットや依存物が索引されている | `mdq.toml` の `exclude` が不足 | キットのパスを `exclude` に追加して `index --rebuild` |
| 索引 DB が壊れた | — | `.mdq/index-<lang>-<strategy>.sqlite` を削除して `index` をやり直す |

`.mdq/` は `.gitignore` に追加してください。`.mdq/usage.jsonl` には検索クエリがそのまま記録されます。

## 7. さらに詳しく

キット同梱のドキュメント（配布物なので、キットをコピーした先にも付いてきます）:

| ファイル | 内容 |
|---|---|
| [`markdown-query/GETTING-STARTED.md`](../markdown-query/GETTING-STARTED.md) | キット単体での導入手順と既知の制約 |
| [`markdown-query/docs/skills-markdown-query.md`](../markdown-query/docs/skills-markdown-query.md) | 技術アーキテクチャ・索引データモデル・利用統計指標 |
| [`markdown-query/skill/SKILL.md`](../markdown-query/skill/SKILL.md) | Skill 定義の正本 |
| [`markdown-query/skill/references/`](../markdown-query/skill/references/) | CLI リファレンス、クエリルーティング、索引内部仕様 |
| [`markdown-query/README.md`](../markdown-query/README.md) | ベンチマーク（トークン削減率の実測）の使い方 |
| [`markdown-query/USAGE.md`](../markdown-query/USAGE.md) | GUI 設定画面の操作 |

> 同梱ドキュメントの一部のリンクは上流リポジトリのパスを指しており、そのリンク先は上流リポジトリでのみ解決します。
