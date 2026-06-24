"""REPO-ATTRIBUTION: every source doc emitted by materialize must carry **Repository:**.

FORMAT CHANGE (org-scoped qualifier fix):
  The **Repository:** line now shows ``owner/repo`` (human-readable) when the remote
  URL is available.  Previously it showed the raw ``repo_qualifier`` string (typically
  the basename) in multi-repo mode and just ``owner_repo[1]`` (the repo name) in
  single-repo mode.  Tests R1-R4 are updated to assert the new ``owner/repo`` form.

  Filenames use the filesystem-safe ``owner__repo`` form (see test_org_scoped_qualifier.py).

The REPO-ATTRIBUTION instrument in schema.md requires the synthesizer to be able to
derive the ``repos:`` frontmatter field from ``**Repository:**`` lines present in each
cited source document.  These tests verify that materialize.py guarantees that line is
present in every emitted document when repo identity is knowable.

Test matrix
-----------
R1  Multi-repo change digest (repo_qualifier set) → ``**Repository:** `<qualifier>``` present.
R2  Multi-repo module snapshot (repo_qualifier set) → ``**Repository:** `<qualifier>``` present.
R3  Single-repo change digest (owner_repo set, no qualifier) → ``**Repository:** `<repo>``` present.
R4  Single-repo module snapshot (owner_repo set, no qualifier) → ``**Repository:** `<repo>``` present.
R5  No-remote digest (owner_repo=None, no qualifier) → no crash; no fabricated Repository line.
"""

from __future__ import annotations

from unittest.mock import patch

from repo_weaver.materialize import _build_change_digest, _build_module_doc

_REPO_MARKER = "**Repository:**"

_FAKE_PR: dict[str, object] = {
    "number": 1,
    "title": "feat: add something",
    "author": {"login": "alice"},
    "mergedAt": "2024-06-01T10:00:00Z",
    "body": "A description of the change.",
    "files": [{"path": "src/main.py"}],
}


def _make_digest(
    owner_repo: tuple[str, str] | None = ("org", "my-repo"),
    repo_qualifier: str | None = None,
) -> str:
    """Build a change digest with patched gh / authors; no network required."""
    fake_prs: list[dict[str, object]] = [_FAKE_PR] if owner_repo else []
    with (
        patch(
            "repo_weaver.materialize.gitio.gh_merged_prs",
            return_value=(fake_prs, None),
        ),
        patch(
            "repo_weaver.materialize.gitio.get_shortlog_authors",
            return_value=[],
        ),
    ):
        return _build_change_digest(
            repo="/fake/repo",
            since="2024-01-01",
            until="2024-06-30",
            until_rev=None,
            commits=[],
            owner_repo=owner_repo,
            max_prs=15,
            repo_qualifier=repo_qualifier,
        )


def _make_module_doc(
    owner_repo: tuple[str, str] | None = ("org", "my-repo"),
    repo_qualifier: str | None = None,
) -> str | None:
    """Build a module snapshot doc with patched gitio; no network required."""
    with (
        patch(
            "repo_weaver.materialize.gitio.get_shortlog_authors",
            return_value=[],
        ),
        patch(
            "repo_weaver.materialize.gitio.get_tree_at_rev",
            return_value=[],
        ),
        patch(
            "repo_weaver.materialize.gitio.get_file_at_rev",
            return_value=None,
        ),
    ):
        return _build_module_doc(
            repo="/fake/repo",
            since="2024-01-01",
            until="2024-06-30",
            until_rev=None,
            module_path="src",
            commit_count=3,
            all_commits=[],
            owner_repo=owner_repo,
            repo_qualifier=repo_qualifier,
        )


# ---------------------------------------------------------------------------
# R1 — Multi-repo change digest carries **Repository:** with qualifier
# ---------------------------------------------------------------------------


def test_multi_repo_digest_has_repository_marker() -> None:
    """Multi-repo change digest must contain **Repository:** `org/my-repo` (org-scoped body form).

    After the org-scoped qualifier fix the body shows the human-readable ``owner/repo``
    form (``org/my-repo``) rather than the raw qualifier string (``my-qualifier``).
    The filename uses the filesystem-safe ``owner__repo`` form — tested separately in
    test_org_scoped_qualifier.py.
    """
    digest = _make_digest(owner_repo=("org", "my-repo"), repo_qualifier="my-qualifier")
    assert _REPO_MARKER in digest, (
        f"Multi-repo change digest must contain '{_REPO_MARKER}'; "
        f"digest snippet:\n{digest[:600]!r}"
    )
    assert "`org/my-repo`" in digest, (
        f"Expected org-scoped body 'org/my-repo' in the digest (not raw qualifier); "
        f"digest snippet:\n{digest[:600]!r}"
    )


# ---------------------------------------------------------------------------
# R2 — Multi-repo module snapshot carries **Repository:** with qualifier
# ---------------------------------------------------------------------------


def test_multi_repo_module_doc_has_repository_marker() -> None:
    """Multi-repo module snapshot must contain **Repository:** `org/my-repo` (org-scoped body form).

    After the org-scoped qualifier fix the body shows ``owner/repo`` rather than the
    raw ``repo_qualifier`` string.
    """
    doc = _make_module_doc(owner_repo=("org", "my-repo"), repo_qualifier="my-qualifier")
    assert doc is not None, "Module doc should not be None for a valid module path"
    assert _REPO_MARKER in doc, (
        f"Multi-repo module doc must contain '{_REPO_MARKER}'; "
        f"doc snippet:\n{doc[:600]!r}"
    )
    assert "`org/my-repo`" in doc, (
        f"Expected org-scoped body 'org/my-repo' in module doc (not raw qualifier); "
        f"doc snippet:\n{doc[:600]!r}"
    )


# ---------------------------------------------------------------------------
# R3 — Single-repo change digest (owner_repo known) carries **Repository:**
# ---------------------------------------------------------------------------


def test_single_repo_digest_has_repository_marker() -> None:
    """Single-repo digest with owner_repo set must carry **Repository:** `org/single-repo`.

    After the org-scoped qualifier fix the body shows the full ``owner/repo`` form
    (``org/single-repo``) rather than just the repo basename (``single-repo``).
    """
    digest = _make_digest(owner_repo=("org", "single-repo"), repo_qualifier=None)
    assert _REPO_MARKER in digest, (
        f"Single-repo change digest must contain '{_REPO_MARKER}' when owner_repo is "
        f"set (so synthesizer can derive repos: frontmatter); "
        f"digest snippet:\n{digest[:600]!r}"
    )
    assert "`org/single-repo`" in digest, (
        f"Expected org-scoped body 'org/single-repo' from owner_repo in the digest "
        f"(not bare repo name); "
        f"digest snippet:\n{digest[:600]!r}"
    )


# ---------------------------------------------------------------------------
# R4 — Single-repo module snapshot (owner_repo known) carries **Repository:**
# ---------------------------------------------------------------------------


def test_single_repo_module_doc_has_repository_marker() -> None:
    """Single-repo module doc with owner_repo set must carry **Repository:** `org/single-repo`.

    After the org-scoped qualifier fix the body shows the full ``owner/repo`` form.
    """
    doc = _make_module_doc(owner_repo=("org", "single-repo"), repo_qualifier=None)
    assert doc is not None, "Module doc should not be None for a valid module path"
    assert _REPO_MARKER in doc, (
        f"Single-repo module doc must contain '{_REPO_MARKER}' when owner_repo is "
        f"set (so synthesizer can derive repos: frontmatter); "
        f"doc snippet:\n{doc[:600]!r}"
    )
    assert "`org/single-repo`" in doc, (
        f"Expected org-scoped body 'org/single-repo' from owner_repo in module doc; "
        f"doc snippet:\n{doc[:600]!r}"
    )


# ---------------------------------------------------------------------------
# R5 — No-remote digest (owner_repo=None) → no crash; no fabricated Repository line
# ---------------------------------------------------------------------------


def test_no_remote_digest_no_fabricated_repository_marker() -> None:
    """No-remote digest must not fabricate a ``**Repository:**`` line.

    When neither ``repo_qualifier`` nor ``owner_repo`` is available there is no
    repo name to emit.  The document must be produced without a Repository line
    (no fabrication) and without raising an exception.
    """
    digest = _make_digest(owner_repo=None, repo_qualifier=None)
    assert isinstance(digest, str) and len(digest) > 0, (
        "Digest must be a non-empty string even without owner_repo"
    )
    assert _REPO_MARKER not in digest, (
        f"No-remote digest must NOT contain a fabricated '{_REPO_MARKER}' line "
        f"(never fabricate provenance); digest snippet:\n{digest[:600]!r}"
    )
