# クエリ例パターン集

## 1. キーワードでざっくり横断検索（既定言語=ja-jp）
```
python -m mdq search --q "業務要件" --top-k 5
```

## 2. 特定ディレクトリのみ
```
python -m mdq search --q "アーキテクチャ" --paths "users-guide/*" --top-k 3
```

## 3. 完全一致 / 厳密一致したい（grep モード）
```
python -m mdq search --q "Bounded Context" --mode grep --top-k 10
```

## 4. frontmatter タグでフィルタ
```
python -m mdq search --q "API" --tags backend security
```

## 5. snippet を最小化してさらに節約
```
python -m mdq search --q "..." --top-k 3 --max-tokens 300 --snippet-radius 1
```

## 6. 人間可読で確認 → chunk_id を抽出 → 詳細取得
```
python -m mdq search --q "..." --format compact
python -m mdq get --chunk-id <ID>
```

## 7. 見出しレベル別の俯瞰
```
python -m mdq list --paths "docs/*" --heading-level 2
```

## 8. 英語ドキュメントを検索（`--lang en-us`）
```
python -m mdq index --lang en-us
python -m mdq search --lang en-us --q "design pattern" --top-k 5
```

## 9. 長文の章を扱う（`--strategy heading_recursive`）
2000 文字を超える章を段落単位でサブチャンク化:
```
python -m mdq index --strategy heading_recursive
python -m mdq search --strategy heading_recursive --q "..."
```
ヒットしたサブチャンクの兄弟 part を同時取得:
```
python -m mdq search --strategy heading_recursive --q "..." --merge-parts
```

## 10. 見出しが整っていない文書 / RAG 用途（`--strategy fixed_window`）
```
python -m mdq index --strategy fixed_window
python -m mdq search --strategy fixed_window --q "..."
```

## 11. 親見出し・隣接チャンクを併せて取得（文脈拡張）
```
python -m mdq search --q "..." --include-parent --expand-neighbors 1
```

## 12. 大規模コーパスで FTS5 を強制利用
```
python -m mdq search --q "..." --engine fts5
```
または環境変数で `auto` モードを FTS5 に切替（PowerShell / bash）:
```powershell
$env:MDQ_FTS5 = "1"
python -m mdq search --q "..."
```
```bash
export MDQ_FTS5=1
python -m mdq search --q "..."
```

## 13. 索引存在確認 → 必要なら再構築
```
python -m mdq stats
python -m mdq index   # 増分。--rebuild で強制全件
```
