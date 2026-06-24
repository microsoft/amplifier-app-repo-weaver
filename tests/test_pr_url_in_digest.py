"""Tests for Change 3: PR GitHub URL in the change digest.

Verifies that _build_change_digest() emits a ``**URL:**`` field for each
substantive PR when ``owner_repo`` is provided, so the synthesizer can
construct the log line:
  ``YYYY-MM-DD — PR #N (author) — https://github.com/…/pull/N — summary``

All tests are pure unit tests — no subprocess calls, no network.
"""

from __future__ import annotations

from unittest.mock import patch

from repo_weaver.materialize import _build_change_digest


# ---------------------------------------------------------------------------
# Synthetic PR fixtures
# ---------------------------------------------------------------------------

_SUBSTANTIVE_PR: dict[str, object] = {
    "number": 42,
    "title": "feat(auth): add OAuth 2.0 login flow",
    "author": {"login": "alice"},
    "mergedAt": "2026-06-20T10:00:00Z",
    "body": "Adds full OAuth 2.0 PKCE flow with token refresh.",
    "files": [{"path": "src/auth.py"}, {"path": "tests/test_auth.py"}],
}

_ROUTINE_PR: dict[str, object] = {
    "number": 1,
    "title": "chore(deps): Bump axios from 0.21.1 to 0.21.4",
    "author": {"login": "dependabot[bot]", "is_bot": True},
    "mergedAt": "2026-06-15T10:00:00Z",
    "body": "Bumps axios from 0.21.1 to 0.21.4.",
    "files": [{"path": "package.json"}],
}

_OWNER_REPO = ("example-owner", "example-repo")


def _make_digest(
    prs: list[dict[str, object]],
    owner_repo: object = _OWNER_REPO,
    classify: bool = True,
) -> str:
    """Call _build_change_digest with patched gh returning *prs*."""
    with (
        patch(
            "repo_weaver.materialize.gitio.gh_merged_prs",
            return_value=(prs, None),
        ),
        patch(
            "repo_weaver.materialize.gitio.get_shortlog_authors",
            return_value=[],
        ),
    ):
        return _build_change_digest(
            repo="/fake/repo",
            since="2026-01-01",
            until="2026-06-30",
            until_rev=None,
            commits=[],
            owner_repo=owner_repo,  # type: ignore[arg-type]
            max_prs=15,
            repo_qualifier=None,
            classify=classify,
        )


# ---------------------------------------------------------------------------
# URL presence in classified digest
# ---------------------------------------------------------------------------


def test_substantive_pr_includes_github_url():
    """Substantive PR detail block includes a GitHub URL line when owner_repo set."""
    digest = _make_digest([_SUBSTANTIVE_PR], classify=True)

    expected_url = "https://github.com/example-owner/example-repo/pull/42"
    assert expected_url in digest, (
        f"Expected URL {expected_url!r} in classified digest; "
        f"snippet: {digest[:1200]!r}"
    )
    assert "- **URL:**" in digest, "Expected '- **URL:**' field in PR detail block"


def test_url_contains_correct_pr_number():
    """The URL includes the exact PR number from the PR dict."""
    digest = _make_digest([_SUBSTANTIVE_PR], classify=True)
    # PR #42 must appear as /pull/42 in the URL
    assert "/pull/42" in digest, (
        f"URL must end with /pull/42 for PR #42; snippet: {digest[:1200]!r}"
    )


# ---------------------------------------------------------------------------
# URL in no-classify mode (all PRs listed with full detail)
# ---------------------------------------------------------------------------


def test_url_present_in_no_classify_mode():
    """Under --no-classify, ALL PRs (including routine) get a URL line."""
    digest = _make_digest([_SUBSTANTIVE_PR, _ROUTINE_PR], classify=False)

    assert "https://github.com/example-owner/example-repo/pull/42" in digest, (
        "Substantive PR URL must appear in --no-classify digest"
    )
    assert "https://github.com/example-owner/example-repo/pull/1" in digest, (
        "Routine PR URL must appear in --no-classify digest (all PRs get full detail)"
    )


# ---------------------------------------------------------------------------
# No URL when owner_repo is None (single-repo without configured remote)
# ---------------------------------------------------------------------------


def test_no_url_when_owner_repo_is_none():
    """When owner_repo is None (no GitHub remote), no URL field is emitted."""
    digest = _make_digest([_SUBSTANTIVE_PR], owner_repo=None, classify=True)

    assert "- **URL:**" not in digest, (
        "URL field must not be emitted when owner_repo is None (no GitHub remote)"
    )
    assert "https://github.com" not in digest, (
        "No GitHub URL should appear in digest without owner_repo"
    )


# ---------------------------------------------------------------------------
# Routine PRs are still collapsed in classified mode
# ---------------------------------------------------------------------------


def test_routine_pr_still_collapsed_in_classified_mode():
    """Routine PR does NOT get a detail block in classified mode (URL or otherwise).

    This verifies that the URL addition doesn't change the existing
    routine-PR-collapse behaviour.
    """
    digest = _make_digest([_SUBSTANTIVE_PR, _ROUTINE_PR], classify=True)

    assert "### PR #1:" not in digest, (
        "Routine PR #1 must still be collapsed (not given a full detail block) "
        "when classify=True, even with URL support added"
    )
