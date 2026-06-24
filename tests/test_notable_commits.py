"""Tests for the Notable Commits section (COVERAGE-STARVATION fix).

All tests are deterministic, fast, and require no network access.  Commits are
supplied as crafted dicts to ``_build_change_digest`` so no real git subprocess
is needed for the digest-level tests.  A dedicated integration test exercises the
``gitio.get_commits_name_only`` extension (date + author fields) against a real
tmp-path git repo.

Test matrix
-----------
NC1  Commit-only window (0 PRs, several real commits) → Notable Commits section
     appears with real SHAs, author names, and commit subjects.  The synthesizer
     now has concrete material instead of an empty stub.

NC2  PR-rich window (>= _NOTABLE_COMMITS_PR_THRESHOLD substantive PRs + many
     commits) → Notable Commits section is present but bounded at
     _NOTABLE_COMMITS_PR_RICH_CAP.  The full commit list is NOT duplicated.

NC3  Empty window (0 commits, 0 PRs) → no Notable Commits section fabricated
     (not even a section header with "none" — strictly no fabrication).

NC4  gitio extension: get_commits_name_only against a real git repo returns
     ``date`` and ``author`` fields in every commit dict.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch


from repo_weaver.materialize import (
    _NOTABLE_COMMITS_FULL_CAP,
    _NOTABLE_COMMITS_PR_RICH_CAP,
    _NOTABLE_COMMITS_PR_THRESHOLD,
    _build_change_digest,
)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_SINCE = "2024-01-01"
_UNTIL = "2024-06-30"


def _make_commit(
    sha: str,
    subject: str,
    author: str = "Jane Doe",
    date: str = "2024-06-01",
    paths: list[str] | None = None,
) -> dict[str, object]:
    """Craft a minimal commit dict matching the get_commits_name_only schema."""
    return {
        "hash": sha,
        "date": date,
        "author": author,
        "subject": subject,
        "paths": paths or ["src/main.py"],
    }


def _make_pr(
    number: int,
    title: str = "feat: do something",
    author: str = "alice",
    merged_at: str = "2024-06-15T10:00:00Z",
) -> dict[str, object]:
    """Craft a minimal substantive PR dict."""
    return {
        "number": number,
        "title": title,
        "author": {"login": author},
        "mergedAt": merged_at,
        "body": f"This is PR {number}.",
        "files": [{"path": "src/thing.py"}],
    }


def _build_digest(
    commits: list[dict[str, object]],
    prs: list[dict[str, object]],
) -> str:
    """Call _build_change_digest with patched gh and authors; no network."""
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
            since=_SINCE,
            until=_UNTIL,
            until_rev=None,
            commits=commits,
            owner_repo=("example-owner", "example-repo"),
            max_prs=15,
        )


# ---------------------------------------------------------------------------
# NC1 — commit-only window: Notable Commits appears with real data
# ---------------------------------------------------------------------------


def test_commit_only_window_notable_commits_present() -> None:
    """0 PRs + several commits → Notable Commits section with concrete substance.

    Verifies that a repo developed via direct commits (no merged PRs) is no
    longer digest-starved.  The section must list each commit's short SHA,
    author name, and subject line so the LLM synthesizer has real material.
    """
    commits = [
        _make_commit(
            "aabbccdd1234567",
            "feat(auth): add OAuth 2.0 PKCE login flow",
            author="Alice Smith",
            date="2024-06-20",
            paths=["auth/oauth.py", "auth/tests.py"],
        ),
        _make_commit(
            "1122334455667788",
            "fix(api): handle null response from upstream service",
            author="Bob Jones",
            date="2024-06-18",
            paths=["api/client.py"],
        ),
        _make_commit(
            "deadbeef99999999",
            "refactor: simplify config loader to use stdlib tomllib",
            author="Carol White",
            date="2024-06-15",
            paths=["config.py"],
        ),
    ]

    digest = _build_digest(commits, prs=[])

    # Section header must be present.
    assert "## Notable Commits" in digest, (
        "Expected '## Notable Commits' section for a commit-only window; "
        f"digest snippet:\n{digest[:800]!r}"
    )

    # Each commit's short SHA (first 7 chars) must appear.
    for commit in commits:
        sha7 = str(commit["hash"])[:7]
        assert sha7 in digest, (
            f"Short SHA '{sha7}' for commit {commit['subject']!r} not found in digest; "
            f"digest snippet:\n{digest[:1200]!r}"
        )

    # Each commit's subject must appear.
    for commit in commits:
        subject = str(commit["subject"])
        assert subject in digest, (
            f"Subject {subject!r} not found in Notable Commits section; "
            f"digest snippet:\n{digest[:1200]!r}"
        )

    # Each commit's author must appear.
    for commit in commits:
        author = str(commit["author"])
        assert author in digest, (
            f"Author {author!r} not found in Notable Commits section; "
            f"digest snippet:\n{digest[:1200]!r}"
        )

    # Confirm the section is genuinely substantive — not just a header with "none".
    assert "## Commit Volume Summary" in digest, (
        "Commit Volume Summary section should still be present"
    )


def test_commit_only_noise_commits_excluded() -> None:
    """Merge commits and bot commits must be excluded from Notable Commits.

    Only the one substantive commit should appear; the merge commit and bot
    commit must not inflate the section.
    """
    commits = [
        _make_commit(
            "aaaa000011111111",
            "feat(ui): add dark mode toggle",
            author="Dev User",
        ),
        _make_commit(
            "bbbb000022222222",
            "Merge pull request #42 from org/feature-branch",
            author="github-actions[bot]",
        ),
        _make_commit(
            "cccc000033333333",
            "chore: release v1.2.0",
            author="release-please[bot]",
        ),
    ]

    digest = _build_digest(commits, prs=[])

    assert "## Notable Commits" in digest, "Notable Commits section must appear"

    # Substantive commit must be listed.
    assert "aaaa000" in digest, "Substantive commit SHA must appear"
    assert "feat(ui): add dark mode toggle" in digest, "Substantive subject must appear"

    # Merge commit must be excluded.
    assert "bbbb000" not in digest, (
        "Merge commit SHA must not appear in Notable Commits"
    )
    assert "Merge pull request" not in digest, (
        "Merge commit subject must not appear in Notable Commits"
    )

    # Bot release commit must be excluded.
    assert "cccc000" not in digest, (
        "Bot release commit SHA must not appear in Notable Commits"
    )


# ---------------------------------------------------------------------------
# NC2 — PR-rich window: Notable Commits is bounded (cap enforced)
# ---------------------------------------------------------------------------


def test_pr_rich_window_notable_commits_capped() -> None:
    """PR-rich window (>= threshold substantive PRs) → Notable Commits bounded.

    Verifies that the section is present but the commit list is capped at
    _NOTABLE_COMMITS_PR_RICH_CAP so a PR-rich digest is not bloated with
    redundant commit lines.  All 50 commits must NOT be listed.
    """
    # Build _NOTABLE_COMMITS_PR_THRESHOLD or more substantive PRs.
    prs = [
        _make_pr(
            i,
            title=f"feat(area{i}): change number {i}",
            merged_at=f"2024-06-{i + 1:02d}T10:00:00Z",
        )
        for i in range(1, _NOTABLE_COMMITS_PR_THRESHOLD + 2)  # threshold+1 → above gate
    ]

    # Supply far more commits than the rich-cap.
    # Use SHAs whose first-7 hex chars are unique per commit: f"{i:07x}" + padding.
    # This ensures the in-digest check for each SHA prefix is unambiguous.
    many_commits = [  # noqa: E501
        _make_commit(
            f"{i:07x}{'a' * 33}",  # 40-char SHA; first 7 chars unique per i
            f"fix(module{i}): tweak number {i}",
            author=f"Dev{i}",
            date=f"2024-06-{(i % 28) + 1:02d}",
        )
        for i in range(50)
    ]

    digest = _build_digest(many_commits, prs=prs)

    # Section must be present (still informative even with PRs).
    assert "## Notable Commits" in digest, (
        "Notable Commits section must appear even in a PR-rich window; "
        f"digest snippet:\n{digest[:600]!r}"
    )

    # Count how many of the 50 unique SHA prefixes (f"{i:07x}") appear in the digest.
    # The Notable Commits section displays the first 7 chars of each hash.
    # Because our SHAs start with f"{i:07x}", each occurrence maps to exactly one commit.
    notable_lines_count = sum(1 for i in range(50) if f"{i:07x}" in digest)

    assert notable_lines_count <= _NOTABLE_COMMITS_PR_RICH_CAP, (
        f"PR-rich window must cap Notable Commits at {_NOTABLE_COMMITS_PR_RICH_CAP}, "
        f"but found {notable_lines_count} commit SHA prefixes in the digest"
    )

    # Confirm the full 50 are NOT all listed (that would defeat the cap).
    assert notable_lines_count < 50, (
        "All 50 commits were listed in a PR-rich digest - cap not enforced"
    )


def test_sparse_pr_window_notable_commits_fuller() -> None:
    """Sparse-PR window (< threshold substantive PRs) uses the full cap.

    With only 1 PR and 15 substantive commits, all 15 should appear
    (15 < _NOTABLE_COMMITS_FULL_CAP).
    """
    prs = [_make_pr(1)]  # only 1 → below _NOTABLE_COMMITS_PR_THRESHOLD

    commits = [
        _make_commit(
            f"sparse{i:010x}",
            f"feat(comp{i}): implement feature {i}",
            author="Alice",
            date=f"2024-06-{i + 1:02d}",
        )
        for i in range(15)
    ]

    digest = _build_digest(commits, prs=prs)

    assert "## Notable Commits" in digest, "Notable Commits section must appear"

    found_count = sum(1 for i in range(15) if f"sparse{i:010x}"[:7] in digest)
    assert found_count == 15, (
        f"Expected all 15 commits in sparse-PR window but found {found_count}; "
        f"full cap is {_NOTABLE_COMMITS_FULL_CAP}"
    )


# ---------------------------------------------------------------------------
# NC3 — empty window: no fabricated Notable Commits section
# ---------------------------------------------------------------------------


def test_empty_window_no_notable_commits_fabricated() -> None:
    """0 commits + 0 PRs → Notable Commits section must NOT appear.

    The synthesizer must never receive a fabricated section.  An empty window
    should produce no Notable Commits output at all — not even a placeholder.
    """
    digest = _build_digest(commits=[], prs=[])

    assert "## Notable Commits" not in digest, (
        "Notable Commits section must NOT appear for an empty window — "
        "no fabricated entries allowed; "
        f"digest snippet:\n{digest[:800]!r}"
    )

    # Sanity: the digest should still contain the standard sections.
    assert "## Commit Volume Summary" in digest, (
        "Commit Volume Summary must still be present for an empty window"
    )
    assert "Total commits in window: 0" in digest, (
        "Volume summary must report 0 commits for an empty window"
    )


def test_only_noise_commits_no_notable_commits_section() -> None:
    """Window with only merge/bot/release commits → no Notable Commits section.

    After noise filtering, the substantive list is empty, so the section header
    must not be emitted (avoids a "## Notable Commits\n\n_(none)_" stub).
    """
    noise_commits = [
        _make_commit(
            "aaaa111122223333", "Merge pull request #1 from org/branch", author="github"
        ),
        _make_commit(
            "bbbb111122223333", "chore: release v2.0.0", author="release-please[bot]"
        ),
        _make_commit(
            "cccc111122223333", "Merge branch 'main' into feature", author="ci-bot"
        ),
    ]

    digest = _build_digest(noise_commits, prs=[])

    assert "## Notable Commits" not in digest, (
        "Notable Commits section must NOT appear when all commits are noise; "
        f"digest snippet:\n{digest[:800]!r}"
    )


# ---------------------------------------------------------------------------
# NC4 — gitio integration: get_commits_name_only returns date + author
# ---------------------------------------------------------------------------


def _init_git_repo(path: Path, author_name: str = "Test Author") -> None:
    """Create a minimal local git repo with one commit."""
    path.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": author_name,
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": author_name,
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    subprocess.run(["git", "init", str(path)], capture_output=True, check=False)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@example.com"],
        capture_output=True,
        check=False,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", author_name],
        capture_output=True,
        check=False,
    )
    # Create a file so the commit is non-empty.
    (path / "hello.txt").write_text("hello\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(path), "add", "hello.txt"],
        capture_output=True,
        check=False,
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "feat: initial implementation"],
        capture_output=True,
        env=env,
        check=False,
    )


def test_get_commits_name_only_includes_date_and_author(tmp_path: Path) -> None:
    """get_commits_name_only returns 'date' and 'author' fields (gitio extension).

    Verifies that the new git format ``%cs\\t%an\\t%s`` is parsed correctly and
    the returned dicts carry the extra fields required by _format_notable_commits_section.
    """
    from repo_weaver import gitio

    author = "Notable Author"
    repo = tmp_path / "testrepo"
    _init_git_repo(repo, author_name=author)

    # Use a wide window that covers today's commit.
    commits = gitio.get_commits_name_only(str(repo), "2000-01-01", "2099-12-31")

    assert len(commits) >= 1, f"Expected at least one commit; got {commits}"

    c = commits[0]
    assert "date" in c, (
        f"Commit dict must contain 'date' key after gitio extension; keys: {list(c.keys())}"
    )
    assert "author" in c, (
        f"Commit dict must contain 'author' key after gitio extension; keys: {list(c.keys())}"
    )
    assert "hash" in c and "subject" in c and "paths" in c, (
        "Existing fields (hash, subject, paths) must still be present"
    )

    # The date should look like YYYY-MM-DD.
    date_val = str(c["date"])
    assert len(date_val) == 10 and date_val[4] == "-" and date_val[7] == "-", (
        f"Expected YYYY-MM-DD date format, got {date_val!r}"
    )

    # Author should match what we configured.
    assert str(c["author"]) == author, (
        f"Expected author {author!r}, got {c['author']!r}"
    )

    # Subject should match the commit message.
    assert "feat: initial implementation" in str(c["subject"]), (
        f"Expected commit subject to contain the message; got {c['subject']!r}"
    )

    # paths should be a list containing 'hello.txt'.
    paths = c["paths"]
    assert isinstance(paths, list), "paths must be a list"
    assert any("hello.txt" in str(p) for p in paths), (
        f"Expected 'hello.txt' in paths; got {paths}"
    )
