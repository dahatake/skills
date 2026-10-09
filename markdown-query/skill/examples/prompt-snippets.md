# Prompt / Custom Agent への組み込み例

## Prompt スニペット（Copilot Chat / 他 Agent ホスト共通）

> リポジトリ内ドキュメントへの質問は、まず以下のコマンドで関連チャンクのみを取得してから回答してください（生 Markdown ファイルを直接読み込まないこと）。
>
> ```
> python -m mdq stats          # 索引存在を確認。未作成 / 古ければ次行を実行
> python -m mdq index          # 増分更新（初回は全件構築）
> python -m mdq search --q "<質問の主要キーワード>" --top-k 5 --max-tokens 800
> ```
>
> ヒットの `snippet` で不足する場合のみ `python -m mdq get --chunk-id <ID>` で本文を取得してください。
> ヒットが 0 件の場合のみ、grep / read_file 等で生ファイルへフォールバックしてください。

## Custom Agent ファイル例（抜粋）

```markdown
## 入力ファイル
- 関連 Markdown は本文を直接読み込まず、`markdown-query` Skill 経由で取得すること

## 手順
1. `python -m mdq stats` で索引存在を確認。未作成なら `python -m mdq index`。
2. 仕様の参照が必要な箇所では `python -m mdq search --q ...` を実行。
3. snippet で不足する場合のみ `get` で本文取得。
4. 引用には `path:lines` を必ず含める。
```

## Context 最小化の効果（実測ガイド）

- 実際の削減率・レイテンシは文書サイズと言語・戦略の組合せで変動する。自リポジトリで計測し、他環境の実測値をそのまま適用しない。
- 実測手段: [tools/skills/markdown_query/benchmark.py](../../../../tools/skills/markdown_query/benchmark.py)（トークン削減率と wall-clock latency を出力 → `tools/skills/markdown_query/results/bench-<ISO8601>.{json,md}`）。
- 各クエリの実利用ログは `.mdq/usage.jsonl` に自動追記され、`mdq.usage_stats` モジュールで集計できる。usage 統計レポート（E1〜E15 指標）は `.mdq/usage-report/` 配下へ別ツール（`python -m mdq.usage_report`）で出力される（用途が異なる）。
