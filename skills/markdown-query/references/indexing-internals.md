# 索引内部仕様

## ストア
- SQLite (`.mdq/index.sqlite`, stdlib `sqlite3`)
- テーブル: `files`(path PK, sha1, mtime, size_bytes, frontmatter JSON), `chunks`(chunk_id PK, path, heading_path, level, start_line, end_line, token_est, text, tags JSON)

## チャンク化
- Markdown を **見出し単位** で分割（H1〜H6）。
- 各チャンクは「見出し行 → 次の同等以上レベルの見出し直前」までの本文。
- 先頭の見出し前の本文は `(preface)` チャンクとして登録。
- フェンスドコードブロック（``` または ~~~）内の `#` は見出しとして扱わない。

## frontmatter
- ファイル先頭の `---` ブロックを PyYAML で解析（PyYAML 任意。未導入時は単純 KV / 生 JSON をベストエフォートで解析し、失敗時は空辞書）。
- `tags`（list または str）をチャンクの `tags` にも複製し検索フィルタに利用。

## ID / 増分更新
- `chunk_id` = SHA1(`path \0 heading_path \0 start_line`)
- ファイル単位で SHA-1 を保持し、再索引時に一致するファイルはスキップ。

## BM25
- 既定で `rank_bm25` を利用（任意導入）。未導入時は同等の `_MiniBM25` フォールバック（stdlib 実装）に切替。
- トークナイザ: 英数 + 日本語 1 文字単位（CJK Unified / Hiragana / Katakana を 1 トークン）。
- スコア > 0 のチャンクのみ返却。

## snippet
- マッチした query token を最も多く含む 1 行を中心に `--snippet-radius` 行を抽出。
- `max_chars=400` で末尾切り詰め。

## 既知の制約（捏造禁止）
- BM25 はチャンク全件をクエリ時にメモリへロードする（小〜中規模向け）。大規模化したら SQLite FTS5 への移行を検討。
- 日本語形態素解析は行っていない（1 文字単位）。固有表現の精度は限定的。
- シンボリックリンクは追従しない（フラグでの切り替えも未実装）。
- バイナリや巨大生成物が混在するディレクトリは、既定除外（`.git`, `node_modules`, `.venv` ほか）でカバーされる範囲外であれば、`mdq index --root` で対象を明示する形で回避してください（`--exclude` での明示除外は未実装）。
