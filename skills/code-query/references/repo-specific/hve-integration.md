# Appendix: HVE リポジトリ固有事項（code-query）

本ファイルは **HVE（Hypervelocity Engineering）リポジトリ固有** の設定・実測値・具体例を収容する。
他リポジトリへ本 Skill を配布する場合、このディレクトリ（`references/repo-specific/`）は同梱されない。

## profile 一覧

`--profile` は対象コードベースの切り替え。本リポジトリの `cq.toml` は次の 2 つを宣言している。

| profile | 対象 | 用途 |
|---|---|---|
| `hve` | `hve/`, `mdq/`, `cq/`, `hve-dev/`, `tools/`, `.github/scripts/` | HVE アプリケーション自体の保守 |
| `app` | `src/` | HVE が生成したアプリケーションの調査 |

`--profile` を毎回書かずに済ませたい場合は環境変数 `CQ_PROFILE`、または配布キットのランチャ
（`cq.ps1` / `cq.sh` / `cq.cmd`）を使う。

## 最短呼び出し例（本リポジトリの値を埋めたもの）

```sh
python -m cq stats  --profile hve                      # 索引の存在と規模を確認
python -m cq index  --profile hve                      # 未作成 or 古ければ実行（増分）
python -m cq search --profile hve --q "<探したい語>"     # 既定 --mode auto / --top-k 5 / --max-tokens 800
python -m cq search --profile hve --q "<探したい語>" --return-unit chunk  # 関数・クラス単位で本文ごと返す
python -m cq def    --profile hve --symbol "StepRunner.set_fork_index"  # 定義へ直行（qualname）
python -m cq get    --profile hve --chunk-id <ID>      # snippet で不足するときだけ本文を取る
python -m cq refs   --profile hve --symbol "resolve_run_id"      # 呼び出し元を列挙
python -m cq trace  --profile app --id TEST-SVC-02-001           # トレース ID → コード位置
python -m cq trace  --profile app --by-path <file>               # コード → 設計文書のパスとアンカー
python -m cq map    --profile hve --paths "hve/gui/*" --max-tokens 1200   # 俯瞰マップ
python -m cq watch  --profile hve                                # 保存を即座に索引へ反映
```

## 検索品質の実測値

**計測日: 2026-08-04。** ゴールデン 21 問 × 2 profile / `--top-k 5` / トークン計数は `tiktoken/cl100k_base`。
`cq` 側は索引を最新化した状態（`hve` 830 files / `app` 154 files）。数値は本リポジトリのコードベースに対する
ものであり、他リポジトリへは外挿できない。

| profile | 手法 | 探索空間 | top-1 | 平均トークン |
|---|---|---:|---:|---:|
| `hve` | **`cq search`** | 索引 830 files | **95.2%** | **280.9** |
| `hve` | `grep` 対照群（全リポジトリ） | 3,182 files | 14.3% | 2,115.3 |
| `hve` | `grep` 対照群（profile roots 限定） | 1,184 files | 28.6% | 1,386.5 |
| `app` | **`cq search`** | 索引 154 files | **95.2%** | **236.0** |
| `app` | `grep` 対照群（全リポジトリ） | 3,182 files | 0.0% | 854.0 |
| `app` | `grep` 対照群（`src/` 限定） | 227 files | 71.4% | 148.8 |

- **`app` profile では、探索空間を揃えると `grep` の方がトークンが少ない**（148.8 対 236.0）。`cq` の優位は
  正解率（71.4% → 95.2%）と、頻出語で出力が爆発しないことにある（実測: `validate_asdw_data_verify_script` は
  `git grep` が 186,941 字を返す場面で `cq` は 211 tokens）。
- **レイテンシは計測していない。** 本環境では同一コマンドの所要時間が 6 倍以上ばらつくため（`pytest cq/tests`
  が 431.83 s と 69.85 s、`cq search` が 292 ms と 842 ms）、比較指標として使えない。
- 旧版に記載していた「`hve` 84.8 トークン / 9.6 ms」は**当時のコーパスと計測条件での値**であり、現在の
  コーパスでは再現しない（[hve-dev/requirement-test-mapping.md](hve-dev/requirement-test-mapping.md) の
  FR-CQ-06 にも同旨の注記がある）。

## 索引運用

- 編集が頻繁な場合は `python -m cq watch --profile hve` を並走させる。
- GUI からの索引運用は `hve/gui/settings_window.py` の Code-Query セクション（FR-GUI-04）を使う。
- 配布キットは [tools/skills/code_query/](../../../../../tools/skills/code_query/)。

## 利用ログ（FR-CQ-14）

- `＜repo-root＞/.cq/usage.jsonl`: `cq` CLI がサブコマンド実行ごとに自動追記する利用ログ（gitignore 済み）。
- 追記モジュール: [cq/usage_log.py](../../../../../cq/usage_log.py)。呼び出し元は `cq/cli.py` の `_record_usage`。
- 1 行 1 レコードで `ts` / `command` / `args` / `elapsed_ms` / `result` / `exit_code` を持ち、
  Orchestrator が伝播した `HVE_RUN_ID` / `HVE_WORKFLOW_ID` / `HVE_STEP_ID` / `HVE_AGENT_ID` のうち
  設定済みのものだけを `context` へ入れる。
- `watch` は長時間常駐するため記録しない。書き込みに失敗しても CLI の終了コードと標準出力は変わらない。
- `markdown-query` の利用ログ（`.mdq/usage.jsonl`）とは**別ファイル**。混在させない。
