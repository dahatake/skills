"""mdq - Local-only Markdown cross-file query toolkit.

Goals:
- Minimize Copilot/Custom-Agent context window by returning only matched chunks.
- 100% local execution. No cloud APIs.
- stdlib-first (sqlite3, re, hashlib). rank_bm25 used if installed; else fallback.
"""

__version__ = "0.8.0"

# NOTE: do NOT re-export submodule-level names here; doing so shadows the
# submodule itself (e.g. `from mdq import search` would bind the function
# instead of the module). Callers should `from mdq import search` to get
# the module, and access `search.search(...)` for the function.
__all__ = ["indexer", "search", "store", "cli"]
