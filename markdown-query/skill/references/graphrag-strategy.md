# `graphrag` 戦略

LightRAG（[lightrag-hku](https://github.com/HKUDS/LightRAG)）をバックエンドにした knowledge-graph 検索戦略。実装は ``mdq/strategies_graphrag.py``（adapter）と ``mdq/graphrag_runtime.py``（LLM/embed callable factory）に分離されています。

> ⚠️ この戦略は **任意（オプション）** です。既定の `heading` / `semantic_paragraph` / `pageindex` で十分なケースが大半です。`graphrag` はエンティティ抽出と関係グラフ構築のために LLM を呼び出すため、コスト・実行時間ともに大幅に高くなります。

## 1. 概要

| 項目 | 値 |
|---|---|
| 実装 (adapter) | `mdq/strategies_graphrag.py` |
| 実装 (LLM/embed runtime) | `mdq/graphrag_runtime.py` |
| 実装 (indexer 分岐) | `mdq/indexer.py` `build_graphrag_index()` |
| extras | `pip install -e .[graphrag]`（`lightrag-hku>=1.4.16,<1.5`） |
| 既定 LLM provider | `ollama`（`--graphrag-llm-provider mock` でテスト用 stub） |
| 既定 LLM model | `qwen2.5:7b` |
| 既定 embedding provider | `ollama` |
| 既定 embedding model | `nomic-embed-text` |
| 既定 base URL | `http://127.0.0.1:11434`（loopback のみ。`--graphrag-allow-remote-ollama` で非 loopback を許可、警告あり） |
| storage | `<working_dir>` 配下に（LightRAG 既定の）nano-vectordb / networkx graph / JSON KV を file persist（既定 `.mdq/graphrag-<lang>/`）。`vector_storage` / `graph_storage` 設定で他バックエンドへ切替可能だが、`mdq` 側ではフラグ未提供 |
| SQLite | **使わない**。`mdq index --strategy graphrag` は `.mdq/index-*.sqlite` を一切作成・更新しません |
| query mode | `local` / `naive` のみ（R7）。`global`/`hybrid`/`mix`/`bypass` は無効化 |

## 2. アルゴリズム（LightRAG 既定フロー）

1. **insert 時**:
   1. Markdown を `chunk_token_size`（既定 1200 tokens）でチャンキング、`chunk_overlap_token_size`（既定 100 tokens）で重ねる。
   2. 各チャンクから LLM でエンティティと関係（entity / relation）を抽出。
   3. エンティティ・関係を NetworkX グラフ（`graph_chunk_entity_relation.graphml`）に追加。
   4. チャンク本文 / エンティティ記述 / 関係記述それぞれを embedding して nano-vectordb に格納。
   5. duplicate detection を経て KV store (`kv_store_*.json`) と vdb (`vdb_*.json`) を file persist。
2. **query 時** (mode=`local`):
   1. クエリから関連エンティティを embedding 経由で検索。
   2. グラフ近傍を辿って関連チャンクを集約。
   3. LLM で最終回答を生成。
3. **query 時** (mode=`naive`):
   - グラフを使わず純粋な vector retrieval → 回答生成。エンティティ抽出が低品質な小規模コーパスでも動作するが、グラフ機能は失われる。

## 3. CLI フラグ

### index

```
python -m mdq index --strategy graphrag \
    --root users-guide \
    --graphrag-working-dir .mdq/graphrag-ja-jp \
    --graphrag-llm-provider ollama \
    --graphrag-llm-model qwen2.5:7b \
    --graphrag-embed-provider ollama \
    --graphrag-embed-model nomic-embed-text \
    --graphrag-base-url http://127.0.0.1:11434 \
    [--graphrag-allow-remote-ollama] \
    [--graphrag-timeout 120] \
    [--rebuild]
```

`--rebuild` は `<working_dir>` を `shutil.rmtree` で削除してから再構築します。`heading` 等の SQLite 戦略と並列に運用しても干渉しません（別物のディレクトリを使います）。

### search

```
python -m mdq search --strategy graphrag \
    --q "What is mdq?" \
    --graphrag-working-dir .mdq/graphrag-ja-jp \
    --graphrag-llm-provider ollama \
    --graphrag-llm-model qwen2.5:7b \
    --graphrag-embed-provider ollama \
    --graphrag-embed-model nomic-embed-text \
    --graphrag-query-mode local \
    --graphrag-top-k 10 \
    --format jsonl
```

- `--format jsonl`（既定）: `{"strategy": "graphrag", "mode": "...", "top_k": N, "answer": "..."}` を 1 行で stdout に出力。
- `--format compact`: `answer` テキストのみを stdout に出力。

検索失敗時（working_dir 不存在、LightRAG import 失敗、Ollama 接続失敗等）は exit code 2 を返し、`{"strategy": "graphrag", "error": "...", "message": "..."}` を stderr に出力します。

## 4. テスト用 mock provider

外部依存なしでテスト・smoke 確認したいときは `mock` provider を使います:

```
python -m mdq index --strategy graphrag --root users-guide \
    --graphrag-llm-provider mock --graphrag-embed-provider mock --rebuild
python -m mdq search --strategy graphrag --q "..." \
    --graphrag-llm-provider mock --graphrag-embed-provider mock
```

mock LLM は `f"mock:{md5(prompt+system_prompt)[:16]}"` を返す決定論的 stub、mock embedding は md5 ベースで L2 正規化された 64 次元ベクトル（既定）を返します。LightRAG のエンティティ抽出はこの stub では成立しないため、検索回答は `"[no-context]"` を含む文字列になります。**実運用には Ollama (or 同等の本物 LLM) が必須** です。

## 5. セキュリティ・運用上の制約

- **R2: loopback only by default**. `--graphrag-base-url` は `http://127.0.0.1:11434` / `localhost` / `[::1]` 系のみ許可されます。非 loopback URL を渡すと `ValueError` で失敗します。明示的に `--graphrag-allow-remote-ollama` を指定するとバイパスできますが、その場合は `RuntimeWarning` が stderr に出ます。**コーパスを社外 LLM に流す可能性があるため、原則 loopback で運用してください**。
- **R4: lightrag.llm.* は import しません**. mdq は `lightrag.llm.ollama` などの bundled provider を一切使いません（これらは `pipmaster` 経由で `ollama`/`openai` 等の SDK を auto-install してしまうため）。Ollama との通信は `urllib.request` で行います。
- **R5: lazy import**. `from lightrag import LightRAG` は `strategies_graphrag._build_rag()` 内でのみ実行されるため、`mdq index --strategy heading` のような無関係なコマンドは LightRAG を load しません。
- **R7: query mode 制限**. `local` / `naive` のみ。`global`/`hybrid`/`mix` は LLM トークン消費が大きく、`mix`（QueryParam の default）は意図せず呼ばれやすいため allow-list 方式で blocking しています。
- **コスト**. 1 ファイルの insert で複数回 LLM call（エンティティ抽出・要約）が走るため、数百ファイル規模では数十分〜数時間のオーダーになります。`--graphrag-llm-model` で小型モデル（例: `qwen2.5:3b`）を使うか、`mock` で構造のみ確認してから本番モデルに切り替えてください。

## 6. 制限事項

- LightRAG の回答は **要約された自然言語テキスト** であり、`path:line` 形式の citation を返しません。`mdq search --strategy heading` のような snippet hit リストを期待する Agent ワークフローでは併用できません。**全文検索のドロップイン置換ではありません**。
- 既存の `mdq watch`（差分インクリメンタル更新）には対応していません。コーパスを更新した場合は `--rebuild` でフル再構築するか、追加分のみ `mdq index --strategy graphrag --root <new-dir>`（`--rebuild` なし）で append してください。LightRAG の duplicate detection が走ります。
- pruning（ファイル削除の反映）はサポートしません。削除を確実に反映するには `--rebuild` を使用してください。
- `mdq stats` / `mdq list` / `mdq get` は SQLite 前提のため graphrag working dir に対しては動きません。

## 7. 既定値の出典

| 値 | 既定 | 出典 |
|---|---|---|
| `chunk_token_size` | 1200 | `mdq/strategies_graphrag.py` の `GraphRAGConfig` 既定値（LightRAG `LightRAG.__init__` default と同値） |
| `chunk_overlap_token_size` | 100 | `mdq/strategies_graphrag.py` の `GraphRAGConfig` 既定値（LightRAG `LightRAG.__init__` default と同値） |
| `embed_mock_dim` | 64 | mdq 内のテスト用最小値（性能ではなく決定性が目的） |
| `llm_timeout` | 120 秒 | LLM エンティティ抽出は 1 prompt あたり 30〜60s が典型 |
| `embed_timeout` | 60 秒 | Ollama embedding は通常 1〜5s だが retry 余裕を確保 |
| `query_mode` | `local` | LightRAG `QueryParam` の default は `mix` だが、コスト・誤発火回避のため `local` に下げる（R7） |
