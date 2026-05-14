# クエリ例パターン集

## 1. キーワードでざっくり横断検索
```
mdq search --q "業務要件" --top-k 5
```

## 2. 特定ディレクトリのみ
```
mdq search --q "アーキテクチャ" --paths "docs/**" --top-k 3
```

## 3. 完全一致 / 厳密一致したい（grep モード）
```
mdq search --q "Bounded Context" --mode grep --top-k 10
```

## 4. frontmatter タグでフィルタ
```
mdq search --q "API" --tags backend security
```

## 5. snippet を最小化してさらに節約
```
mdq search --q "..." --top-k 3 --max-tokens 300 --snippet-radius 1
```

## 6. 人間可読で確認 → chunk_id を抽出 → 詳細取得
```
mdq search --q "..." --format compact
mdq get --chunk-id <ID>
```

## 7. 見出しレベル別の俯瞰
```
mdq list --paths "docs/**" --heading-level 2
```

## 8. ワークスペース全体を俯瞰（H1 のみ）
```
mdq list --heading-level 1 --limit 100
```

## 9. 索引対象を絞って再索引
```
mdq index --root docs --root knowledge
```
