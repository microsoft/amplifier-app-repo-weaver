"""Tests for cross-repo reference detection in _build_change_digest.

All tests are deterministic and require no network access.  External calls
(gitio.gh_merged_prs, gitio.get_shortlog_authors) are patched with synthetic
PR dicts that follow the same structure used in test_resume_and_classify.py.

Test matrix
-----------
X1  Digest built from PRs whose bodies contain a GitHub bundle URL, an
    amplifier-core dep pin, and a cross-repo PR ref → Cross-repo references
    section present, names all three repos with correct originating PR numbers.
X2  Digest built from PRs with NO cross-repo references → section absent
    (no fabricated empty section).
X3  A PR that mentions the same repo it belongs to → self-reference excluded,
    section absent when that is the only reference.
"""

from __future__ import annotations

from unittest.mock import patch

from repo_weaver.materialize import _build_change_digest, _extract_cross_repo_refs


# ---------------------------------------------------------------------------
# Shared helper: call _build_change_digest with patched gh / authors
# ---------------------------------------------------------------------------


def _make_digest(
    fake_prs: list[dict[str, object]],
    owner_repo: tuple[str, str] = ("example-owner", "example-repo"),
) -> str:
    """Call _build_change_digest with synthetic PR dicts; no network required."""
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
            repo_qualifier=None,
            classify=True,
        )


# ---------------------------------------------------------------------------
# X1 — Three distinct cross-repo signals detected, section present
# ---------------------------------------------------------------------------

# Synthetic PRs whose bodies contain all three signal types:
#   (i)  git+https://github.com/microsoft/amplifier-foundation@main#...   → amplifier-foundation
#   (ii) amplifier-core>=1.6.0                                             → amplifier-core
#   (iii) microsoft/amplifier-docs#12                                      → amplifier-docs

_BUNDLE_REF_PR: dict[str, object] = {
    "number": 208,
    "title": "feat(bundles): register foundation anchors bundle",
    "author": {"login": "alice"},
    "mergedAt": "2024-03-10T12:00:00Z",
    "body": (
        "Registers the bundle ref "
        "git+https://github.com/microsoft/amplifier-foundation@main"
        "#subdirectory=bundles/anchors/bundle.md "
        "so the CLI can resolve anchors at runtime."
    ),
    "files": [{"path": "src/bundles.py"}],
}

_DEP_PIN_PR: dict[str, object] = {
    "number": 43,
    "title": "chore(deps): pin amplifier-core for 1.6 compat",
    "author": {"login": "bob"},
    "mergedAt": "2024-04-05T09:00:00Z",
    "body": (
        "Pins amplifier-core>=1.6.0 to pick up the new evaluation hooks "
        "introduced in that release."
    ),
    "files": [{"path": "pyproject.toml"}],
}

_CROSS_PR_REF_PR: dict[str, object] = {
    "number": 99,
    "title": "fix: align with docs team change",
    "author": {"login": "carol"},
    "mergedAt": "2024-05-20T15:00:00Z",
    "body": "Companion change for microsoft/amplifier-docs#12 which updated the schema.",
    "files": [{"path": "docs/schema.md"}],
}


def test_cross_repo_refs_detected_in_digest() -> None:
    """Digest with bundle URL + dep pin + cross-repo PR ref → section present.

    Verifies:
    - A ``### Cross-repo references`` section appears in the digest.
    - ``amplifier-foundation`` named with originating PR #208.
    - ``amplifier-core`` named with originating PR #43.
    - ``amplifier-docs`` named with originating PR #99.
    """
    digest = _make_digest([_BUNDLE_REF_PR, _DEP_PIN_PR, _CROSS_PR_REF_PR])

    section_start = digest.find("### Cross-repo references")
    assert section_start != -1, (
        "Expected a '### Cross-repo references' section in the digest; "
        f"digest snippet:\n{digest[:800]!r}"
    )

    section = digest[section_start:]

    assert "amplifier-foundation" in section, (
        "Expected 'amplifier-foundation' in the cross-repo section; "
        f"section:\n{section[:500]!r}"
    )
    assert "PR #208" in section, (
        "Expected PR #208 (source of amplifier-foundation ref) in the section; "
        f"section:\n{section[:500]!r}"
    )

    assert "amplifier-core" in section, (
        "Expected 'amplifier-core' (dep pin) in the cross-repo section; "
        f"section:\n{section[:500]!r}"
    )
    assert "PR #43" in section, (
        "Expected PR #43 (source of amplifier-core pin) in the section; "
        f"section:\n{section[:500]!r}"
    )

    assert "amplifier-docs" in section, (
        "Expected 'amplifier-docs' (cross-repo PR ref) in the cross-repo section; "
        f"section:\n{section[:500]!r}"
    )
    assert "PR #99" in section, (
        "Expected PR #99 (source of amplifier-docs ref) in the section; "
        f"section:\n{section[:500]!r}"
    )


# ---------------------------------------------------------------------------
# X2 — No cross-repo references → section entirely absent
# ---------------------------------------------------------------------------

_CLEAN_PR_1: dict[str, object] = {
    "number": 1,
    "title": "fix(auth): handle token expiry gracefully",
    "author": {"login": "alice"},
    "mergedAt": "2024-02-01T10:00:00Z",
    "body": "Fixes a bug where expired tokens caused a 500 instead of a 401.",
    "files": [{"path": "src/auth.py"}],
}

_CLEAN_PR_2: dict[str, object] = {
    "number": 2,
    "title": "feat(dashboard): add activity feed",
    "author": {"login": "bob"},
    "mergedAt": "2024-02-15T14:00:00Z",
    "body": "Adds a real-time activity feed to the dashboard sidebar.",
    "files": [{"path": "src/dashboard.py"}, {"path": "tests/test_dashboard.py"}],
}


def test_no_cross_repo_refs_section_absent() -> None:
    """Digest with PRs containing no cross-repo signals → no section emitted.

    Verifies:
    - The ``### Cross-repo references`` section does NOT appear (no fabrication,
      no empty section placeholder).
    """
    digest = _make_digest([_CLEAN_PR_1, _CLEAN_PR_2])

    assert "### Cross-repo references" not in digest, (
        "No cross-repo references in PR bodies — section must NOT be emitted. "
        f"Digest snippet:\n{digest[:800]!r}"
    )


# ---------------------------------------------------------------------------
# X3 — Self-reference excluded: PR mentions the repo it belongs to
# ---------------------------------------------------------------------------

_SELF_REF_PR: dict[str, object] = {
    "number": 5,
    "title": "fix: reference to existing issue",
    "author": {"login": "dave"},
    "mergedAt": "2024-06-01T11:00:00Z",
    "body": "Resolves a regression, see microsoft/amplifier-app-cli#3 for context.",
    "files": [{"path": "src/cli.py"}],
}


def test_self_reference_not_listed() -> None:
    """PR mentioning the same repo it belongs to → self-ref excluded.

    Setup: single PR whose body references ``microsoft/amplifier-app-cli#3``;
    the digest is built for the ``amplifier-app-cli`` repo itself.

    Verifies:
    - ``amplifier-app-cli`` does NOT appear in any cross-repo section.
    - The ``### Cross-repo references`` section is absent entirely (no other
      refs present, so the section should be omitted).
    """
    digest = _make_digest(
        [_SELF_REF_PR],
        owner_repo=("microsoft", "amplifier-app-cli"),
    )

    assert "### Cross-repo references" not in digest, (
        "Self-reference to the repo being materialized must be excluded; "
        "the cross-repo section must not be emitted when the only reference "
        "is to the repo itself. "
        f"Digest snippet:\n{digest[:800]!r}"
    )


# ---------------------------------------------------------------------------
# Unit tests for _extract_cross_repo_refs directly (faster, more targeted)
# ---------------------------------------------------------------------------


def test_extract_refs_all_three_patterns() -> None:
    """_extract_cross_repo_refs detects all three signal types in one call."""
    prs = [_BUNDLE_REF_PR, _DEP_PIN_PR, _CROSS_PR_REF_PR]
    refs = _extract_cross_repo_refs(prs, self_repo_name="example-repo")

    repo_names = {r[0] for r in refs}
    assert "amplifier-foundation" in repo_names, (
        f"Expected amplifier-foundation in refs; got {repo_names}"
    )
    assert "amplifier-core" in repo_names, (
        f"Expected amplifier-core in refs; got {repo_names}"
    )
    assert "amplifier-docs" in repo_names, (
        f"Expected amplifier-docs in refs; got {repo_names}"
    )

    # Each ref must carry the correct originating PR number.
    pr_by_repo = {r[0]: r[2] for r in refs}
    assert pr_by_repo.get("amplifier-foundation") == 208, (
        f"Expected PR 208 for amplifier-foundation; got {pr_by_repo}"
    )
    assert pr_by_repo.get("amplifier-core") == 43, (
        f"Expected PR 43 for amplifier-core; got {pr_by_repo}"
    )
    assert pr_by_repo.get("amplifier-docs") == 99, (
        f"Expected PR 99 for amplifier-docs; got {pr_by_repo}"
    )


def test_extract_refs_empty_prs() -> None:
    """Empty PR list → empty refs list (no fabrication)."""
    refs = _extract_cross_repo_refs([], self_repo_name=None)
    assert refs == [], f"Expected empty refs for empty PR list; got {refs}"


def test_extract_refs_self_excluded() -> None:
    """Self-reference to 'amplifier-app-cli' is filtered out."""
    refs = _extract_cross_repo_refs([_SELF_REF_PR], self_repo_name="amplifier-app-cli")
    repo_names = {r[0] for r in refs}
    assert "amplifier-app-cli" not in repo_names, (
        f"Self-reference must not appear in refs; got {repo_names}"
    )
    assert len(refs) == 0, (
        f"Expected zero refs when only self-reference present; got {refs}"
    )


def test_extract_refs_deduplication_per_pr() -> None:
    """Same repo referenced twice in one PR body → only one entry emitted."""
    # A PR that references amplifier-foundation twice: once via URL, once via dep pin.
    dual_pr: dict[str, object] = {
        "number": 7,
        "title": "chore: update bundle and pin",
        "author": {"login": "eve"},
        "mergedAt": "2024-06-10T10:00:00Z",
        "body": (
            "Updates git+https://github.com/microsoft/amplifier-foundation@main "
            "and also pins amplifier-foundation>=2.0.0 directly."
        ),
        "files": [],
    }
    refs = _extract_cross_repo_refs([dual_pr], self_repo_name="other-repo")
    foundation_refs = [r for r in refs if r[0] == "amplifier-foundation"]
    assert len(foundation_refs) == 1, (
        "Same repo referenced twice in one PR must appear only once; "
        f"got {foundation_refs}"
    )
    assert foundation_refs[0][2] == 7, "PR number must be 7"
