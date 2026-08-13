"""Tests for the injectable GitHub-fetcher seam (``repo_weaver.fetcher``).

Test matrix
-----------
F1  materialize() with no ``fetcher`` argument constructs a ``GhCliFetcher``
    and behaves exactly as before -- the existing ``gitio.gh_*`` functions are
    invoked with the same arguments as pre-seam behaviour.
F2  materialize() with a custom ``fetcher`` supplied uses ONLY that fetcher
    for all four GitHub-API operations -- ``gitio.gh_*`` is never called.
F3  A fetcher that RAISES propagates the exception straight out of
    materialize() -- no degraded document is produced and swallowed silently.
    This is the important one: it verifies the raise-vs-degrade seam actually
    works, since materialize() must not wrap fetcher calls in try/except.
F4  ``gitio._run()`` returns a failure ``CompletedProcess`` (non-zero
    returncode, descriptive stderr) rather than raising on
    ``subprocess.TimeoutExpired``.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from repo_weaver import gitio
from repo_weaver.fetcher import GhCliFetcher
from repo_weaver.materialize import materialize

_OWNER_REPO_URL = "https://github.com/acme/widgets.git"

_FAKE_PR: dict[str, object] = {
    "number": 42,
    "title": "feat: add widget",
    "author": {"login": "alice"},
    "mergedAt": "2024-06-15T00:00:00Z",
    "body": "Adds a widget.",
    "files": [{"path": "src/widget.py"}],
}

_FAKE_ISSUE: dict[str, object] = {
    "number": 7,
    "title": "Bug: crash on empty input",
    "author": {"login": "bob"},
    "createdAt": "2024-06-10T00:00:00Z",
    "updatedAt": "2024-06-16T00:00:00Z",
    "body": "Passing an empty string crashes the parser.",
}


class _RecordingFetcher:
    """A minimal, hand-written GitHubFetcher (duck-typed, no inheritance needed
    since GitHubFetcher is a structural Protocol) that records every call it
    receives so tests can assert on exact arguments.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def gh_merged_prs(
        self, owner_repo: str, since: str, until: str, max_fetch: int = 120
    ) -> tuple[list[dict[str, object]], str | None]:
        self.calls.append(
            ("gh_merged_prs", (owner_repo, since, until), {"max_fetch": max_fetch})
        )
        return [_FAKE_PR], None

    def gh_pr_discussion(
        self, owner_repo: str, pr_number: int
    ) -> tuple[dict[str, list[dict[str, object]]], str | None]:
        self.calls.append(("gh_pr_discussion", (owner_repo, pr_number), {}))
        return {"comments": [], "reviews": []}, None

    def gh_issues(
        self, owner_repo: str, since: str, until: str, max_fetch: int = 120
    ) -> tuple[list[dict[str, object]], str | None]:
        self.calls.append(
            ("gh_issues", (owner_repo, since, until), {"max_fetch": max_fetch})
        )
        return [_FAKE_ISSUE], None

    def gh_issue_discussion(
        self, owner_repo: str, issue_number: int
    ) -> tuple[list[dict[str, object]], str | None]:
        self.calls.append(("gh_issue_discussion", (owner_repo, issue_number), {}))
        return [], None


class _RaisingFetcher:
    """A fetcher whose gh_merged_prs raises instead of returning (empty, error) --
    the raise-vs-degrade behaviour documented in repo_weaver/fetcher.py.
    """

    def gh_merged_prs(
        self, owner_repo: str, since: str, until: str, max_fetch: int = 120
    ) -> tuple[list[dict[str, object]], str | None]:
        raise RuntimeError("simulated rate-limit exhaustion")

    def gh_pr_discussion(
        self, owner_repo: str, pr_number: int
    ) -> tuple[dict[str, list[dict[str, object]]], str | None]:
        raise AssertionError("should never be reached -- gh_merged_prs raises first")

    def gh_issues(
        self, owner_repo: str, since: str, until: str, max_fetch: int = 120
    ) -> tuple[list[dict[str, object]], str | None]:
        raise AssertionError("should never be reached -- gh_merged_prs raises first")

    def gh_issue_discussion(
        self, owner_repo: str, issue_number: int
    ) -> tuple[list[dict[str, object]], str | None]:
        raise AssertionError("should never be reached -- gh_merged_prs raises first")


def _patched_git_local():
    """Patch the git-local gitio calls materialize() needs, common to all tests
    here -- no real git repo required."""
    return (
        patch("repo_weaver.materialize.gitio.get_origin_url", return_value=_OWNER_REPO_URL),
        patch("repo_weaver.materialize.gitio.get_window_rev", return_value=None),
        patch("repo_weaver.materialize.gitio.get_commits_name_only", return_value=[]),
        patch("repo_weaver.materialize.gitio.get_shortlog_authors", return_value=[]),
    )


# ---------------------------------------------------------------------------
# F1 -- default fetcher (no `fetcher=` argument) preserves today's behaviour
# ---------------------------------------------------------------------------


def test_materialize_default_fetcher_uses_gitio_gh_functions():
    p1, p2, p3, p4 = _patched_git_local()
    with (
        p1,
        p2,
        p3,
        p4,
        patch(
            "repo_weaver.materialize.gitio.gh_merged_prs",
            return_value=([_FAKE_PR], None),
        ) as mock_merged_prs,
        patch(
            "repo_weaver.materialize.gitio.gh_pr_discussion",
            return_value=({"comments": [], "reviews": []}, None),
        ) as mock_pr_discussion,
        patch(
            "repo_weaver.materialize.gitio.gh_issues",
            return_value=([_FAKE_ISSUE], None),
        ) as mock_issues,
        patch(
            "repo_weaver.materialize.gitio.gh_issue_discussion",
            return_value=([], None),
        ) as mock_issue_discussion,
    ):
        docs = materialize(
            repo="/fake/repo",
            since="2024-06-01",
            until="2024-06-30",
        )

    assert docs  # digest was produced

    mock_merged_prs.assert_called_once_with(
        "acme/widgets", "2024-06-01", "2024-06-30", max_fetch=120
    )
    mock_pr_discussion.assert_called_once_with("acme/widgets", 42)
    # GhCliFetcher.gh_issues has its own max_fetch=120 default and forwards it
    # explicitly to gitio.gh_issues -- functionally identical to the pre-seam
    # call (which relied on gh_issues' own default), just spelled out as a
    # keyword argument now that it passes through an extra layer.
    mock_issues.assert_called_once_with(
        "acme/widgets", "2024-06-01", "2024-06-30", max_fetch=120
    )
    mock_issue_discussion.assert_called_once_with("acme/widgets", 7)

    digest_content = docs[0][1]
    assert "PR #42" in digest_content
    assert "Issue #7" in digest_content


def test_materialize_none_fetcher_constructs_ghclifetcher_instance():
    """Sanity check that GhCliFetcher is genuinely a thin pass-through with
    the exact same method names as the gitio functions it wraps."""
    fetcher = GhCliFetcher()
    assert hasattr(fetcher, "gh_merged_prs")
    assert hasattr(fetcher, "gh_pr_discussion")
    assert hasattr(fetcher, "gh_issues")
    assert hasattr(fetcher, "gh_issue_discussion")

    with patch.object(
        gitio, "gh_merged_prs", return_value=([_FAKE_PR], None)
    ) as mock_fn:
        result = fetcher.gh_merged_prs("acme/widgets", "2024-06-01", "2024-06-30")
        mock_fn.assert_called_once_with(
            "acme/widgets", "2024-06-01", "2024-06-30", max_fetch=120
        )
        assert result == ([_FAKE_PR], None)


# ---------------------------------------------------------------------------
# F2 -- a custom fetcher is used INSTEAD of gitio.gh_* when supplied
# ---------------------------------------------------------------------------


def test_materialize_custom_fetcher_bypasses_gitio_gh_functions():
    fetcher = _RecordingFetcher()

    def _fail_if_called(*args: object, **kwargs: object) -> None:
        raise AssertionError(
            "gitio.gh_* must NOT be called directly when a custom fetcher is supplied"
        )

    p1, p2, p3, p4 = _patched_git_local()
    with (
        p1,
        p2,
        p3,
        p4,
        patch("repo_weaver.materialize.gitio.gh_merged_prs", side_effect=_fail_if_called),
        patch("repo_weaver.materialize.gitio.gh_pr_discussion", side_effect=_fail_if_called),
        patch("repo_weaver.materialize.gitio.gh_issues", side_effect=_fail_if_called),
        patch(
            "repo_weaver.materialize.gitio.gh_issue_discussion", side_effect=_fail_if_called
        ),
    ):
        docs = materialize(
            repo="/fake/repo",
            since="2024-06-01",
            until="2024-06-30",
            fetcher=fetcher,
        )

    assert docs
    digest_content = docs[0][1]
    assert "PR #42" in digest_content
    assert "Issue #7" in digest_content

    call_names = [c[0] for c in fetcher.calls]
    assert "gh_merged_prs" in call_names
    assert "gh_pr_discussion" in call_names
    assert "gh_issues" in call_names
    assert "gh_issue_discussion" in call_names

    merged_prs_call = next(c for c in fetcher.calls if c[0] == "gh_merged_prs")
    assert merged_prs_call[1] == ("acme/widgets", "2024-06-01", "2024-06-30")

    pr_discussion_call = next(c for c in fetcher.calls if c[0] == "gh_pr_discussion")
    assert pr_discussion_call[1] == ("acme/widgets", 42)

    issues_call = next(c for c in fetcher.calls if c[0] == "gh_issues")
    assert issues_call[1] == ("acme/widgets", "2024-06-01", "2024-06-30")

    issue_discussion_call = next(
        c for c in fetcher.calls if c[0] == "gh_issue_discussion"
    )
    assert issue_discussion_call[1] == ("acme/widgets", 7)


# ---------------------------------------------------------------------------
# F3 -- a fetcher that raises propagates out of materialize() (no degraded doc)
# ---------------------------------------------------------------------------


def test_materialize_fetcher_raise_propagates_no_degraded_document():
    fetcher = _RaisingFetcher()

    p1, p2, p3, p4 = _patched_git_local()
    with p1, p2, p3, p4:
        with pytest.raises(RuntimeError, match="simulated rate-limit exhaustion"):
            materialize(
                repo="/fake/repo",
                since="2024-06-01",
                until="2024-06-30",
                fetcher=fetcher,
            )


# ---------------------------------------------------------------------------
# F4 -- gitio._run() never raises on subprocess timeout
# ---------------------------------------------------------------------------


def test_run_returns_failure_result_on_timeout(monkeypatch: pytest.MonkeyPatch):
    import subprocess as subprocess_module

    def fake_run(*args: object, **kwargs: object):
        raise subprocess_module.TimeoutExpired(cmd=["git", "slow-op"], timeout=0.001)

    monkeypatch.setattr(gitio.subprocess, "run", fake_run)

    result = gitio._run(["git", "slow-op"], timeout=0.001)

    assert result.returncode != 0
    assert "timed out" in result.stderr


def test_run_timeout_end_to_end_with_real_subprocess():
    """Belt-and-suspenders: use a REAL subprocess (no mocking) that outlives a
    tiny timeout, to prove the TimeoutExpired path is wired correctly end to
    end, not just when subprocess.run is mocked out."""
    result = gitio._run(["sleep", "5"], timeout=0.2)

    assert result.returncode != 0
    assert "timed out" in result.stderr
