"""COLLISION-FIX: org-scoped qualifier prevents silent name-collision data-loss.

Regression tests for the bug where two repos with the same basename but different
orgs produced IDENTICAL digest filenames and IDENTICAL ``**Repository:**`` lines —
causing the second repo to silently overwrite the first's source doc in the corpus.

Test matrix
-----------
OQ1  REGRESSION: same basename, different owners → DISTINCT digest filenames AND
     DISTINCT ``**Repository:**`` lines.  This is the primary regression guard.
     Asserts: ``bkrabach__amplifier-bundle-skills-…`` ≠ ``microsoft__amplifier-bundle-skills-…``
     and ``bkrabach/amplifier-bundle-skills`` vs ``microsoft/amplifier-bundle-skills``.

OQ2  Repo WITH a remote → filename uses ``owner__repo`` form (double-underscore);
     ``**Repository:**`` body line shows human-readable ``owner/repo``.

OQ3  Repo WITHOUT a remote (owner_repo None) → falls back to the caller-supplied
     qualifier (basename); no crash; no fabricated owner appears in filename or body.

OQ4  Single-repo mode (no repo_qualifier) with owner_repo known → ``**Repository:**``
     shows ``owner/repo`` (not just the bare repo name as before the fix).
"""

from __future__ import annotations

from unittest.mock import patch

from repo_weaver.materialize import _build_change_digest, materialize


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_SINCE = "2024-01-01"
_UNTIL = "2024-06-30"


def _make_materialize(
    owner_url: str | None,
    repo_qualifier: str | None,
) -> list[tuple[str, str]]:
    """Call materialize() with all git/gh I/O mocked out; returns (filename, content) pairs.

    *owner_url* is returned by the mocked ``gitio.get_origin_url``.  Pass ``None``
    to simulate a repo with no remote configured.

    *repo_qualifier* controls single-repo (``None``) vs multi-repo mode (non-None).
    """
    with (
        patch(
            "repo_weaver.materialize.gitio.get_origin_url",
            return_value=owner_url,
        ),
        patch(
            "repo_weaver.materialize.gitio.get_window_rev",
            return_value=None,
        ),
        patch(
            "repo_weaver.materialize.gitio.get_commits_name_only",
            return_value=[],
        ),
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
            return_value=([], None),
        ),
        patch(
            "repo_weaver.materialize.gitio.gh_pr_discussion",
            return_value=({"comments": [], "reviews": []}, None),
        ),
    ):
        return materialize(
            repo="/fake/repo",
            since=_SINCE,
            until=_UNTIL,
            repo_qualifier=repo_qualifier,
        )


def _make_digest(
    owner_repo: tuple[str, str] | None,
    repo_qualifier: str | None = None,
) -> str:
    """Call _build_change_digest with patched gh / authors; no network required."""
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
            return_value=([], None),
        ),
        patch(
            "repo_weaver.materialize.gitio.gh_pr_discussion",
            return_value=({"comments": [], "reviews": []}, None),
        ),
    ):
        return _build_change_digest(
            repo="/fake/repo",
            since=_SINCE,
            until=_UNTIL,
            until_rev=None,
            commits=[],
            owner_repo=owner_repo,
            max_prs=15,
            repo_qualifier=repo_qualifier,
        )


# ---------------------------------------------------------------------------
# OQ1 — REGRESSION: same basename, different owners → distinct filenames + body
# ---------------------------------------------------------------------------


def test_same_basename_different_owners_distinct_digest_filenames() -> None:
    """Two repos with the same basename but different orgs must produce DISTINCT filenames.

    Before the fix both produced the same ``amplifier-bundle-skills-<until>-changes.md``
    digest filename, causing the second to silently overwrite the first in the corpus.
    After the fix they produce ``bkrabach__amplifier-bundle-skills-…`` vs
    ``microsoft__amplifier-bundle-skills-…``.
    """
    bkrabach_docs = _make_materialize(
        owner_url="https://github.com/bkrabach/amplifier-bundle-skills.git",
        repo_qualifier="amplifier-bundle-skills",
    )
    microsoft_docs = _make_materialize(
        owner_url="https://github.com/microsoft/amplifier-bundle-skills.git",
        repo_qualifier="amplifier-bundle-skills",
    )

    bkrabach_filename = bkrabach_docs[0][0]
    microsoft_filename = microsoft_docs[0][0]

    # Filenames MUST be distinct — this is the regression guard.
    assert bkrabach_filename != microsoft_filename, (
        f"REGRESSION: two repos with the same basename but different owners produced "
        f"IDENTICAL digest filenames — silent data-loss when both are woven into one "
        f"corpus.\n"
        f"  bkrabach: {bkrabach_filename!r}\n"
        f"  microsoft: {microsoft_filename!r}"
    )

    # Org-scoped filenames must contain the owner prefix.
    assert "bkrabach__" in bkrabach_filename, (
        f"Expected 'bkrabach__' prefix in filename; got {bkrabach_filename!r}"
    )
    assert "microsoft__" in microsoft_filename, (
        f"Expected 'microsoft__' prefix in filename; got {microsoft_filename!r}"
    )


def test_same_basename_different_owners_distinct_repository_lines() -> None:
    """Two repos with the same basename but different orgs must show DISTINCT **Repository:** lines.

    Before the fix both showed ``**Repository:** `amplifier-bundle-skills``` — the
    synthesiser could not distinguish which org each page came from.
    After the fix they show ``bkrabach/amplifier-bundle-skills`` vs
    ``microsoft/amplifier-bundle-skills``.
    """
    bkrabach_digest = _make_digest(
        owner_repo=("bkrabach", "amplifier-bundle-skills"),
        repo_qualifier="amplifier-bundle-skills",
    )
    microsoft_digest = _make_digest(
        owner_repo=("microsoft", "amplifier-bundle-skills"),
        repo_qualifier="amplifier-bundle-skills",
    )

    repo_marker = "**Repository:**"

    assert repo_marker in bkrabach_digest, (
        f"Expected '{repo_marker}' in bkrabach digest; snippet:\n{bkrabach_digest[:400]!r}"
    )
    assert repo_marker in microsoft_digest, (
        f"Expected '{repo_marker}' in microsoft digest; snippet:\n{microsoft_digest[:400]!r}"
    )

    assert "`bkrabach/amplifier-bundle-skills`" in bkrabach_digest, (
        f"Expected 'bkrabach/amplifier-bundle-skills' in bkrabach digest; "
        f"snippet:\n{bkrabach_digest[:600]!r}"
    )
    assert "`microsoft/amplifier-bundle-skills`" in microsoft_digest, (
        f"Expected 'microsoft/amplifier-bundle-skills' in microsoft digest; "
        f"snippet:\n{microsoft_digest[:600]!r}"
    )

    # The two **Repository:** lines must be different — the core disambiguation test.
    assert "`bkrabach/amplifier-bundle-skills`" not in microsoft_digest, (
        "bkrabach qualifier must NOT appear in microsoft digest — still colliding!"
    )
    assert "`microsoft/amplifier-bundle-skills`" not in bkrabach_digest, (
        "microsoft qualifier must NOT appear in bkrabach digest — still colliding!"
    )


# ---------------------------------------------------------------------------
# OQ2 — Repo WITH a remote → org-scoped filename + owner/repo body
# ---------------------------------------------------------------------------


def test_repo_with_remote_filename_uses_owner_double_underscore_repo() -> None:
    """Repo with GitHub remote → digest filename uses ``owner__repo`` (filesystem-safe form).

    ``bkrabach/amplifier-bundle-skills`` → ``bkrabach__amplifier-bundle-skills-2024-06-30-changes.md``
    """
    docs = _make_materialize(
        owner_url="https://github.com/bkrabach/amplifier-bundle-skills.git",
        repo_qualifier="amplifier-bundle-skills",
    )
    filename = docs[0][0]

    expected = f"bkrabach__amplifier-bundle-skills-{_UNTIL}-changes.md"
    assert filename == expected, (
        f"Expected digest filename {expected!r} but got {filename!r}.\n"
        f"The org-scoped filename form must use '__' (double-underscore) separator."
    )


def test_repo_with_remote_body_shows_owner_slash_repo() -> None:
    """Repo with GitHub remote → **Repository:** body line shows ``owner/repo`` (human-readable)."""
    digest = _make_digest(
        owner_repo=("bkrabach", "amplifier-bundle-skills"),
        repo_qualifier="amplifier-bundle-skills",
    )

    assert "**Repository:**" in digest, (
        f"Expected '**Repository:**' in digest; snippet:\n{digest[:400]!r}"
    )
    assert "`bkrabach/amplifier-bundle-skills`" in digest, (
        f"Expected body to show 'bkrabach/amplifier-bundle-skills' (owner/repo form); "
        f"snippet:\n{digest[:600]!r}"
    )
    # Must NOT contain the raw qualifier (basename only) in the Repository line.
    # The qualifier string appears nowhere on its own in the body.
    # (It may appear inside the longer 'bkrabach/amplifier-bundle-skills' token, but
    #  the bare `` `amplifier-bundle-skills` `` must not be the Repository value.)
    assert "**Repository:** `amplifier-bundle-skills`" not in digest, (
        f"Body must show org-scoped 'bkrabach/amplifier-bundle-skills', not bare basename; "
        f"snippet:\n{digest[:600]!r}"
    )


# ---------------------------------------------------------------------------
# OQ3 — Repo WITHOUT a remote → basename fallback; no fabricated owner
# ---------------------------------------------------------------------------


def test_no_remote_falls_back_to_basename_filename() -> None:
    """Repo with no remote in multi-repo mode → filename uses the supplied basename qualifier.

    No owner is fabricated.  The filename is ``<basename>-<until>-changes.md``.
    """
    docs = _make_materialize(
        owner_url=None,  # no remote configured
        repo_qualifier="my-local-repo",
    )
    filename = docs[0][0]

    expected = f"my-local-repo-{_UNTIL}-changes.md"
    assert filename == expected, (
        f"Expected basename-fallback filename {expected!r} but got {filename!r}.\n"
        f"When there is no remote, the supplied qualifier must be used as-is."
    )

    # Must not contain a fabricated double-underscore owner.
    assert "__" not in filename, (
        "Filename must not contain '__' when there is no remote (no owner to fabricate); "
        f"got {filename!r}"
    )


def test_no_remote_falls_back_to_basename_body() -> None:
    """Repo with no remote → **Repository:** body falls back to the qualifier (no fabrication)."""
    digest = _make_digest(
        owner_repo=None,  # no remote
        repo_qualifier="my-local-repo",
    )

    # The qualifier itself appears as the fallback.
    assert "`my-local-repo`" in digest, (
        f"Expected fallback qualifier 'my-local-repo' in body; "
        f"snippet:\n{digest[:600]!r}"
    )

    # Must not contain any fabricated 'owner/' prefix.
    # If a '/' appears inside the Repository line it would mean an owner was fabricated.
    repo_line = [line for line in digest.splitlines() if "**Repository:**" in line]
    assert repo_line, f"Expected a **Repository:** line; got:\n{digest[:400]!r}"
    assert "/" not in repo_line[0], (
        "No '/' must appear in the Repository line when there is no remote — "
        f"never fabricate an owner; line: {repo_line[0]!r}"
    )


def test_no_remote_no_crash() -> None:
    """Repo with no remote in multi-repo mode → no exception is raised."""
    docs = _make_materialize(
        owner_url=None,
        repo_qualifier="my-local-repo",
    )
    assert isinstance(docs, list) and len(docs) > 0, (
        "materialize() must return at least one document even without a remote"
    )


# ---------------------------------------------------------------------------
# OQ4 — Single-repo mode (no qualifier) + owner_repo known → owner/repo body
# ---------------------------------------------------------------------------


def test_single_repo_mode_body_shows_owner_slash_repo() -> None:
    """Single-repo mode (no qualifier) with known remote → **Repository:** shows owner/repo.

    This validates the body format change for R3/R4 in test_repo_attribution.py.
    Previously showed only the repo basename (``my-repo``); now shows ``org/my-repo``.
    """
    digest = _make_digest(
        owner_repo=("org", "my-repo"),
        repo_qualifier=None,  # single-repo mode
    )

    assert "**Repository:**" in digest, (
        f"Expected '**Repository:**' in single-repo digest; snippet:\n{digest[:400]!r}"
    )
    assert "`org/my-repo`" in digest, (
        f"Expected 'org/my-repo' (owner/repo form) in single-repo body; "
        f"snippet:\n{digest[:600]!r}"
    )
    # Old bare-basename form must be gone.
    assert "**Repository:** `my-repo`" not in digest, (
        f"Single-repo body must show 'org/my-repo', not bare 'my-repo'; "
        f"snippet:\n{digest[:600]!r}"
    )
