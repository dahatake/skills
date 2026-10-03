"""Auto strategy router for mdq search.

Given a user query (and optionally the set of strategies whose per-(lang,
strategy) SQLite DB actually exists on disk), pick the chunking strategy
most likely to surface relevant chunks. Pure rule-based, no LLM call.

The router is **invoked by CLI / Skill side** when ``--strategy auto`` is
passed. The query interface stays unified: callers do not need to pick a
strategy themselves. Decisions are returned as :class:`RouterDecision` so
they can be logged for later evaluation (see ``usage_stats`` H1 metric).

Rules (highest-priority first; first match wins):

  1. ``id_lookup``         : query matches an identifier pattern
                             (e.g. ``D03``, ``APP-12``, ``SRV-AUTH``)
  2. ``exact_match``       : query is quoted ``"..."``
  3. ``short_proper_noun`` : <= 3 short tokens with proper-noun characteristics
  4. ``concept_overview``  : query contains overview/architecture/etc. terms
                             -> ``pageindex`` (table-of-contents navigation)
  5. ``narrative_query``   : long natural-language question (>=8 tokens or
                             contains how/why/what markers)
  6. ``code_fragment``     : query contains code-ish punctuation (=>, (), {})
  7. ``default``           : fallback to ``heading_recursive``

Each rule maps to one of ``heading`` / ``heading_recursive`` / ``fixed_window``
/ ``semantic_paragraph`` / ``pageindex``.
When the chosen strategy's DB is absent (``available_strategies`` does not
include it), the router falls back through a fixed preference list and
records ``fallback_used=True``.

Note on evaluation order vs. ``rule_id``:
  ``rule_id`` is the **identifier** of the rule (stable across versions) and
  matches the numbering in the table above. The **evaluation order** in
  :func:`classify_query` is slightly different: ``code_fragment`` (rule_id=6)
  is checked *before* ``short_proper_noun`` (rule_id=3) so that queries
  containing code-ish punctuation are not absorbed by the short-token
  heuristic. The ``rule_id`` field on :class:`RouterDecision` lets analyses
  group decisions independently of any future reordering.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

# ---------------------------------------------------------------------------
# Dictionary constants (intentional: Q10=B -- no external JSON override).
# ---------------------------------------------------------------------------

# Identifier-shaped tokens. Examples that should match:
#   D03, D12-foo, APP-12, SRV-AUTH, ARCH-7
_ID_RE = re.compile(
    r"^(?:[A-Z]{1,8}[-_]?\d{1,5}|D\d{2}|[A-Z]{2,8}-[A-Z0-9]{1,16})$"
)

# Concept / overview / architecture terms. Both JA and EN.
CONCEPT_TERMS: frozenset[str] = frozenset({
    # JA
    "概要", "全体像", "アーキテクチャ", "方針", "設計", "構成", "目的",
    "背景", "結論", "サマリ", "サマリー", "要約", "戦略",
    # EN
    "overview", "architecture", "summary", "summarize", "summarise",
    "purpose", "goal", "vision", "background", "strategy", "design",
    "conclusion", "abstract",
})

# Narrative-question markers (substring match, case-insensitive for EN).
NARRATIVE_MARKERS: tuple[str, ...] = (
    # JA
    "とは", "について", "なぜ", "どうやって", "どのように", "違い", "比較",
    "教えて", "説明",
    # EN
    "how do", "how does", "how to", "why ", "what is", "what are",
    "explain", "describe", "compare", "difference between",
)

# Code-fragment markers.
_CODE_SUBSTR: tuple[str, ...] = ("=>", "->", "::", "()", "{}", "[]")
_CODE_CHARS: frozenset[str] = frozenset({"{", "}", "(", ")", ";"})

# Quoted-exact-match detector. Both ASCII and Japanese fullwidth quotes.
_QUOTED_RE = re.compile(r'^"[^"]+"$|^「[^」]+」$|^『[^』]+』$')

# Token regex (matches mdq.search.tokenize: ASCII word OR single CJK char).
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u3040-\u30ff\u4e00-\u9fff]")


# Fallback preference when the chosen DB is unavailable.
# Order rationale: pageindex provides the strongest table-of-contents
# expansion when present; semantic_paragraph is the most expressive
# embedding-based strategy; heading_recursive/heading/fixed_window form
# the existing legacy chain.
_FALLBACK_ORDER: tuple[str, ...] = (
    "pageindex", "semantic_paragraph", "heading_recursive",
    "heading", "fixed_window",
)


@dataclass
class RouterDecision:
    """Outcome of :func:`classify_query`.

    Attributes:
        strategy: Effective strategy after fallback resolution. Always one
            of ``heading`` / ``heading_recursive`` / ``fixed_window``.
        reason: Short ASCII label identifying which rule fired.
        rule_id: Integer 1..7 matching the rule numbering in the module docstring.
        original_strategy: Strategy chosen by the rule, before fallback.
        fallback_used: True when ``original_strategy != strategy``.
        candidates: Strategies considered, in evaluation order (debug).
    """

    strategy: str
    reason: str
    rule_id: int
    original_strategy: str
    fallback_used: bool = False
    candidates: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "reason": self.reason,
            "rule_id": self.rule_id,
            "original_strategy": self.original_strategy,
            "fallback_used": self.fallback_used,
            "candidates": list(self.candidates),
        }


def _tokenize(q: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(q)]


def _looks_like_id(q: str) -> bool:
    """Return True when the trimmed query (or any whitespace-separated token)
    matches an ID pattern."""
    s = q.strip()
    if _ID_RE.match(s):
        return True
    # A two-token form like "APP-3 概要" should *not* be treated as id_lookup
    # because there is descriptive content. Only single-token or
    # "ID ID" patterns count.
    parts = s.split()
    if len(parts) <= 2 and all(_ID_RE.match(p) for p in parts):
        return True
    return False


def _is_quoted(q: str) -> bool:
    return bool(_QUOTED_RE.match(q.strip()))


def _is_short_proper_noun(q: str, tokens: list[str]) -> bool:
    if len(tokens) == 0 or len(tokens) > 3:
        return False
    s = q.strip()
    if any(c.isspace() for c in s) and len(tokens) > 1:
        # Multiple whitespace-separated tokens: require all to look proper-ish.
        pass
    # Heuristic: starts with uppercase ASCII, OR contains a CJK ideograph,
    # AND length <= 24 chars.
    if len(s) > 24:
        return False
    has_cjk = any("\u3040" <= c <= "\u30ff" or "\u4e00" <= c <= "\u9fff" for c in s)
    starts_upper = bool(s) and s[0].isupper() and s[0].isascii()
    return has_cjk or starts_upper


def _has_concept_term(tokens: list[str], raw: str) -> bool:
    raw_lc = raw.lower()
    for term in CONCEPT_TERMS:
        if term.lower() in raw_lc:
            return True
    return False


def _is_narrative(tokens: list[str], raw: str) -> bool:
    if len(tokens) >= 8:
        return True
    raw_lc = raw.lower()
    for marker in NARRATIVE_MARKERS:
        if marker in raw_lc:
            return True
    # Sentence-ish punctuation in JA
    if any(p in raw for p in ("。", "？", "?", "！", "!")):
        return len(tokens) >= 4
    return False


def _has_code_fragment(raw: str) -> bool:
    for s in _CODE_SUBSTR:
        if s in raw:
            return True
    # Multiple code-ish chars
    hits = sum(1 for c in raw if c in _CODE_CHARS)
    return hits >= 2


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def classify_query(
    query: str,
    *,
    available_strategies: Iterable[str] | None = None,
    mode: str = "bm25",
) -> RouterDecision:
    """Classify ``query`` and return a :class:`RouterDecision`.

    ``available_strategies`` (optional) is the set of strategies whose
    per-(lang, strategy) DB currently exists. When the rule-selected strategy
    is absent from this set, fall back through :data:`_FALLBACK_ORDER` to
    the first available one (or keep the original if none are listed).

    ``mode`` is the search mode (``bm25`` or ``grep``). Grep queries always
    map to ``heading`` (exact match semantics).
    """
    q = query or ""
    tokens = _tokenize(q)
    available = (
        set(available_strategies) if available_strategies is not None else None
    )
    candidates: list[str] = []

    # Rule 2 has highest precedence for grep mode -> exact match.
    if mode == "grep":
        original = "heading"
        candidates.append(original)
        return _finalize(original, "exact_match", 2, candidates, available)

    # Rule 1: ID lookup
    if _looks_like_id(q):
        original = "heading"
        candidates.append(original)
        return _finalize(original, "id_lookup", 1, candidates, available)

    # Rule 2: quoted exact match
    if _is_quoted(q):
        original = "heading"
        candidates.append(original)
        return _finalize(original, "exact_match", 2, candidates, available)

    # Rule 6 (checked early so code-ish queries beat short-token heuristics):
    if _has_code_fragment(q):
        original = "fixed_window"
        candidates.append(original)
        return _finalize(original, "code_fragment", 6, candidates, available)

    # Rule 3: short proper-noun-ish query
    if _is_short_proper_noun(q, tokens):
        original = "heading"
        candidates.append(original)
        return _finalize(original, "short_proper_noun", 3, candidates, available)

    # Rule 4: concept / overview term. Prefer pageindex when its DB
    # exists — PageIndex-style table-of-contents navigation matches the
    # "give me an overview" intent. Fallback chain picks the next best
    # strategy when the pageindex DB is absent.
    if _has_concept_term(tokens, q):
        original = "pageindex"
        candidates.append(original)
        return _finalize(original, "concept_overview", 4, candidates, available)

    # Rule 5: narrative question. Prefer semantic_paragraph for long
    # natural-language queries when its DB exists; the existing
    # `_finalize` fallback chain will pick `heading_recursive` otherwise.
    if _is_narrative(tokens, q):
        original = "semantic_paragraph"
        candidates.append(original)
        candidates.append("heading_recursive")
        return _finalize(original, "narrative_query", 5, candidates, available)

    # Rule 7: default
    original = "heading_recursive"
    candidates.append(original)
    return _finalize(original, "default", 7, candidates, available)


def _finalize(
    original: str,
    reason: str,
    rule_id: int,
    candidates: list[str],
    available: set[str] | None,
) -> RouterDecision:
    if available is None or original in available:
        return RouterDecision(
            strategy=original,
            reason=reason,
            rule_id=rule_id,
            original_strategy=original,
            fallback_used=False,
            candidates=list(candidates),
        )
    # Fallback through preference list.
    for alt in _FALLBACK_ORDER:
        if alt == original:
            continue
        if alt in available:
            candidates.append(alt)
            return RouterDecision(
                strategy=alt,
                reason=reason,
                rule_id=rule_id,
                original_strategy=original,
                fallback_used=True,
                candidates=list(candidates),
            )
    # No alternative available — return the original anyway with
    # fallback_used=True so analytics (H1) does not under-report routing
    # failures. Callers will see an empty result set when the DB is missing.
    return RouterDecision(
        strategy=original,
        reason=reason,
        rule_id=rule_id,
        original_strategy=original,
        fallback_used=True,
        candidates=list(candidates),
    )


def discover_available_strategies(repo_root) -> set[str]:
    """Return the set of strategies whose per-(lang, strategy) DB exists.

    Language is ignored here; a strategy is available if ANY language has its
    DB present. Callers pass the result to :func:`classify_query` so the router
    can fall back gracefully when the chosen strategy was never indexed.

    The filename layout is decoded by :func:`mdq.store.existing_index_dbs`,
    which owns the ``index-<lang>-<strategy>.sqlite`` convention.
    """
    from . import store as _store

    return {strategy for _lang, strategy, _path in _store.existing_index_dbs(repo_root)}
