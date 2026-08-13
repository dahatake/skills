# tool-search

GitHub Copilot SDK のセッションに対して、**ツール定義を毎ターン全件渡すのをやめ、必要なものだけをその場で発見させる** ための仕組みです。SDK 組み込みの `tool_search_tool` を差し替え、ランキング（日本語対応 BM25）・pin ポリシー・Skill のカタログ合流・利用統計を自前で持ちます。

> **他の 2 つと性格が違います。**
> `markdown-query` / `code-query` は `.github/skills/` へ配置する Skill ですが、Tool Search は **Copilot SDK を呼ぶアプリケーション側へ組み込むライブラリ** です。Skill 定義は同梱しません。索引も作りません。

---

## 1. インストール

```powershell
# Windows
pwsh -NoLogo -NoProfile -File tools\kits\tool-search\install.ps1
```

```bash
# macOS / Linux
bash tools/kits/tool-search/install.sh
```

導入されるもの:

| パス | 内容 |
|---|---|
| `<kit>/.venv-toolsearch/` | 依存（`pydantic`）を隔離した venv |
| `<kit>/vendor/toolsearch/` | 実装本体 |
| `<kit>/vendor/mdq/tokenize.py` | ランキングが使う日本語対応トークナイザ |

リポジトリルートには何も生成しません。`--repo-root` / `--skip-prereq` は他のキットと共通ですが、このキットには GUI も監視も追加依存も無いため `--with-gui` / `--with-watch` / `--no-extras` は何もしません。索引も Skill 配置も行わないので `--no-index` / `--no-skill` も同様です。

## 2. 動作確認

`<kit>` はキットを配置したディレクトリ（例: `tools/kits/tool-search`）です。

```powershell
<kit>\toolsearch.ps1 policy      # 同梱 policy.json を読み込んで妥当性を確認
<kit>\toolsearch.ps1 skills      # 検索対象になる SKILL.md を列挙
<kit>\toolsearch.ps1 dashboard   # 収集済みイベントの集計
<kit>\toolsearch.ps1 eval        # golden クエリで Recall@k / MRR / トークン削減率
```

```bash
bash <kit>/toolsearch.sh policy
bash <kit>/toolsearch.sh skills --repo-root .
bash <kit>/toolsearch.sh dashboard --json
bash <kit>/toolsearch.sh dashboard --html tool-search.html
```

- `skills` は `<repo>/.github/skills` に加えて、ユーザースコープの `~/.copilot/skills` と `~/.agents/skills` も走査します。
- `dashboard --html <path>` は外部 CDN・フォント・スクリプトを一切参照しない自己完結 HTML を出力します。
- `eval` に同梱されている golden クエリは **上流リポジトリ専用** です。そのまま実行しても Recall は意味を持ちません。自分のリポジトリで測るには `--golden ./my-golden.json` を渡してください。

## 3. Copilot SDK セッションへの配線

このコードは **呼び出し側に Copilot SDK が入っていること** を前提とします。キットの venv には `pydantic` しか入りませんので、アプリケーション側の環境で実行してください。

`build_session_toolset()` が `create_session(tools=...)` へ渡すツール列を組み立てます。`config` は次の属性を持つ任意のオブジェクトで構いません。

| 属性 | 意味 |
|---|---|
| `tool_search` | `True` のとき SDK の遅延ロードを使う |
| `tool_search_ranking` | `"hve"` のときだけランキングを本実装へ差し替える |
| `excluded_tools` | カタログから外す `ToolEntry.id` の列（任意） |

```python
import sys
from pathlib import Path
from types import SimpleNamespace

KIT = Path(__file__).resolve().parent / "tools" / "kits" / "tool-search"
sys.path.insert(0, str(KIT / "vendor"))

from toolsearch.session import (
    build_session_toolset,
    record_session_usage,
    resolve_called_tool_ids,
)
from toolsearch.stats import StatsCollector

config = SimpleNamespace(
    tool_search=True,
    tool_search_ranking="hve",
    excluded_tools=(),
)

tools, context = build_session_toolset(
    config,
    repo_root=Path.cwd(),
    workflow_id="my-workflow",
    step_id="1.1",
    on_event=StatsCollector(run_id="run-001", workflow_id="my-workflow", step_id="1.1"),
)

session = client.create_session(
    tool_search={"enabled": True},
    tools=tools,          # 空リストなら SDK 既定のランキングのまま動く
)

# セッション終了時。呼ばれたツール名を記録すると自動 pin の学習材料になる。
record_session_usage(
    resolve_called_tool_ids(context, called_tool_names),
    session_id=session.id,
    workflow_id="my-workflow",
    step_id="1.1",
)
```

`build_session_toolset` は差し替えが無効なとき・`policy.json` が壊れているときに `([], None)` を返します。**ポリシー不正で処理を落とさない** 設計なので、ポリシーに関しては呼び出し側で例外処理を足す必要はありません（SDK 側の例外は別です）。

## 4. ポリシーの調整

設定ファイルは 1 つだけです。優先順位は次のとおり。

1. `<repo>/.toolsearch/policy.json`（あればこちらが使われる。**導入先ではこちらを推奨**）
2. `<kit>/vendor/toolsearch/policy.json`（同梱の既定値。上流リポジトリの pin と語彙が入ったまま）

全フィールドが必須なので、**同梱物をコピーしてから編集してください**。一部のキーだけを書いたファイルは `policy 不正: policy is missing required field` で拒否され、アプリケーション側では例外を出さず SDK 既定の振る舞いへ黙って戻ります。

```powershell
New-Item -ItemType Directory -Force .toolsearch | Out-Null
Copy-Item <kit>\vendor\toolsearch\policy.json .toolsearch\policy.json
```

```bash
mkdir -p .toolsearch
cp <kit>/vendor/toolsearch/policy.json .toolsearch/policy.json
```

同梱側を直接書き換えると `install.py --verify` が「改変」と報告し、キットを入れ直すと消えます。

| キー | 意味 |
|---|---|
| `limit` / `max_limit` | 1 回の検索で返す件数と上限 |
| `tau` | 適応的打ち切りの閾値（0.0〜1.0） |
| `field_weights` | `name` / `additional_search_text` / `description` / `arg_terms` の重み |
| `pins` | `always`（常時公開） / `auto`（検索対象・自動 pin あり） / `never`（検索対象・自動 pin なし） |
| `additional_search_text` | 検索専用の追加語彙。日本語の言い回しを足すとヒット率が上がる |
| `step_overrides` | `"<workflow>:<step>"` 単位で `search` / `pin_only` を切り替える |

キーは常に `{kind}:{server}:{name}` 形式か、サーバーワイルドカード `{kind}:{server}:*` です。ツール名だけのキーは fail-closed で拒否されます（MCP サーバー間で名前が衝突しうるため）。

編集後は必ず確認します。

```powershell
<kit>\toolsearch.ps1 policy
```

## 5. 収集されるデータ

いずれもリポジトリスコープの追記専用 JSONL で、ネットワークへは送りません。

| ファイル | 既定パス | 環境変数 |
|---|---|---|
| 検索イベント | `<repo>/.toolsearch/events.jsonl` | `HVE_TOOLSEARCH_EVENTS` |
| 利用履歴（自動 pin の学習材料） | `<repo>/.toolsearch/usage.jsonl` | `HVE_TOOLSEARCH_USAGE` |

`.toolsearch/` は `.gitignore` に追加してください。

## 6. 既知の制約

- **現行の Copilot CLI ではトークンが増えるという実測があります。** 遅延公開が発火せず、差し替えのランキングだけが乗るためです。常時有効化する前に [`tool-search/docs/tool-search-dashboard.md`](../tool-search/docs/tool-search-dashboard.md) の冒頭を必ず確認してください。コンテキスト削減が目的なら、まず公開する MCP サーバー自体を絞る方が確実です。
- SDK 側の `available_tools`（ライブカタログ）が渡ってくる呼び出しでのみ動きます。SDK の対応版が必要です。
- オフラインで評価できるのは Skill 由来のエントリだけです。ライブカタログはセッション中しか取得できません。
- 同梱ドキュメントは上流リポジトリ（HVE）を前提に書かれています。`hve/toolsearch/` という記述は、本キットでは `vendor/toolsearch/` に読み替えてください。`hve orchestrate --tool-search-ranking hve` に相当する操作は §3 の配線コードです。

## 7. さらに詳しく

| ファイル | 内容 |
|---|---|
| [`tool-search/GETTING-STARTED.md`](../tool-search/GETTING-STARTED.md) | キット単体での導入手順 |
| [`tool-search/docs/tool-search.md`](../tool-search/docs/tool-search.md) | 設計方針・アーキテクチャ・ランキング・評価 |
| [`tool-search/docs/tool-search-dashboard.md`](../tool-search/docs/tool-search-dashboard.md) | ダッシュボードの読み方と切り分けフロー |

> 同梱ドキュメントの一部のリンクは上流リポジトリのパスを指しており、そのリンク先は上流リポジトリでのみ解決します。
