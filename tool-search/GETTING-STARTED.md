# tool-search — 他リポジトリでの導入手順

GitHub Copilot SDK のセッションに対して、**ツール定義を毎ターン全件渡すのをやめ、
必要なものだけをその場で発見させる**ための仕組み。SDK 組み込みの `tool_search_tool` を
差し替え、ランキング（日本語対応 BM25）・pin ポリシー・Skill のカタログ合流・
利用統計を自前で持つ。

このフォルダは上流リポジトリ（`dahatake/RoyalytyService2ndGen`）の
`hve/toolsearch/` と `mdq/tokenize.py` を `tools/for-other-repo/copy_to_repo.py` で
まとめたもの。同梱の版情報は [`KIT-VERSION.json`](./KIT-VERSION.json) にある。

> **他の 2 つと性格が違う**
> `markdown-query` / `code-query` は `.github/skills/` へ配置する Skill だが、
> Tool Search は **Copilot SDK を呼ぶアプリケーション側へ組み込むライブラリ**である。
> Skill 定義は同梱しない。

---

## 1. セットアップ（OS だけの状態から）

導入先リポジトリの**ルート**で実行する。`install.ps1` / `install.sh` が
Python 3.11+ と git を確認し、無ければ OS のパッケージマネージャで導入してから
venv を作り `pydantic` を入れる。索引も Skill 配置も行わない。

### Windows

```pwsh
pwsh -NoLogo -NoProfile -File <このフォルダ>\install.ps1
```

### macOS / Linux

```bash
bash <このフォルダ>/install.sh
```

導入されるもの:

- `<このフォルダ>/.venv-toolsearch/` — 依存（`pydantic`）を隔離した venv
- `<このフォルダ>/vendor/toolsearch/` — 実装本体
- `<このフォルダ>/vendor/mdq/tokenize.py` — ランキングが使う日本語対応トークナイザ

---

## 2. 動作確認

```pwsh
<kit>\toolsearch.ps1 policy      # 採用される policy.json を読み込んで妥当性を確認
<kit>\toolsearch.ps1 skills      # 検索対象になる SKILL.md を列挙
<kit>\toolsearch.ps1 dashboard   # 収集済みイベントの集計（未収集なら「データ不足」）
<kit>\toolsearch.ps1 eval        # golden クエリで Recall@k / MRR / トークン削減率
```

```bash
bash <kit>/toolsearch.sh policy
bash <kit>/toolsearch.sh skills --repo-root .
bash <kit>/toolsearch.sh dashboard --json
bash <kit>/toolsearch.sh eval --golden ./my-golden.json --fail-on-miss
```

いずれも **導入先リポジトリのルートで** 実行する（`--repo-root` の既定がカレントで、
`policy` はそこの `.toolsearch/policy.json` を優先するため）。

`dashboard --html <path>` で自己完結 HTML（外部 CDN・フォント・スクリプトを一切参照しない）を出力できる。

`eval` は `.github/skills` から作ったカタログを golden クエリで評価する。ライブカタログ
（SDK の `available_tools`）はセッション中しか手に入らないため、オフラインで測れるのは
Skill 由来のエントリだけである。

---

## 3. Copilot SDK セッションへの配線

`build_session_toolset()` が `create_session(tools=...)` へ渡すツール列を組み立てる。
`config` は次の属性を持つ任意のオブジェクトでよい。

| 属性 | 意味 |
|---|---|
| `tool_search` | `True` のとき SDK の遅延ロードを使う |
| `tool_search_ranking` | `"hve"` のときだけランキングを本実装へ差し替える |
| `excluded_tools` | カタログから外す `ToolEntry.id` の列（任意） |

```python
import sys
from pathlib import Path
from types import SimpleNamespace

KIT = Path(__file__).resolve().parent / "tool-search"
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

`build_session_toolset` は差し替えが無効なとき・`policy.json` が壊れているときに
`([], None)` を返す。**ポリシー不正で処理を落とさない**設計なので、
呼び出し側で例外処理を足す必要はない。

---

## 4. ポリシーの調整

採用される `policy.json` は 1 つだけで、優先順は次のとおり。

1. `<repo>/.toolsearch/policy.json`（あればこちら。**導入先ではこちらを推奨**）
2. [`vendor/toolsearch/policy.json`](./vendor/toolsearch/policy.json)（同梱の既定値。
   上流リポジトリの pin と語彙がそのまま入っている）

同梱側を直接書き換えると `install.py --verify` が「改変」と報告し、キットを入れ直すと
消える。リポジトリ側に置く方が運用しやすい。

| キー | 意味 |
|---|---|
| `limit` / `max_limit` | 1 回の検索で返す件数と上限 |
| `tau` | 適応的打ち切りの閾値（0.0〜1.0） |
| `field_weights` | `name` / `additional_search_text` / `description` / `arg_terms` の重み |
| `pins` | `always`（常時公開） / `auto`（検索対象・自動 pin あり） / `never`（検索対象・自動 pin なし） |
| `additional_search_text` | 検索専用の追加語彙。日本語の言い回しをここへ足すとヒット率が上がる |
| `step_overrides` | `"<workflow>:<step>"` 単位で `search` / `pin_only` を切り替える |

キーは常に `{kind}:{server}:{name}` 形式か、サーバーワイルドカード `{kind}:{server}:*`。
ツール名だけのキーは fail-closed で拒否される（MCP サーバー間で名前が衝突しうるため）。

編集後は必ず確認する（導入先リポジトリのルートで実行すること）:

```pwsh
<kit>\toolsearch.ps1 policy
```

---

## 5. 収集されるデータ

いずれもユーザースコープの追記専用 JSONL。ネットワークへは送らない。

| ファイル | 既定パス | 環境変数 |
|---|---|---|
| 検索イベント | `＜repo-root＞/.toolsearch/events.jsonl` | `HVE_TOOLSEARCH_EVENTS` |
| 利用履歴（自動 pin の学習材料） | `＜repo-root＞/.toolsearch/usage.jsonl` | `HVE_TOOLSEARCH_USAGE` |

---

## 6. ドキュメント

| ファイル | 内容 |
|---|---|
| [`docs/tool-search.md`](./docs/tool-search.md) | 設計方針・アーキテクチャ・ランキング・評価（`users-guide` から同梱） |

> 同梱ドキュメントは上流リポジトリ（HVE）を前提に書かれている。
> `hve/toolsearch/` という記述は、本キットでは `vendor/toolsearch/` に読み替える。
> `hve orchestrate --tool-search-ranking hve` に相当する操作は §3 の配線コードである。

---

## 7. 更新（版の同期）

現状確認はこのフォルダだけでできる。

```pwsh
python install.py --kit-dir . --version    # 導入済みの版
python install.py --kit-dir . --verify     # 同梱ファイルの改変・欠落（policy.json は対象外）
```

更新は、配布リポジトリ（`dahatake/skills`）を再取得し、**古いキットを削除してから**
コピーし直してインストーラを再実行する。

```pwsh
Remove-Item -Recurse -Force <kit>
Copy-Item -Recurse <取得先>\tool-search <kit>
pwsh -NoLogo -NoProfile -File <kit>\install.ps1
```

リポジトリ側に置いた `.toolsearch/policy.json` はキットの外なので影響を受けない。

---

## 8. 既知の制約

- SDK 側の `available_tools`（ライブカタログ）が渡ってくる呼び出しでのみ動く。SDK の対応版が必要。
- Skill の合流は `.github/skills/` / `~/.agents/skills/` / `~/.copilot/skills/` 配下の `SKILL.md` を対象とする。
  frontmatter に `name:` が無いファイルは無視される。
- `session.load_skill_manifest()` は上流固有の `hve/skill_manifest.json` を読む。
  他リポジトリでは存在しないため空として扱われ、`manifest_pins` は効かない（`policy.json` の `pins` は効く）。
- 同梱の `golden-tool-queries.json` は上流のツール構成に対する評価用データ。
  導入先では `toolsearch eval --golden <自前の JSON>` で差し替える。
