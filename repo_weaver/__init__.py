"""repo-weaver: git repo → wiki-weaver source documents.

Public library surface — import ``repo_weaver`` to use repo-weaver as a Python
library rather than a CLI tool.  All functions/classes below are the stable
importable API; internal helpers (prefixed with ``_``) are not guaranteed to
remain stable across releases.

Typical lib usage::

    import repo_weaver

    # Scaffold a corpus and register a repo
    repo_weaver.init("/path/to/corpus", repos=["/path/to/my-repo"])

    # Ingest git history into the corpus
    repo_weaver.weave(corpus="/path/to/corpus", repo="/path/to/my-repo",
                      since=None, until=None)

    # Query the corpus
    exit_code = repo_weaver.ask("What changed in auth last month?",
                                corpus="/path/to/corpus")

    # Pure change-detection query, no corpus/watermark required — useful for
    # a caller with their own scheduler and their own "last processed" state
    signal = repo_weaver.changed_since("owner/repo", since="2026-06-01")
    if signal.changed:
        print(signal.reasons)  # e.g. ["push activity", "issue activity"]
"""

from __future__ import annotations

from ._version import __version__
from .materialize import materialize
from .sync import ChangeSignal, changed_since
from .weave import ask, init, replay_windows, weave, weave_multi

__all__ = [
    "__version__",
    "ask",
    "changed_since",
    "ChangeSignal",
    "init",
    "materialize",
    "replay_windows",
    "weave",
    "weave_multi",
]
