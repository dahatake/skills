---
name: markdown-query
description: >
  ローカルのみで動作する Markdown 横断クエリを実行し、該当チャンクのみを返して Context Window を最小化する。
  USE FOR: search markdown, query docs, find heading, lookup across markdown files, bm25 markdown search, grep markdown, list markdown by tag.
  DO NOT USE FOR: editing markdown (use knowledge-management), knowledge/ D01-D21 lookup (use knowledge-lookup), cloud embedding search, html rendering.
  WHEN: 複数 Markdown を横断検索したい、見出し単位で本文を取得したい、Context を節約しつつドキュメント参照したい。
metadata:
  origin: user
  version: 0.2.0
---

# markdown-query

## 目的
- ローカル完結（外部 API なし）で Markdown 群に対する横断クエリを行う。
- Copilot / Custom Agent の **Context Window 消費を最小化** するため、ヒットしたチャンクの **小さな snippet（既定 ±2 行）** のみを返す。
- 索引対象既定: **カレントディレクトリ（リポジトリルート）配下の全 `.md` / `.markdown` ファイル**。既定除外ディレクトリ: `.git`, `node_modules`, `.venv`, `venv`, `__pycache__`, `.mdq`, `dist`, `build`, `.next`, `.cache`（再帰走査時に名前一致でプルーニングされる）。任意の root に絞りたい場合は `mdq index --root docs --root knowledge` のように指定する。

## Non-goals（このスキルの範囲外）
- Markdown の編集 / 生成
- クラウド埋め込み / リモート検索 / HTML レンダリング。

## トリガー
- frontmatter `description` の USE FOR / DO NOT USE FOR / WHEN に従う。
- 詳細は [references/cli-reference.md](references/cli-reference.md) を参照。

## 手順サマリ
1. **索引（初回 or 変更後）**: `mdq index`
   - 既定でカレントディレクトリを再帰走査し、既定除外ディレクトリ（`.git`, `node_modules`, `.venv`, `venv`, `__pycache__`, `.mdq`, `dist`, `build`, `.next`, `.cache`）を name 一致でプルーニングして `.md` / `.markdown` を索引化。
   - 増分更新（SHA-1 + mtime 一致ファイルはスキップ）
   - 既定で自動 prune（ディスク上に存在しないファイルのチャンクを削除、`--no-prune` で無効化可）
   - **重要**: 索引ファイル `.mdq/index.sqlite` はセッション間で共有されない前提。**この Skill を使う前に必ず 1 回実行すること**。`.gitignore` に `.mdq/` を追加することを推奨。
   - 任意: `mdq watch` でファイル変更を逐次反映（`watchdog` 必須）。
2. **検索**: `mdq search --q "クエリ" --top-k 5 --max-tokens 800`
   - 既定モード: `bm25`、出力: JSONL（1行=1ヒット）
   - `--paths`, `--tags`, `--mode grep`, `--snippet-radius` で絞り込み
3. **本文取得（必要時のみ）**: `mdq get --chunk-id <ID>`
4. 結果を **そのまま Agent に渡す**（生 Markdown を読み込まない）。

> 互換: `mdq` の代わりに `python -m mdq` でも同じサブコマンドを実行できる。

## 入出力例

### 入力（Agent が発行するコマンド）
```
mdq search --q "業務要件 概要" --paths "docs/**" --top-k 3 --max-tokens 500
```

### 出力（JSONL: 1行=1ヒット）
```json
{"chunk_id":"<sha1>","path":"docs/business-requirement.md","heading_path":"# 概要 > ## 範囲","lines":[42,71],"score":12.7,"snippet":"...マッチ前後 ±2 行..."}
```

## Context 節約のコツ
- まず `--format compact` で目視確認 → 必要な `chunk_id` だけ `get` で詳細取得。
- `--top-k` を 3〜5、`--max-tokens` を 400〜800 に保つ（既定）。
- `--paths` でディレクトリを絞ると BM25 精度も向上する。

