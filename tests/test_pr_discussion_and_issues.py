"""Tests for PR comments/reviews + GitHub Issues as corpus content and as
change-detection triggers.

Test matrix
-----------
GATE1  gh_most_recent_update() invokes the correct `gh <pr|issue> list`
       command and parses the single most-recently-updated item's date.
GATE2  gh_most_recent_update() treats "gh succeeded, zero items" as
       (None, None) -- NOT an error.
GATE3  gh_most_recent_update() surfaces a genuine gh failure as (None, error).
GATE4  gh_most_recent_update() treats "Issues disabled for this repo" (a
       permanent, benign repo config, kind="issue" only) as (None, None) --
       NOT an error -- while still surfacing other, real failures normally
       and never applying this leniency to kind="pr".

SYNCD1 sync.py's CHANGED-selection loop marks a repo CHANGED when its own
       pushedAt is unchanged but a PR/issue's updatedAt moved past the
       repo's last-sync watermark (the discussion-activity trigger).
SYNCD2 A gh_most_recent_update failure is recorded loudly in `errors` but
       does not abort detection for other repos/owners.

DISC1  gh_pr_discussion() invokes `gh pr view <n> --json comments,reviews`
       and returns the parsed {"comments": [...], "reviews": [...]} shape.
DISC2  gh_pr_discussion() surfaces a gh failure as ({"comments": [], "reviews": []}, error).
DISC3  gh_issues() invokes `gh issue list ... --search "updated:<since>..<until> ..."`
       and filters to the window client-side.
DISC4  gh_issues() surfaces a gh failure as ([], error).
DISC5  gh_issue_discussion() invokes `gh issue view <n> --json comments`
       and returns the parsed comment list.

CONTENT1  _append_pr_detail() renders a **Discussion:** subsection for PR
          comments and a **Reviews:** subsection for PR reviews.
CONTENT2  _append_pr_detail() surfaces a discussion fetch error explicitly
          rather than silently omitting the subsections.
CONTENT3  _build_change_digest() emits a "## Issues" section with full
          per-issue detail (including nested Discussion) when issues exist
          in the window.
CONTENT4  _build_change_digest() emits the "(No issues found...)" placeholder
          when the window has zero issues -- not a fabricated section.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any
from unittest.mock import patch

import pytest

from repo_weaver import gitio, sync
from repo_weaver.materialize import _append_pr_detail, _build_change_digest

# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------


def _completed(
    returncode: int, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["gh"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _write_source(corpus, filename: str) -> None:
    sources = corpus / "_sources"
    sources.mkdir(parents=True, exist_ok=True)
    (sources / filename).write_text("dummy content", encoding="utf-8")


# ---------------------------------------------------------------------------
# GATE -- gitio.gh_most_recent_update
# ---------------------------------------------------------------------------


def test_gh_most_recent_update_pr_invokes_correct_command_and_parses_date(
    monkeypatch: pytest.MonkeyPatch,
):
    captured_cmd: list[str] = []

    def fake_run_gh_with_retry(
        cmd: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        captured_cmd.extend(cmd)
        return _completed(0, stdout=json.dumps([{"updatedAt": "2026-07-08T19:04:23Z"}]))

    monkeypatch.setattr(gitio, "_run_gh_with_retry", fake_run_gh_with_retry)

    date_str, error = gitio.gh_most_recent_update("o/r", "pr")

    assert error is None
    assert date_str == "2026-07-08"
    assert captured_cmd == [
        "gh",
        "pr",
        "list",
        "--repo",
        "o/r",
        "--state",
        "all",
        "--json",
        "updatedAt",
        "--limit",
        "1",
        "--search",
        "sort:updated-desc",
    ]


def test_gh_most_recent_update_issue_invokes_issue_list(
    monkeypatch: pytest.MonkeyPatch,
):
    captured_cmd: list[str] = []

    def fake_run_gh_with_retry(
        cmd: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        captured_cmd.extend(cmd)
        return _completed(0, stdout=json.dumps([{"updatedAt": "2026-07-01T00:00:00Z"}]))

    monkeypatch.setattr(gitio, "_run_gh_with_retry", fake_run_gh_with_retry)

    date_str, error = gitio.gh_most_recent_update("o/r", "issue")

    assert error is None
    assert date_str == "2026-07-01"
    assert captured_cmd[:3] == ["gh", "issue", "list"]


def test_gh_most_recent_update_zero_items_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
):
    def fake_run_gh_with_retry(
        cmd: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        return _completed(0, stdout="[]")

    monkeypatch.setattr(gitio, "_run_gh_with_retry", fake_run_gh_with_retry)

    date_str, error = gitio.gh_most_recent_update("o/r", "pr")
    assert date_str is None
    assert error is None, "gh succeeded with zero items -- must not be an error."


def test_gh_most_recent_update_empty_stdout_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
):
    def fake_run_gh_with_retry(
        cmd: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        return _completed(0, stdout="")

    monkeypatch.setattr(gitio, "_run_gh_with_retry", fake_run_gh_with_retry)

    date_str, error = gitio.gh_most_recent_update("o/r", "issue")
    assert date_str is None
    assert error is None


def test_gh_most_recent_update_surfaces_genuine_gh_failure(
    monkeypatch: pytest.MonkeyPatch,
):
    def fake_run_gh_with_retry(
        cmd: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        return _completed(1, stderr="gh: authentication failed")

    monkeypatch.setattr(gitio, "_run_gh_with_retry", fake_run_gh_with_retry)

    date_str, error = gitio.gh_most_recent_update("o/r", "pr")
    assert date_str is None
    assert error is not None
    assert "authentication failed" in error


def test_gh_most_recent_update_issues_disabled_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
):
    """GATE4: a repo with GitHub Issues disabled is a permanent, benign repo
    configuration -- not a transient failure. It must fold into the same
    (None, None) "no signal" case as genuinely-zero-issues.

    Error text confirmed LIVE against a real repo with Issues disabled
    (``gh issue list --repo torvalds/linux --state all --json updatedAt
    --limit 1 --search "sort:updated-desc"``), which produced exactly:
    "the 'torvalds/linux' repository has disabled issues"
    """

    def fake_run_gh_with_retry(
        cmd: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        return _completed(
            1, stderr="the 'torvalds/linux' repository has disabled issues"
        )

    monkeypatch.setattr(gitio, "_run_gh_with_retry", fake_run_gh_with_retry)

    date_str, error = gitio.gh_most_recent_update("torvalds/linux", "issue")
    assert date_str is None
    assert error is None, "Issues-disabled must NOT be surfaced as an error."


def test_gh_most_recent_update_issues_disabled_graphql_wording_variant(
    monkeypatch: pytest.MonkeyPatch,
):
    """The GraphQL-flavored wording variant must also be recognized, not just
    the exact REST-style string observed live."""

    def fake_run_gh_with_retry(
        cmd: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        return _completed(
            1,
            stderr="GraphQL: Issues have been disabled for this repo (repository.issues)",
        )

    monkeypatch.setattr(gitio, "_run_gh_with_retry", fake_run_gh_with_retry)

    date_str, error = gitio.gh_most_recent_update("o/r", "issue")
    assert date_str is None
    assert error is None


def test_gh_most_recent_update_issues_disabled_detection_scoped_to_issue_kind(
    monkeypatch: pytest.MonkeyPatch,
):
    """The issues-disabled detection must never apply to kind="pr" -- PRs
    can't be individually disabled the same way, so a similarly-shaped
    error on a "pr" call must still surface as a real error."""

    def fake_run_gh_with_retry(
        cmd: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        return _completed(1, stderr="the 'o/r' repository has disabled issues")

    monkeypatch.setattr(gitio, "_run_gh_with_retry", fake_run_gh_with_retry)

    date_str, error = gitio.gh_most_recent_update("o/r", "pr")
    assert date_str is None
    assert error is not None
    assert "disabled issues" in error


def test_gh_most_recent_update_does_not_overbroaden_match_for_real_failures(
    monkeypatch: pytest.MonkeyPatch,
):
    """A genuinely different transient failure (rate limit) must still
    surface as a real error -- the issues-disabled match must not be so
    broad it swallows unrelated failures."""

    def fake_run_gh_with_retry(
        cmd: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        return _completed(1, stderr="API rate limit exceeded for user ID 12345.")

    monkeypatch.setattr(gitio, "_run_gh_with_retry", fake_run_gh_with_retry)

    date_str, error = gitio.gh_most_recent_update("o/r", "issue")
    assert date_str is None
    assert error is not None
    assert "rate limit exceeded" in error


def test_gh_most_recent_update_command_not_found_still_surfaces_as_error(
    monkeypatch: pytest.MonkeyPatch,
):
    def fake_run_gh_with_retry(
        cmd: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        return _completed(127, stderr="gh: command not found")

    monkeypatch.setattr(gitio, "_run_gh_with_retry", fake_run_gh_with_retry)

    date_str, error = gitio.gh_most_recent_update("o/r", "issue")
    assert date_str is None
    assert error is not None
    assert "command not found" in error


# ---------------------------------------------------------------------------
# SYNCD -- sync.py CHANGED-selection gating on discussion activity
# ---------------------------------------------------------------------------


def _make_corpus_with_one_tracked_repo(tmp_path):
    corpus = tmp_path / "corpus"
    _write_source(corpus, "microsoft__amplifier-app-repo-weaver-2026-06-25-changes.md")
    return corpus


def test_sync_marks_repo_changed_via_pr_discussion_activity_despite_unchanged_pushed_at(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """SYNCD1: pushedAt is BEFORE last-sync (would be "unchanged" under the old
    push-only signal), but a PR's own updatedAt moved past last-sync -- this
    must still mark the repo CHANGED.
    """
    corpus = _make_corpus_with_one_tracked_repo(tmp_path)

    def fake_gh_list_repos(
        owner: str, include_forks: bool = True, visibility: str = "all"
    ) -> tuple[list[dict[str, Any]], None]:
        return [
            {
                "name": "amplifier-app-repo-weaver",
                "isFork": False,
                "pushedAt": "2026-06-20T00:00:00Z",  # BEFORE last-sync (2026-06-25)
                "nameWithOwner": "microsoft/amplifier-app-repo-weaver",
            }
        ], None

    def fake_gh_most_recent_update(
        owner_repo: str, kind: str
    ) -> tuple[str | None, str | None]:
        if kind == "pr":
            # A PR was commented on/reviewed AFTER last-sync -- the trigger.
            return "2026-07-01", None
        return None, None  # no issues at all

    monkeypatch.setattr(gitio, "gh_list_repos", fake_gh_list_repos)
    monkeypatch.setattr(gitio, "gh_most_recent_update", fake_gh_most_recent_update)

    result = sync.sync_corpus(
        corpus=str(corpus),
        clones_dir=str(tmp_path / "clones"),
        dry_run=True,
    )

    changed_names = {e["nameWithOwner"] for e in result["changed"]}
    assert changed_names == {"microsoft/amplifier-app-repo-weaver"}, (
        "A repo with unchanged pushedAt but a moved PR updatedAt must be "
        "detected as CHANGED via the discussion-activity gate."
    )
    assert result["errors"] == []


def test_sync_marks_repo_changed_via_issue_discussion_activity(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """Same as above but the trigger is an ISSUE's updatedAt, not a PR's."""
    corpus = _make_corpus_with_one_tracked_repo(tmp_path)

    def fake_gh_list_repos(
        owner: str, include_forks: bool = True, visibility: str = "all"
    ) -> tuple[list[dict[str, Any]], None]:
        return [
            {
                "name": "amplifier-app-repo-weaver",
                "isFork": False,
                "pushedAt": "2026-06-20T00:00:00Z",
                "nameWithOwner": "microsoft/amplifier-app-repo-weaver",
            }
        ], None

    def fake_gh_most_recent_update(
        owner_repo: str, kind: str
    ) -> tuple[str | None, str | None]:
        if kind == "issue":
            return "2026-07-02", None
        return None, None

    monkeypatch.setattr(gitio, "gh_list_repos", fake_gh_list_repos)
    monkeypatch.setattr(gitio, "gh_most_recent_update", fake_gh_most_recent_update)

    result = sync.sync_corpus(
        corpus=str(corpus),
        clones_dir=str(tmp_path / "clones"),
        dry_run=True,
    )

    changed_names = {e["nameWithOwner"] for e in result["changed"]}
    assert changed_names == {"microsoft/amplifier-app-repo-weaver"}


def test_sync_no_discussion_activity_and_no_push_leaves_repo_unchanged(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """Negative case: neither pushedAt nor PR/issue updatedAt moved -> NOT changed."""
    corpus = _make_corpus_with_one_tracked_repo(tmp_path)

    def fake_gh_list_repos(
        owner: str, include_forks: bool = True, visibility: str = "all"
    ) -> tuple[list[dict[str, Any]], None]:
        return [
            {
                "name": "amplifier-app-repo-weaver",
                "isFork": False,
                "pushedAt": "2026-06-20T00:00:00Z",
                "nameWithOwner": "microsoft/amplifier-app-repo-weaver",
            }
        ], None

    monkeypatch.setattr(gitio, "gh_list_repos", fake_gh_list_repos)
    monkeypatch.setattr(
        gitio, "gh_most_recent_update", lambda owner_repo, kind: (None, None)
    )

    result = sync.sync_corpus(
        corpus=str(corpus),
        clones_dir=str(tmp_path / "clones"),
        dry_run=True,
    )

    assert result["changed"] == []


def test_sync_gh_most_recent_update_failure_recorded_loudly_but_does_not_abort(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """SYNCD2: a genuine gh failure from the gating check is surfaced in
    `errors` (fail-loud, matching this codebase's established convention)
    but does not raise/crash -- the repo's pushedAt signal (if any) still
    applies, and detection for other repos is unaffected.
    """
    corpus = _make_corpus_with_one_tracked_repo(tmp_path)

    def fake_gh_list_repos(
        owner: str, include_forks: bool = True, visibility: str = "all"
    ) -> tuple[list[dict[str, Any]], None]:
        return [
            {
                "name": "amplifier-app-repo-weaver",
                "isFork": False,
                "pushedAt": "2026-07-01T00:00:00Z",  # pushed AFTER last-sync anyway
                "nameWithOwner": "microsoft/amplifier-app-repo-weaver",
            }
        ], None

    def fake_gh_most_recent_update(
        owner_repo: str, kind: str
    ) -> tuple[str | None, str | None]:
        return None, "gh error: rate limit exceeded"

    monkeypatch.setattr(gitio, "gh_list_repos", fake_gh_list_repos)
    monkeypatch.setattr(gitio, "gh_most_recent_update", fake_gh_most_recent_update)

    result = sync.sync_corpus(
        corpus=str(corpus),
        clones_dir=str(tmp_path / "clones"),
        dry_run=True,
    )

    # pushedAt alone still triggers CHANGED -- the gating failure doesn't mask it.
    changed_names = {e["nameWithOwner"] for e in result["changed"]}
    assert changed_names == {"microsoft/amplifier-app-repo-weaver"}
    # But the failure IS surfaced loudly (2 calls -- pr + issue -- both fail).
    assert any("rate limit exceeded" in e for e in result["errors"])


# ---------------------------------------------------------------------------
# DISC -- gh_pr_discussion / gh_issues / gh_issue_discussion
# ---------------------------------------------------------------------------


def test_gh_pr_discussion_invokes_correct_command_and_parses_shape(
    monkeypatch: pytest.MonkeyPatch,
):
    captured_cmd: list[str] = []

    def fake_run_gh_with_retry(
        cmd: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        captured_cmd.extend(cmd)
        return _completed(
            0,
            stdout=json.dumps(
                {
                    "comments": [{"author": {"login": "alice"}, "body": "hi"}],
                    "reviews": [
                        {"author": {"login": "bob"}, "state": "APPROVED", "body": ""}
                    ],
                }
            ),
        )

    monkeypatch.setattr(gitio, "_run_gh_with_retry", fake_run_gh_with_retry)

    discussion, error = gitio.gh_pr_discussion("o/r", 42)

    assert error is None
    assert captured_cmd == [
        "gh",
        "pr",
        "view",
        "42",
        "--repo",
        "o/r",
        "--json",
        "comments,reviews",
    ]
    assert len(discussion["comments"]) == 1
    assert discussion["comments"][0]["body"] == "hi"
    assert len(discussion["reviews"]) == 1
    assert discussion["reviews"][0]["state"] == "APPROVED"


def test_gh_pr_discussion_surfaces_gh_failure(monkeypatch: pytest.MonkeyPatch):
    def fake_run_gh_with_retry(
        cmd: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        return _completed(1, stderr="gh: not found")

    monkeypatch.setattr(gitio, "_run_gh_with_retry", fake_run_gh_with_retry)

    discussion, error = gitio.gh_pr_discussion("o/r", 42)
    assert discussion == {"comments": [], "reviews": []}
    assert error is not None
    assert "not found" in error


def test_gh_pr_discussion_empty_comments_and_reviews_is_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
):
    def fake_run_gh_with_retry(
        cmd: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        return _completed(0, stdout=json.dumps({"comments": [], "reviews": []}))

    monkeypatch.setattr(gitio, "_run_gh_with_retry", fake_run_gh_with_retry)

    discussion, error = gitio.gh_pr_discussion("o/r", 1)
    assert error is None
    assert discussion == {"comments": [], "reviews": []}


def test_gh_issues_invokes_correct_command_and_filters_window(
    monkeypatch: pytest.MonkeyPatch,
):
    captured_cmd: list[str] = []

    def fake_run_gh_with_retry(
        cmd: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        captured_cmd.extend(cmd)
        return _completed(
            0,
            stdout=json.dumps(
                [
                    {
                        "number": 1,
                        "title": "in window",
                        "updatedAt": "2026-07-05T00:00:00Z",
                    },
                    {
                        "number": 2,
                        "title": "out of window (too early)",
                        "updatedAt": "2026-06-01T00:00:00Z",
                    },
                ]
            ),
        )

    monkeypatch.setattr(gitio, "_run_gh_with_retry", fake_run_gh_with_retry)

    issues, error = gitio.gh_issues("o/r", "2026-07-01", "2026-07-08")

    assert error is None
    assert [i["number"] for i in issues] == [1]
    assert captured_cmd[:3] == ["gh", "issue", "list"]
    assert "--search" in captured_cmd
    search_idx = captured_cmd.index("--search")
    assert "updated:2026-07-01..2026-07-08" in captured_cmd[search_idx + 1]
    assert "number,title,body,createdAt,updatedAt,author" in captured_cmd


def test_gh_issues_surfaces_gh_failure(monkeypatch: pytest.MonkeyPatch):
    def fake_run_gh_with_retry(
        cmd: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        return _completed(1, stderr="gh: rate limit exceeded")

    monkeypatch.setattr(gitio, "_run_gh_with_retry", fake_run_gh_with_retry)

    issues, error = gitio.gh_issues("o/r", "2026-07-01", "2026-07-08")
    assert issues == []
    assert error is not None
    assert "rate limit exceeded" in error


def test_gh_issues_zero_issues_is_not_an_error(monkeypatch: pytest.MonkeyPatch):
    def fake_run_gh_with_retry(
        cmd: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        return _completed(0, stdout="[]")

    monkeypatch.setattr(gitio, "_run_gh_with_retry", fake_run_gh_with_retry)

    issues, error = gitio.gh_issues("o/r", "2026-07-01", "2026-07-08")
    assert issues == []
    assert error is None


def test_gh_issue_discussion_invokes_correct_command_and_parses_comments(
    monkeypatch: pytest.MonkeyPatch,
):
    captured_cmd: list[str] = []

    def fake_run_gh_with_retry(
        cmd: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        captured_cmd.extend(cmd)
        return _completed(
            0,
            stdout=json.dumps(
                {"comments": [{"author": {"login": "carol"}, "body": "me too"}]}
            ),
        )

    monkeypatch.setattr(gitio, "_run_gh_with_retry", fake_run_gh_with_retry)

    comments, error = gitio.gh_issue_discussion("o/r", 7)

    assert error is None
    assert captured_cmd == [
        "gh",
        "issue",
        "view",
        "7",
        "--repo",
        "o/r",
        "--json",
        "comments",
    ]
    assert comments[0]["body"] == "me too"


def test_gh_issue_discussion_surfaces_gh_failure(monkeypatch: pytest.MonkeyPatch):
    def fake_run_gh_with_retry(
        cmd: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        return _completed(1, stderr="gh: permission denied")

    monkeypatch.setattr(gitio, "_run_gh_with_retry", fake_run_gh_with_retry)

    comments, error = gitio.gh_issue_discussion("o/r", 7)
    assert comments == []
    assert error is not None
    assert "permission denied" in error


# ---------------------------------------------------------------------------
# CONTENT -- materialize.py shaping of discussion/reviews/issues
# ---------------------------------------------------------------------------

_SAMPLE_PR: dict[str, object] = {
    "number": 42,
    "title": "feat(auth): add PKCE flow",
    "author": {"login": "alice"},
    "mergedAt": "2026-07-01T00:00:00Z",
    "body": "Adds PKCE support.",
    "files": [{"path": "src/auth.py"}],
}


def test_append_pr_detail_renders_discussion_and_reviews_sections():
    parts: list[str] = []
    discussion = {
        "comments": [
            {
                "author": {"login": "bob"},
                "body": "Looks good, one nit.",
                "createdAt": "2026-07-01T10:00:00Z",
            }
        ],
        "reviews": [
            {
                "author": {"login": "carol"},
                "state": "APPROVED",
                "body": "LGTM",
                "submittedAt": "2026-07-01T12:00:00Z",
            }
        ],
    }
    _append_pr_detail(parts, _SAMPLE_PR, pr_url=None, discussion=discussion)
    text = "".join(parts)

    assert "### PR #42: feat(auth): add PKCE flow" in text
    assert "**Discussion:**" in text
    assert "**bob** (2026-07-01): Looks good, one nit." in text
    assert "**Reviews:**" in text
    assert "**carol** (2026-07-01) \u2014 APPROVED: LGTM" in text


def test_append_pr_detail_omits_discussion_sections_when_none_provided():
    parts: list[str] = []
    _append_pr_detail(parts, _SAMPLE_PR, pr_url=None, discussion=None)
    text = "".join(parts)
    assert "**Discussion:**" not in text
    assert "**Reviews:**" not in text


def test_append_pr_detail_empty_discussion_lists_produce_no_subsections():
    parts: list[str] = []
    _append_pr_detail(
        parts,
        _SAMPLE_PR,
        pr_url=None,
        discussion={"comments": [], "reviews": []},
    )
    text = "".join(parts)
    assert "**Discussion:**" not in text
    assert "**Reviews:**" not in text


def test_append_pr_detail_surfaces_discussion_fetch_error():
    parts: list[str] = []
    _append_pr_detail(
        parts,
        _SAMPLE_PR,
        pr_url=None,
        discussion=None,
        discussion_error="gh error: rate limit exceeded",
    )
    text = "".join(parts)
    assert "rate limit exceeded" in text
    assert "could not be fetched for this PR" in text


def _build_digest_with_issues(
    issues: list[dict[str, object]],
    issues_error: str | None = None,
) -> str:
    with (
        patch(
            "repo_weaver.materialize.gitio.gh_merged_prs",
            return_value=([], None),
        ),
        patch(
            "repo_weaver.materialize.gitio.get_shortlog_authors",
            return_value=[],
        ),
        patch(
            "repo_weaver.materialize.gitio.gh_issues",
            return_value=(issues, issues_error),
        ),
        patch(
            "repo_weaver.materialize.gitio.gh_issue_discussion",
            return_value=(
                [
                    {
                        "author": {"login": "dave"},
                        "body": "same issue here",
                        "createdAt": "2026-07-03T00:00:00Z",
                    }
                ],
                None,
            ),
        ),
    ):
        return _build_change_digest(
            repo="/fake/repo",
            since="2026-07-01",
            until="2026-07-08",
            until_rev=None,
            commits=[],
            owner_repo=("example-owner", "example-repo"),
            max_prs=15,
        )


def test_build_change_digest_emits_issues_section_with_discussion():
    issues = [
        {
            "number": 7,
            "title": "Crash on empty input",
            "author": {"login": "erin"},
            "createdAt": "2026-07-02T00:00:00Z",
            "updatedAt": "2026-07-03T00:00:00Z",
            "body": "Passing an empty string crashes the parser.",
        }
    ]
    digest = _build_digest_with_issues(issues)

    assert "## Issues (2026-07-01 \u2192 2026-07-08)" in digest
    assert "### Issue #7: Crash on empty input" in digest
    assert "- **Author:** erin" in digest
    assert "- **Created:** 2026-07-02" in digest
    assert "- **Updated:** 2026-07-03" in digest
    assert "Passing an empty string crashes the parser." in digest
    assert "**Discussion:**" in digest
    assert "**dave** (2026-07-03): same issue here" in digest


def test_build_change_digest_shows_placeholder_when_no_issues():
    digest = _build_digest_with_issues([])
    assert "## Issues (2026-07-01 \u2192 2026-07-08)" in digest
    assert "_(No issues found in this window.)_" in digest
    assert "### Issue #" not in digest


def test_build_change_digest_surfaces_issues_fetch_error():
    digest = _build_digest_with_issues(
        [], issues_error="gh error: authentication failed"
    )
    assert "authentication failed" in digest
    assert "issue data could not be fetched" in digest
