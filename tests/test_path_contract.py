"""Contract test: repo-weaver path expectations must match wiki-weaver's helpers.

wiki_weaver.lib is the single source of truth for corpus layout.  This test
hard-fails CI if either side drifts — i.e. if wiki-weaver changes a path and
repo-weaver is not updated, or vice-versa.

Every assertion here mirrors a call-site in repo_weaver/weave.py or
repo_weaver/cli.py.  If a new wiki_weaver.lib helper is used in repo-weaver
code, add its expectation here.
"""

from __future__ import annotations

from pathlib import Path

# These imports must resolve: if wiki_weaver is not installed in the venv,
# the test fails immediately with an ImportError rather than silently
# skipping — exactly the "hard-fail" behaviour we want.
from wiki_weaver.lib import (
    LEDGER_NAME,
    SOURCES,
    WIKI_DIR,
    wiki_dashboard,
    wiki_failed,
    wiki_inbox,
    wiki_ledger,
    wiki_sources,
)

# A synthetic corpus root used for all path assertions.
_CORPUS = Path("/test-corpus")


class TestPathConstants:
    """The named constants must have the expected values."""

    def test_wiki_dir_constant(self) -> None:
        assert WIKI_DIR == ".wiki"

    def test_sources_constant(self) -> None:
        """_sources is the visible processed-sources dir (was _archive)."""
        assert SOURCES == "_sources"

    def test_ledger_name_constant(self) -> None:
        assert LEDGER_NAME == ".processed.jsonl"


class TestPathHelpers:
    """Path helpers must resolve to the new-layout paths.

    Each assertion corresponds to a call-site in repo_weaver/ code.
    If any assertion fails, a path diverged between the two repos.
    """

    # ----------------------------------------------------------------
    # weave.py: wiki_ledger — _read_ledger_for_source()
    # ----------------------------------------------------------------
    def test_wiki_ledger(self) -> None:
        """Ledger lives at <corpus>/.wiki/.processed.jsonl (hidden)."""
        assert wiki_ledger(_CORPUS) == _CORPUS / ".wiki" / ".processed.jsonl"

    # ----------------------------------------------------------------
    # weave.py: wiki_failed — _retry_failed_sources()
    # ----------------------------------------------------------------
    def test_wiki_failed(self) -> None:
        """Failed sources dir lives at <corpus>/.wiki/failed (hidden)."""
        assert wiki_failed(_CORPUS) == _CORPUS / ".wiki" / "failed"

    # ----------------------------------------------------------------
    # weave.py: wiki_inbox — _retry_failed_sources(), weave(), weave_multi()
    # ----------------------------------------------------------------
    def test_wiki_inbox(self) -> None:
        """Inbox lives at <corpus>/_inbox (visible, user-facing)."""
        assert wiki_inbox(_CORPUS) == _CORPUS / "_inbox"

    # ----------------------------------------------------------------
    # weave.py: wiki_sources — _retry_failed_sources(), weave(), weave_multi()
    # ----------------------------------------------------------------
    def test_wiki_sources(self) -> None:
        """Processed-sources dir lives at <corpus>/_sources (visible; was _archive)."""
        assert wiki_sources(_CORPUS) == _CORPUS / "_sources"

    # ----------------------------------------------------------------
    # cli.py: wiki_dashboard — _ensure_corpus_theme()
    # ----------------------------------------------------------------
    def test_wiki_dashboard(self) -> None:
        """Dashboard dir lives at <corpus>/.wiki/dashboard (hidden)."""
        assert wiki_dashboard(_CORPUS) == _CORPUS / ".wiki" / "dashboard"


class TestNoOldPaths:
    """Explicit regression: the OLD paths must NOT be produced by the helpers."""

    def test_ledger_not_at_old_path(self) -> None:
        """Old: <corpus>/.processed.jsonl — now hidden under .wiki/."""
        assert wiki_ledger(_CORPUS) != _CORPUS / ".processed.jsonl"

    def test_failed_not_at_old_path(self) -> None:
        """Old: <corpus>/_failed — now hidden under .wiki/."""
        assert wiki_failed(_CORPUS) != _CORPUS / "_failed"

    def test_sources_not_named_archive(self) -> None:
        """Old name was _archive — it is now _sources."""
        assert wiki_sources(_CORPUS) != _CORPUS / "_archive"

    def test_dashboard_not_at_old_path(self) -> None:
        """Old: <corpus>/.wiki-dashboard — now hidden under .wiki/dashboard."""
        assert wiki_dashboard(_CORPUS) != _CORPUS / ".wiki-dashboard"
