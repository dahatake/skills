# Auto Strategy Routing（`--strategy auto`）

`python -m mdq search --strategy auto` を指定すると、Skill 側のルーティングモジュール ``mdq/query_router.py`` がユーザークエリから最適な chunking strategy を選択する。クエリインタフェースは strategy で分岐しないため、呼び出し側（Agent / Skill）は `auto` のみを意識すればよい。

> **設計原則**: ローカル完結（LLM 呼び出しなし）の純ルールベース。判定根拠は `RouterDecision` として返却され、`mdq.usage_log` に `router_reason` / `router_rule_id` / `router_fallback_used` として記録される。

## 1. 判定ルール（優先順位順、最初にマッチしたものを採用）

| 順 | rule_id | reason | マッチ条件 | 採用 strategy |
|---|---|---|---|---|
| 1 | 1 | `id_lookup` | クエリが ID パターン（`^[A-Z]{1,8}[-_]?\d{1,5}` / `D\d{2}` / `[A-Z]{2,8}-[A-Z0-9]{1,16}`）、もしくは 1〜2 個の ID トークンのみ | `heading` |
| 2 | 2 | `exact_match` | クエリが `"..."` `「...」` `『...』` で囲まれている、または `--mode grep` | `heading` |
| 3 | 6 | `code_fragment` | クエリに `=>` `->` `::` `()` `{}` `[]` を含む、または `{` `}` `(` `)` `;` が 2 文字以上含まれる（Rule 3 より優先） | `fixed_window` |
| 4 | 3 | `short_proper_noun` | トークン数 ≤ 3、24 文字以下、CJK を含むか ASCII 大文字始まり | `heading` |
| 5 | 4 | `concept_overview` | クエリに `概要` `アーキテクチャ` `方針` `設計` `overview` `architecture` `summary` 等の概念語を含む | `pageindex` (PageIndex 型の目次ナビゲーション。`pageindex` DB 不在時はフォールバック) |
| 6 | 5 | `narrative_query` | トークン数 ≥ 8、または `とは` `について` `なぜ` `how to` `why ` `what is` 等のマーカーを含む、または句読点含みで ≥ 4 トークン | `semantic_paragraph` （`[semantic]` extra 不在時は `heading_recursive` へフォールバック） |
| 7 | 7 | `default` | 上記いずれもマッチしない | `heading_recursive` |

辞書定数は `query_router.py` 内の `CONCEPT_TERMS` / `NARRATIVE_MARKERS` / `_CODE_SUBSTR` / `_CODE_CHARS` に集約されている。**外部 JSON での上書きは行わない**（リポジトリ間の挙動差を避けるため、辞書追加は upstream への PR で行う）。

## 2. 在庫フォールバック

ルールで選ばれた strategy の DB（`.mdq/index-<lang>-<strategy>.sqlite`）が存在しない場合、以下の順序で別 strategy へフォールバックする:

```
pageindex  →  semantic_paragraph  →  heading_recursive  →  heading  →  fixed_window
```

フォールバックが発生した場合 `RouterDecision.fallback_used = True` を記録し、`usage_log` に `router_fallback_used` を残す。1 つも DB が無ければ元の strategy を返却し、検索結果は 0 件となる（Skill が明示的に "0 hits" を返す）。

> 注: `graphrag` は **auto の候補に含まれない**（フォールバック序列にも入らない）。`graphrag` は SQLite 索引ではなく LightRAG 専用 working_dir を使う別系統のため、`--strategy graphrag` を明示した時のみ起動する。詳細は [graphrag-strategy.md](graphrag-strategy.md)。

## 3. RouterDecision スキーマ

```python
@dataclass
class RouterDecision:
    strategy: str             # 最終採用 strategy（フォールバック後）
    reason: str               # ASCII ラベル（上表 reason）
    rule_id: int              # 1..7
    original_strategy: str    # フォールバック前
    fallback_used: bool       # 在庫不在で代替したか
    candidates: list[str]     # 評価された候補（debug）
```

`usage_log` レコードの `args` には `strategy="auto"`, `effective_strategy=<採用>`, `router_reason`, `router_rule_id`, `router_fallback_used` が記録される。

## 4. 統計集計（usage_stats H1）

``mdq/usage_stats.py`` の `_group_routing()` が `auto` 実行ログを集計し、以下のキーで返す:

```jsonc
{
  "H1_auto_strategy_distribution": {
    "total_auto_search": 42,
    "fallback_count": 2,
    "fallback_rate": 0.0476,
    "by_reason": {
      "concept_overview": {"count": 9, "rate": 0.2143},
      "narrative_query": {"count": 18, "rate": 0.4286},
      "id_lookup":       {"count": 8, "rate": 0.1905}
    },
    "by_effective_strategy": {
      "heading":           {"count": 25, "rate": 0.5952},
      "heading_recursive": {"count": 17, "rate": 0.4048}
    }
  },
  "H2_parent_expansion_rate": {
    "total_parent_requests": 12,
    "request_expanded_rate": 0.9167,
    "hit_expanded_rate":     0.5714
  }
}
```

`usage_stats.aggregate_usage_stats()` の戻り値 `schema_version` は **2** に上がっている（v1 互換も保持される範囲）。

## 5. 手動オーバーライド

- 自動判定を無効化したい場合は `--strategy heading|heading_recursive|fixed_window` を明示する。
- 採用判断のログを確認するには `.mdq/usage.jsonl` の `args.router_reason` / `args.router_rule_id` を見る。
- 統計レポート（`python -m mdq.usage_report` 経由）には H1 / H2 の集計が反映される。
