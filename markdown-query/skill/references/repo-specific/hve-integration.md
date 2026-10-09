# HVE（Hypervelocity Engineering）固有統合事項

本ドキュメントは、汎用 `markdown-query` Skill を **HVE リポジトリ** で運用する際の固有事項を集約する。汎用版（他リポジトリ）利用者は本ファイルを参照する必要はない。

---

## 1. リアルタイム索引（MdqWatcher）

HVE CLI Orchestrator（`hve orchestrate`）実行中は、バックグラウンドの `MdqWatcher` が `.md` ファイルの追加 / 更新 / 削除を OS イベント（`watchdog`）で検知し、索引 DB を逐次更新する。手動の `python -m mdq index` を毎ステップ実行しなくても、サブセッションが常に最新の索引を参照できる。

- **既定**: ON
- **依存**: `watchdog`（`pip install -e .[mdq-watch]` で導入）
- **無効化**: CLI 引数 `--no-mdq-watch` または環境変数 `HVE_MDQ_WATCH=0`
- **スタンドアロン版**: `python -m mdq watch`
- **共存**: 手動の `python -m mdq index` は引き続き利用可能（書き込み経路は直列で競合しない）
- **動作対象外**: GitHub Actions / Copilot Cloud Agent（ファイルシステム揮発のため）

## 2. セットアップスクリプトとの連動

- `hve/setup-hve.ps1` / `hve/setup-hve.sh` / `hve/setup-hve.cmd` は既定で `pip install -e ".[mdq-watch]"` を実行し、`python -m mdq --help` の動作確認まで行う。
- 抑止フラグ: `-SkipMdq` / `--skip-mdq`（インストールと検証を抑止。失敗は警告に降格）

## 3. Cloud Agent / GitHub Actions 運用

- Cloud runner 上では作業ツリーが揮発し、索引ファイル `.mdq/index.sqlite` は gitignore 済でセッション間で共有されない。
- **Cloud Agent セッションでは、毎回 `python -m mdq index` を自身で実行**してから `search` / `get` を使う運用とする（増分キャッシュは効かない）。
- CI の索引スモークテストは `.github/workflows/test-hve-python.yml` の `mdq-smoke` job が直接実行する。GitHub Actions runner で生成した索引は独立した Cloud Agent セッションへ引き継げないため、索引構築専用の reusable workflow は使用しない。

## 4. 利用統計ログ（HVE 固有）

- `.mdq/usage.jsonl`: `mdq` CLI が自動追記する利用ログ（gitignore 済）
- `run_journal` 側の参照定数: `hve.run_journal.MDQ_USAGE_LOG_RELATIVE`
- 集計モジュール: `mdq.usage_stats`
- レポート生成: `python -m mdq.usage_report`（または `python tools/skills/markdown_query/generate_usage_report.py`）
- レポート保存先: `<repo>/.mdq/usage-report/`（集計元の `.mdq/usage.jsonl` と同じリポジトリ配下）
- レポート定義・指標の算出: `mdq.usage_stats` / `mdq.usage_report`（実装が正本）

## 5. ベンチマーク（撤去判断用）

- スクリプト: `tools/skills/markdown_query/benchmark.py`
- サンプルクエリ: `tools/skills/markdown_query/queries.sample.txt`
- 詳細: `tools/skills/markdown_query/README.md`

### 過去の実測値（他リポジトリへ外挿しない）

以下は Skill 入口と組み込み例から移設した HVE 固有の過去記録であり、今回の変更の再測定結果ではない。索引対象規模・クエリ分布が異なる他リポジトリでは値が変わるため、自リポジトリで計測する。

実測日: 2026-05-18 / 索引対象: 当時の HVE 既定 11 ルート（81 files, 1,003,418 chars）/ トークナイザ: `fallback(chars/4)` / クエリ 5 件 × 3 回 (n=15) / `--top-k 5 --max-tokens 800 --lang ja-jp --strategy heading`

| 指標 | 値 |
|---|---|
| baseline_full（全 `.md` の合計トークン数） | 250,823 tokens |
| mdq_bm25 平均レスポンストークン | **480.8 tokens / query** |
| mdq_bm25 平均 Context 削減率 | **99.81 %** |
| mdq_bm25 レイテンシ (mean / p50 / p95) | 139.6 ms / 140.7 ms / 147.6 ms |
| mdq_grep 平均レスポンストークン | **323.2 tokens / query** |
| mdq_grep 平均 Context 削減率 | **99.87 %** |
| mdq_grep レイテンシ (mean / p50 / p95) | 13.4 ms / 12.6 ms / 19.4 ms |

レポート: [bench-20260518T022346Z.md](../../../../../tools/skills/markdown_query/results/bench-20260518T022346Z.md)。同名 JSON も同ディレクトリに保存される。tiktoken 導入時は `cl100k_base` で再計測されるが、上記は `chars/4` フォールバックでの近似である。

別の既存記録（ゴールデン 60 問）では、`--top-k 5 --max-tokens 1600` が平均 4.5〜4.8 件 / 1,234〜1,316 tokens、`--top-k 20 --max-tokens 800 --return-unit locations` が平均 6.7〜7.1 件 / 714〜756 tokens で、期待箇所への到達率は 4 スライスすべてで同等以上だった。旧入口に測定日の記載はなく、現在の性能や上記 2026-05-18 の測定と同一とは扱わない。

## 6. Related Skills（HVE 内の棲み分け）

- `knowledge-lookup`: `knowledge/D01〜D21` の参照ルール（こちらが優先）
- `knowledge-management`: `knowledge/` への書き込み
- `repo-onboarding-fast`: 初見リポジトリでのファイル探索補助

### DO NOT USE FOR（HVE 固有）

- `knowledge/D01〜D21-*.md` の参照は `knowledge-lookup` Skill の責務。`markdown-query` の `--paths` で `knowledge/D...` を指定しないこと。
