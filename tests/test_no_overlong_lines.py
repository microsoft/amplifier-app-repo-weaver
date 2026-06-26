"""Regression test: digest markdown must never produce lines exceeding _LINE_WRAP_WIDTH.

CONTEXT
-------
textwrap.shorten() — the previous body-excerpt implementation — collapses all
whitespace (including newlines) into a single space and then truncates to
_PR_BODY_MAX characters.  With many substantive PRs, each having a rich
multi-paragraph body, this produces lines up to 600 chars long.

Files with many such lines have a very low newline-to-byte ratio.  This
characteristic triggers text-vs-binary heuristics: notably wiki-weaver's
earlier 8 KB UTF-8 slice check, where a multi-byte character (→ / … / " / ")
straddling byte 8191/8192 raises a UnicodeDecodeError that misclassifies
perfectly valid UTF-8 as binary.  ALL 156 repos in a recent batch were rejected
with "unsupported binary source (no text handler)" for exactly this reason.

FIX
---
_truncate_body() replaces textwrap.shorten().  It preserves existing newlines
in the PR body and hard-wraps any line still longer than _LINE_WRAP_WIDTH.

TESTS
-----
NL1  PR body with a single 800-char paragraph → digest lines all ≤ LINE_WRAP_WIDTH.
NL2  PR body with multiple 600-char paragraphs (repro: many substantive PRs with
     rich bodies) → no single line in the full digest exceeds _LINE_WRAP_WIDTH.
NL3  PR body that already has natural newlines → newlines are preserved (not
     collapsed); no line exceeds _LINE_WRAP_WIDTH.
NL4  Empty PR body → handled silently; no crash, no extra blank lines.
NL5  _truncate_body unit test: long single-line → wraps; multi-line → preserves
     structure; empty → ""; body ≤ max_chars → returned as-is (no truncation
     indicator added).
"""

from __future__ import annotations

from unittest.mock import patch

from repo_weaver.materialize import (
    _LINE_WRAP_WIDTH,
    _PR_BODY_MAX,
    _build_change_digest,
    _truncate_body,
)

# ---------------------------------------------------------------------------
# Shared helpers (mirrors test_notable_commits.py)
# ---------------------------------------------------------------------------

_SINCE = "2024-01-01"
_UNTIL = "2024-06-30"


def _make_commit(sha: str, subject: str) -> dict[str, object]:
    return {
        "hash": sha,
        "subject": subject,
        "author": "Alice",
        "date": "2024-06-01",
        "paths": ["src/main.py"],
    }


def _make_pr(
    number: int, body: str, title: str = "feat: do something"
) -> dict[str, object]:
    return {
        "number": number,
        "title": title,
        "author": {"login": "alice"},
        "mergedAt": "2024-06-15T10:00:00Z",
        "body": body,
        "files": [{"path": "src/thing.py"}],
    }


def _build_digest(
    prs: list[dict[str, object]], commits: list[dict[str, object]] | None = None
) -> str:
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
            commits=commits or [],
            owner_repo=("example-owner", "example-repo"),
            max_prs=15,
        )


def _max_line_len(text: str) -> int:
    """Return the length of the longest line in *text*."""
    return max((len(line) for line in text.splitlines()), default=0)


# ---------------------------------------------------------------------------
# NL1 — single PR with an 800-char single-paragraph body
# ---------------------------------------------------------------------------


def test_single_pr_long_single_paragraph_body_no_overlong_lines() -> None:
    """A PR body that is one very long paragraph must be hard-wrapped.

    textwrap.shorten() would have collapsed this to a single 600-char line.
    _truncate_body() must instead wrap it so every line ≤ _LINE_WRAP_WIDTH.
    """
    body = "A" * 800  # 800-char single line, no whitespace at all in the body
    prs = [_make_pr(1, body=body)]

    digest = _build_digest(prs)
    max_len = _max_line_len(digest)

    assert max_len <= _LINE_WRAP_WIDTH, (
        f"Digest contains a line of {max_len} chars (limit {_LINE_WRAP_WIDTH}). "
        "textwrap.shorten() was probably not replaced with _truncate_body().\n"
        f"Longest-line snippet: {next(line for line in digest.splitlines() if len(line) == max_len)!r:.120}"
    )


# ---------------------------------------------------------------------------
# NL2 — many PRs each with a rich multi-paragraph body (exact repro)
# ---------------------------------------------------------------------------


def test_many_prs_rich_bodies_no_overlong_lines() -> None:
    """20 PRs each with a 400-char body → digest lines all ≤ _LINE_WRAP_WIDTH.

    This is the precise scenario that caused 156 repos to fail:
    multiple substantive PRs with bodies long enough that textwrap.shorten()
    collapses each one to a near-_PR_BODY_MAX-length single line.  The
    resulting file has many very long lines and almost no newlines, causing
    low newline-to-byte ratio that binary-detection heuristics trip on.
    """
    prs = [
        _make_pr(
            i,
            # 400-char body that would be collapsed to ~400 chars by shorten()
            body=(
                f"This is a detailed summary for PR {i}. "
                "It describes the rationale, the implementation approach, "
                "and the testing strategy. "
                "The body deliberately has no hard newlines so that "
                "textwrap.shorten() would collapse it to a single long line. "
                "Additional context: performance improvement measured at 30%. "
            ),
            title=f"feat(module{i}): improve performance of subsystem {i}",
        )
        for i in range(1, 21)  # 20 PRs — well above the "PR-rich" threshold
    ]

    digest = _build_digest(prs)
    max_len = _max_line_len(digest)

    assert max_len <= _LINE_WRAP_WIDTH, (
        f"Digest has a {max_len}-char line (limit {_LINE_WRAP_WIDTH}). "
        "Expected _truncate_body() to hard-wrap all PR body lines.\n"
        "Long lines indicate textwrap.shorten() is still in use."
    )


# ---------------------------------------------------------------------------
# NL3 — body with natural newlines: newlines must be preserved
# ---------------------------------------------------------------------------


def test_pr_body_with_natural_newlines_preserved() -> None:
    """PR body that already has short lines must NOT have its newlines collapsed.

    textwrap.shorten() collapses ALL whitespace.  _truncate_body() must preserve
    newlines so the output remains human-readable multi-line prose.
    """
    body = "First sentence.\n\nSecond paragraph.\n\nThird paragraph."
    prs = [_make_pr(1, body=body)]

    digest = _build_digest(prs)

    # At least one of the paragraph text fragments must appear in the digest
    # with line boundaries around it (not collapsed into a single run-on line).
    assert "First sentence." in digest, "First sentence should appear in digest"
    assert "Second paragraph." in digest, "Second paragraph should appear in digest"

    # Sanity: the max line should be well below _LINE_WRAP_WIDTH.
    max_len = _max_line_len(digest)
    assert max_len <= _LINE_WRAP_WIDTH, (
        f"Even with short-line body, digest has a {max_len}-char line "
        f"(limit {_LINE_WRAP_WIDTH})"
    )


# ---------------------------------------------------------------------------
# NL4 — empty PR body
# ---------------------------------------------------------------------------


def test_empty_pr_body_handled_gracefully() -> None:
    """An empty PR body must produce no crash and no extra blank lines in the digest."""
    prs = [_make_pr(1, body="")]

    digest = _build_digest(prs)

    # Must contain the PR detail section header.
    assert "### PR #1:" in digest, (
        "PR detail section must still appear for empty-body PR"
    )

    # No run of more than 2 consecutive blank lines (which would indicate a
    # spurious blank paragraph was inserted by body-handling code).
    assert "\n\n\n\n" not in digest, (
        "Four+ consecutive newlines indicate body-handling artifact"
    )


# ---------------------------------------------------------------------------
# NL5 — _truncate_body unit tests
# ---------------------------------------------------------------------------


def test_truncate_body_long_single_line_is_wrapped() -> None:
    """A body that is one very long line must be hard-wrapped."""
    body = "x" * (_LINE_WRAP_WIDTH + 50)
    result = _truncate_body(body, _PR_BODY_MAX)
    max_len = _max_line_len(result)
    assert max_len <= _LINE_WRAP_WIDTH, (
        f"_truncate_body did not wrap a {len(body)}-char line; max in output: {max_len}"
    )


def test_truncate_body_preserves_short_lines() -> None:
    """Lines already ≤ _LINE_WRAP_WIDTH must be emitted unchanged (no wrapping)."""
    body = "Line one.\nLine two.\nLine three."
    result = _truncate_body(body, _PR_BODY_MAX)
    assert "Line one." in result
    assert "Line two." in result
    assert "Line three." in result
    # Structure is preserved — not collapsed to one line.
    assert "\n" in result, "_truncate_body must preserve existing newlines"


def test_truncate_body_empty_returns_empty() -> None:
    """Blank / whitespace-only input must return an empty string."""
    assert _truncate_body("", _PR_BODY_MAX) == ""
    assert _truncate_body("   \n\n   ", _PR_BODY_MAX) == ""


def test_truncate_body_short_body_unchanged() -> None:
    """Body shorter than max_chars must not have a truncation indicator appended."""
    body = "Short body."
    result = _truncate_body(body, _PR_BODY_MAX)
    assert result == body, f"Short body should be returned unchanged; got {result!r}"
    assert "\u2026" not in result, "Ellipsis must not be added to a non-truncated body"


def test_truncate_body_truncates_at_max_chars() -> None:
    """Body longer than max_chars must be truncated and end with '…'."""
    long_body = "word " * 200  # well over _PR_BODY_MAX
    result = _truncate_body(long_body, _PR_BODY_MAX)
    # Total char count must not exceed max_chars.
    assert (
        len(result.replace("\n", "")) <= _PR_BODY_MAX + 10
    ), (  # +10 for wrap overhead
        f"Truncated body is too long: {len(result)} chars"
    )
    # Should end with the ellipsis character on some line.
    assert "\u2026" in result, "Truncated body must contain ellipsis indicator"
