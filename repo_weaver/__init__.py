"""repo-weaver: git repo → wiki-weaver source documents.

Public library surface — import ``repo_weaver`` to use repo-weaver as a Python
library rather than a CLI tool.  All six functions below are the stable
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
"""

from __future__ import annotations

from .materialize import materialize
from .weave import ask, init, replay_windows, weave, weave_multi

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "ask",
    "init",
    "materialize",
    "replay_windows",
    "weave",
    "weave_multi",
]
