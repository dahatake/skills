# dahatake/skills

コーディングエージェント向けの **スキル集** を提供するプラグインです。
GitHub Copilot CLI、Claude Code、Gemini CLI、および [APM](https://github.com/microsoft/apm) 対応の各種ハーネスから同じスキルを利用できます。

> **注意**: 各スキルは SKILL.md の配布のみで完結しないものがあります。例えば `markdown-query` は別途 `mdq` CLI のインストールが必要です（[`setup/` スクリプト](#setup-スクリプトでのスキル別追加導入) 参照）。プラグインを入れただけでは動作しない点にご注意ください。

## 提供スキル

| スキル | 概要 |
| --- | --- |
| [`markdown-query`](skills/markdown-query/SKILL.md) | ローカル完結で Markdown 群を横断検索し、ヒットしたチャンクのみを返して Context Window を節約します（BM25 / grep / タグ検索対応）。 |

---

## `markdown-query` スキル詳細

> 「初めてこのリポジトリを触る人」向けに、**何が用意されているのか / 何ができるのか / どういう場合に使うのか** を最初にまとめます。手順だけ知りたい方は [使用方法（`markdown-query`）](#使用方法markdown-query) まで読み飛ばして構いません。

### 何ができるのか（What）

`markdown-query` は、リポジトリ内の Markdown 群（仕様書、設計書、ナレッジ、README など）を **ローカル完結で横断検索** し、ヒットした **見出し単位の小さなチャンク（snippet）だけ** をエージェントに返すスキルです。

- **BM25 検索**: 自然言語クエリで関連度順に上位 N 件を返す。
- **grep 検索**: 完全一致 / 厳密一致したいキーワードに使う。
- **タグ / パス絞り込み**: frontmatter の `tags` や `docs/**` のような glob で範囲限定。
- **見出し階層の俯瞰**: `mdq list` でファイル横断の見出し一覧を取得。
- **本文取得**: `mdq get --chunk-id <ID>` で必要な箇所だけ完全な本文を取り出せる。

### なぜ使うのか（Why）

LLM ベースのコーディングエージェントでは、関連 Markdown を **丸ごと Context に投入すると Context Window を大量消費** し、コスト・速度・回答品質（noise）すべてに悪影響が出ます。

- 「全文を `read_file` させる」 → 数万トークン消費、関係ない章まで読まれる。
- `markdown-query` 経由 → 該当チャンク数百〜千トークン程度に圧縮されて投入される。

実測結果は [評価方法（ベンチマーク）](#評価方法ベンチマーク) を参照してください。

### どういう場合に使うのか（When）

- 複数の Markdown（仕様書群、ナレッジベース、複数 README）を **横断的に参照したい** とき。
- エージェントに「このリポジトリの仕様に従って実装して」と依頼する前に、**関連箇所だけを切り出して渡したい** とき。
- Context Window を節約しつつ、**引用元 (`path:lines`) を明示** したいとき。

逆に **使わないケース**:

- Markdown を編集・生成したい（このスキルは読み取り専用）。
- クラウド埋め込み / リモートベクトル検索を使いたい（本スキルは外部 API を呼びません）。
- 単一の小さな README を眺めたいだけ（普通に開いた方が速い）。

### スキルパッケージに含まれるもの

| パス | 内容 |
| --- | --- |
| [`skills/markdown-query/SKILL.md`](skills/markdown-query/SKILL.md) | スキル本体。エージェントが読み込むトリガー定義と手順サマリ。 |
| [`skills/markdown-query/references/cli-reference.md`](skills/markdown-query/references/cli-reference.md) | `mdq` CLI の全サブコマンド・全オプション仕様。 |
| [`skills/markdown-query/references/query-patterns.md`](skills/markdown-query/references/query-patterns.md) | よくあるクエリ例（タグ絞り込み、grep、見出し俯瞰など）。 |
| [`skills/markdown-query/references/indexing-internals.md`](skills/markdown-query/references/indexing-internals.md) | 索引のデータモデルとチャンク分割ルール（高度な利用者向け）。 |
| [`skills/markdown-query/examples/prompt-snippets.md`](skills/markdown-query/examples/prompt-snippets.md) | Copilot Chat / Custom Agent に組み込む際のプロンプト例。 |
| [`mdq/`](mdq/) | スキルが内部で呼び出す Python 製 CLI 本体。 |
| [`setup/setup-markdown-query.{ps1,sh}`](setup/) | `mdq` を `.venv` にインストールするセットアップスクリプト。 |
| [`tools/markdown-query/`](tools/markdown-query/) | Context 削減効果を数値で確認するためのベンチマーク CLI。 |

## インストール

### 前提条件

- Git CLI（全インストール経路で必須）
- Node.js 18+ （`npx skills` 経由でインストールする場合のみ必須）
- Python 3.11+（`markdown-query` の `mdq` CLI を利用する場合のみ必須）

各エージェント CLI（`copilot` / `claude` / `gemini` / `apm`）は事前にインストールしておいてください。

### APM（複数ハーネス対応）

[APM](https://github.com/microsoft/apm) を使うと、1 コマンドで複数のエージェント環境に導入できます。

```bash
apm install dahatake/skills
```

### GitHub Copilot CLI

`copilot` CLI のインタラクティブセッション内で次を実行します（初回のみマーケットプレイス追加）。

```text
/plugin marketplace add dahatake/skills
/plugin install dahatake-skills@dahatake-skills
```

シェルから直接実行する場合:

```bash
copilot plugin marketplace add dahatake/skills
copilot plugin install dahatake-skills@dahatake-skills
```

更新する場合:

```bash
copilot plugin update dahatake-skills
```

インストール確認:

```bash
copilot plugin list
```

### Claude Code

`claude` を起動した対話プロンプト内で次を実行します（初回のみマーケットプレイス追加）。

```text
/plugin marketplace add dahatake/skills
/plugin install dahatake-skills@dahatake-skills
```

インストール状況は `/plugin list` で確認できます。

### Gemini CLI

```bash
gemini extensions install https://github.com/dahatake/skills
```

更新する場合:

```bash
gemini extensions update dahatake-skills
```

> **Gemini CLI 連携は未検証です**。`gemini-extension.json` は最小構成（name / version / description）のみで提供しており、Gemini CLI 側で `skills/` 配下が自動認識されるかは環境により異なる可能性があります。動作確認できない場合は手動インストールまたは `setup/` スクリプトをご利用ください。

> **Codex CLI について**: 本レビュー時点（2026 年 5 月）の [openai/codex](https://github.com/openai/codex) には公式のプラグインマーケットプレイス機能はありません。Codex CLI から本リポジトリのスキルを使う場合は、下記「手動インストール」または `setup/` スクリプトをご利用ください。

### 手動インストール（GitHub Copilot 全般）

```bash
npx skills add https://github.com/dahatake/skills/tree/main/skills -a github-copilot -g -y
```

### `setup/` スクリプトでのスキル別追加導入

スキルによっては、CLI 等の追加コンポーネントが必要です。`setup/` 配下のスクリプトでローカル `.venv` に必要な CLI をインストールできます。

#### `markdown-query` スキル: `mdq` CLI

リポジトリをクローンしたうえで、OS に応じて次のいずれかを実行してください。

**Windows (PowerShell)**

```powershell
git clone https://github.com/dahatake/skills.git
cd skills
./setup/setup-markdown-query.ps1
```

主なオプション:

| オプション | 説明 |
| --- | --- |
| `-CheckOnly` | 変更を加えず、現状のみを確認する |
| `-ForceRecreateVenv` | `.venv` の Python が 3.11 未満なら作り直す |
| `-WithWatch` | `mdq watch` 用に `watchdog` も導入する |
| `-From <PATH>` | PyPI ではなくローカルソースから `pip install -e` する |

**macOS / Linux (bash)**

```bash
git clone https://github.com/dahatake/skills.git
cd skills
chmod +x ./setup/setup-markdown-query.sh
./setup/setup-markdown-query.sh
```

主なオプション:

| オプション | 説明 |
| --- | --- |
| `--check-only` | 変更を加えず、現状のみを確認する |
| `--force-recreate-venv` | `.venv` の Python が 3.11 未満なら作り直す |
| `--with-watch` | `mdq watch` 用に `watchdog` も導入する |
| `--from PATH` | PyPI ではなくローカルソースから `pip install -e` する |
| `-h`, `--help` | ヘルプを表示する |

> 前提: Python 3.11 以上。スクリプトはリポジトリルートに `.venv` を作成し、その中に `mdq` をインストールします。インストール後はシェルで `.venv` を有効化するか、`./.venv/bin/python -m mdq ...`（Windows は `./.venv/Scripts/python.exe -m mdq ...`）の形で呼び出してください（スクリプト末尾の "Next steps" と同じ形式）。

## 使用方法（`markdown-query`）

`markdown-query` は `mdq` CLI を内部で呼び出します。エージェントに依頼する場合も、手動で実行する場合も、**事前にインデックスを作成しておく必要があります**。

### 1. インデックスの作成（初回 / 必須）

`.mdq/index.sqlite` はセッション間で共有されない前提のため、**このスキルを使う前に必ず 1 回実行してください**。検索対象のリポジトリのルートで実行します。

```bash
mdq index
```

- 既定でカレントディレクトリを再帰走査し、`.md` / `.markdown` を索引化します。
- 既定除外ディレクトリ: `.git`, `node_modules`, `.venv`, `venv`, `__pycache__`, `.mdq`, `dist`, `build`, `.next`, `.cache`（名前一致で再帰走査時にプルーニングされます）。
- 索引対象を特定のディレクトリに絞りたい場合は `mdq index --root docs --root knowledge` のように `--root` を繰り返し指定します。
- `.mdq/` 自体を `.gitignore` に追加することを推奨します。

### 2. インデックスの更新

ファイルを追加・編集した後はインデックスの更新が必要です。

```bash
# 増分更新（SHA-1 + mtime が一致するファイルはスキップ）
mdq index

# 削除されたファイルのチャンクも自動で prune されます（--no-prune で無効化可）
```

ファイル変更を逐次反映したい場合は、別ターミナルで watch モードを起動できます（`watchdog` が必要、`setup-markdown-query` で `-WithWatch` / `--with-watch` を指定するとインストールされます）。

```bash
mdq watch
```

### 3. 検索

```bash
mdq search --q "クエリ" --top-k 5 --max-tokens 800
```

主なオプション:

| オプション | 説明 |
| --- | --- |
| `--q` | 検索クエリ（必須） |
| `--top-k` | 返すヒット数（既定 5 推奨範囲 3〜5） |
| `--max-tokens` | 出力の最大トークン数（既定 800 推奨範囲 400〜800） |
| `--paths` | 検索対象パスを絞り込み（例: `"docs/**"`） |
| `--tags` | frontmatter のタグで絞り込み |
| `--mode` | `bm25`（既定） / `grep` |
| `--snippet-radius` | ヒット行前後の表示行数（既定 ±2） |

出力は JSONL（1 行 = 1 ヒット）です。

### 4. 本文取得（必要時のみ）

検索結果の `chunk_id` を指定して該当チャンクの本文を取得します。

```bash
mdq get --chunk-id <ID>
```

### エージェントから使う場合

インデックス作成後、エージェントに次のように依頼できます。

> このリポジトリ配下の Markdown から "context window" を含む見出しを探して。

`markdown-query` スキルが起動し、ヒットしたチャンクのみが返ってくれば成功です。

> `mdq` の代わりに `python -m mdq` でも同じサブコマンドを実行できます。詳細なオプションは [`skills/markdown-query/references/cli-reference.md`](skills/markdown-query/references/cli-reference.md) を参照してください。

## 動作確認

インストール後、エージェントに次のように尋ねてみてください。

> このリポジトリ配下の Markdown から "context window" を含む見出しを探して。

`markdown-query` スキルが起動し、ヒットしたチャンクのみが返ってくれば成功です。

## 評価方法（ベンチマーク）

「本当に Context Window を節約できているか」を **自分のリポジトリで数値確認** するためのベンチマーク CLI を [`tools/markdown-query/benchmark.py`](tools/markdown-query/benchmark.py) として同梱しています。詳細は [`tools/markdown-query/README.md`](tools/markdown-query/README.md) を参照してください。

### 何を測るのか

同一クエリ集合に対し、次の 3 シナリオを比較します。

| シナリオ | Context に投入する内容 | 想定する使い方 |
| --- | --- | --- |
| `baseline_full` | 索引対象配下の **全 Markdown 本文** | スキルを使わない場合の上限値 |
| `mdq_bm25` | `mdq search --mode bm25` のヒットのみ | 既定の検索モード |
| `mdq_grep` | `mdq search --mode grep` のヒットのみ | 厳密一致モード |

各シナリオで **応答トークン数 / 検索 wall-clock / ベースライン比削減率 / coverage（任意）** を計測します。

### 実行手順

1. 索引を作成しておく（未作成なら `--ensure-index` を付ければ自動で作成されます）。

   ```bash
   mdq index
   ```

2. 計測したいクエリを 1 行 1 件で書いたファイルを用意します（例として [`tools/markdown-query/queries.sample.txt`](tools/markdown-query/queries.sample.txt) を同梱）。

3. ベンチマークを実行します。

   ```bash
   python tools/markdown-query/benchmark.py \
     --queries-file tools/markdown-query/queries.sample.txt \
     --top-k 5 --max-tokens 800 --repeat 3 --ensure-index
   ```

4. 結果は `tools/markdown-query/results/bench-<UTCタイムスタンプ>.{json,md}` に出力されます（`results/` は `.gitignore` 済）。

### 結果（Markdown レポート）の見方

`bench-*.md` には次のセクションが順に並びます。

1. **Environment**: トークナイザ（`tiktoken/cl100k_base` か fallback）、Python・OS・コミットハッシュ。**他環境の数値と絶対比較するときは必ずここを確認**。
2. **Parameters**: `--top-k` `--max-tokens` `--repeat` などの実行条件。
3. **Index summary**: `--ensure-index` 時の索引作成の所要時間。
4. **baseline_full**: 全文投入時の `files / chars / tokens`。これが **削減率の分母**。
5. **Skill なし vs Skill あり (プロンプトトークン比較)**: シナリオごとに次を表示します。
   - `avg_response_tokens`: クエリ平均の応答トークン数（小さいほど Context 節約）。
   - `avg_vs_baseline_savings_pct`: ベースライン比の削減率（例: `98.5%` なら全文投入比 1.5% に圧縮）。
   - `latency_ms_all`: 全クエリ × `--repeat` 回の `mean / p50 / p95 / min / max`（同一マシン内の A/B 比較用）。
   - `per_query[]`: クエリごとの hits 数、トークン、削減率、`coverage_proxy`（期待パス付き JSON 利用時のみ）。

### 数値の解釈と注意点

- **削減率が高い = 良い** とは限らない。`coverage_proxy`（期待パスが Hit に含まれた割合）が低ければ、絞り込みすぎている可能性があります。期待パス付きの `--queries-json` でセットで評価するのが推奨。
- **`latency_ms` は絶対値で比較しない**。同一マシン・同一コミット内で `--mode bm25` vs `grep`、`--top-k` 違いなどを A/B するための指標です。
- **このベンチマークは LLM API を呼ばない**。回答品質ではなく「Context 投入量と検索速度の代理指標」のみを測ります。回答品質まで含めた評価は別途必要です。
- **撤去判断の閾値はツール側では提示しません**。「埋め込みベース RAG が導入されたら本スキルを退役させるか」などは、出力された数値を見て利用者が判断してください。

### クエリに期待パスを紐付けて評価する（推奨）

`coverage_proxy` を出すには、期待パス付きの JSON を渡します。

```json
[
  {"q": "業務要件 概要", "expected_paths": ["sample/business-requirement.md"]},
  {"q": "ユースケース", "expected_paths": ["sample/usecase-list.md"]}
]
```

```bash
python tools/markdown-query/benchmark.py \
  --queries-json my-queries.json --repeat 3
```

### 実行例: `sample/` フォルダーに対するベンチマーク結果

本リポジトリ同梱の [`sample/`](sample/) 配下（業務要件・ユースケース・サービス記述書の Markdown 4 ファイル）に対し、[`tools/markdown-query/queries.sample.txt`](tools/markdown-query/queries.sample.txt) の 5 クエリで実行した **参考値** です。生のレポートは [`tools/markdown-query/results/bench-20260514T012257Z.md`](tools/markdown-query/results/bench-20260514T012257Z.md) を参照してください。

> **重要 — あくまで「例」です**: 以下の数値は **特定の環境・特定のリポジトリ・特定のクエリ集合** で測定したものであり、**ユーザーの環境で同じ結果や精度を保証するものではありません**。Markdown の量・章構造・クエリの語彙・トークナイザ・マシン性能などにより結果は大きく変動します。必ず自分のリポジトリで `benchmark.py` を実行して判断してください。

**測定環境（例）**: `tiktoken/cl100k_base` / Python 3.12.10 / Windows 11 / `top_k=5`, `max_tokens=800`, `repeat=3`

**baseline_full**: 4 files / 83,230 chars / **68,440 tokens**

| シナリオ | avg tokens (with skill) | avg savings vs baseline | latency mean / p50 / p95 (ms) |
| --- | ---: | ---: | --- |
| `mdq_bm25` | 1,794.6 | **97.38%** | 13.35 / 13.65 / 14.20 |
| `mdq_grep` | 700.6 | **98.98%** | 1.45 / 0.47 / 3.33 |

クエリ別（`mdq_bm25`、抜粋）:

| query | hits | tokens | savings % |
| --- | ---: | ---: | ---: |
| ロイヤルティプログラム | 5 | 2,484 | 96.37 |
| 生成AI パーソナライズ | 5 | 1,816 | 97.35 |
| ポイント付与 失効 | 5 | 1,250 | 98.17 |
| 会員 同意管理 | 5 | 1,402 | 97.95 |
| ユースケース | 5 | 2,021 | 97.05 |

> この例では全文投入の **約 1〜3%** までプロンプトトークンが圧縮されています。一方で `mdq_grep` は語の表記揺れに弱く、`生成AI パーソナライズ` など 0 hit になるクエリもあります（`savings % = 100` は「ヒットなし」を意味するため、別途 `coverage_proxy` での確認が必要です）。**削減率だけで判断せず、必ず期待パス付き JSON で coverage も併せて評価してください**。

## リポジトリ構成

```
README.md                           本ファイル
LICENSE                             MIT ライセンス
plugin.json                         プラグイン定義（Copilot CLI / 共通）
apm.yml                             APM マーケットプレイス定義（authoring）
.claude-plugin/marketplace.json     Claude Code / Copilot CLI 用マーケットプレイス
.claude-plugin/plugin.json          Claude Code 用プラグイン定義
gemini-extension.json               Gemini CLI 拡張定義
.mcp.json                           MCP サーバー設定（現状は空、必要に応じて拡張）
setup/                              スキル別の追加 CLI インストールスクリプト
  setup-markdown-query.ps1
  setup-markdown-query.sh
skills/                             スキル本体
  markdown-query/
    SKILL.md
    examples/
    references/
```

> **注意**: `plugin.json`（ルート）と `.claude-plugin/plugin.json` は内容を重複させています。片方を更新する際はもう片方も同期してください。`apm.yml` と `.claude-plugin/marketplace.json` も同様です。

## ライセンス

[MIT](LICENSE)

