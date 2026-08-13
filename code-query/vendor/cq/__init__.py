"""cq — local source-code search for HVE (FR-CQ-01).

Kept import-light on purpose: `python -m cq search` must not pay for optional
dependencies at import time (NFR-CQ-01).
"""

__all__ = ["__version__"]

__version__ = "0.3.0"
