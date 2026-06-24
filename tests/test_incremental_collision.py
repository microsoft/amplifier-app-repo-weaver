"""SILENT-OVERWRITE regression tests for incremental (one-repo-at-a-time) weaving.

Regression suite for the bug where weaving repo A then repo B into the same
corpus with the same --until date produced IDENTICAL bare ``<until>-changes.md``
inbox filenames, causing repo B's digest to silently overwrite repo A's.

Test matrix
-----------
INC1  INCREMENTAL COLLISION REGRESSION: two repos (owner1/foo, owner2/bar),
      same until date, woven one-at-a-time → two DISTINCT inbox files
      (``owner1__foo-<until>-changes.md`` and ``owner2__bar-<until>-changes.md``);
      neither overwrites the other.

INC2  SAME-BASENAME INCREMENTAL: two repos with basename ``skills`` but owners
      ``bkrabach`` and ``microsoft``, woven one-at-a-time, same until →
      ``bkrabach__skills-...`` and ``microsoft__skills-...`` are distinct.

INC3a NO-REMOTE FALLBACK INCREMENTAL: a local-only repo (no remote) →
      filename uses the directory basename, no crash, no fabricated owner.

INC3b TWO NO-REMOTE DIFFERENT BASENAMES: two local-only repos with distinct
      basenames → two distinct inbox files.

INC3c TWO NO-REMOTE SAME BASENAME LIMITATION: two local-only repos with the
      SAME basename and no remote still collide — unavoidable without a remote;
      the test accepts this limitation and asserts no crash.

INC4a DEDUP / IDEMPOTENCY — single-repo archive-skip: weave() skips when the
      QUALIFIED digest is already in _archive/.

INC4b DEDUP / IDEMPOTENCY — weave_multi() archive-skip: the existing archive-
      skip in weave_multi() continues to match the new qualified filename form.

INC4c DEDUP / IDEMPOTENCY — no-remote archive-skip: weave() uses the basename
      form for the archive-skip when there is no remote.
"""

from __future__ import annotations

import os
import subprocess
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from unittest.mock import patch

from repo_weaver.weave import weave, weave_multi


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SINCE = "2026-01-01"
_UNTIL = "2026-06-23"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_git_repo(path: Path) -> None:
    """Create a minimal local git repo with one empty commit."""
    path.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "t@test.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "t@test.com",
    }
    subprocess.run(["git", "init", str(path)], capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "t@test.com"],
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test"],
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "--allow-empty", "-m", "initial"],
        capture_output=True,
        env=env,
    )


def _setup_corpus(tmp_path: Path) -> Path:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "_inbox").mkdir()
    (corpus / "_archive").mkdir()
    return corpus


@contextmanager
def _patched_weave(
    *,
    origin_url: str | None,
    is_repo: bool = True,
) -> Iterator[None]:
    """Patch all gitio subprocess calls used by weave() and materialize().

    Uses no_fetch=True on the weave() call so the staleness-check helper is
    skipped entirely (no fetch_origin / commits_behind_origin patches needed).

    ``origin_url`` controls what both weave.gitio and materialize.gitio return
    for get_origin_url — same value so the archive-skip and file_qualifier
    computations are consistent.

    parse_owner_repo is intentionally NOT patched — it is a pure regex function
    (no subprocess calls) and must run against the real URL strings provided by
    the mocked get_origin_url.  Patching it would require a recursive passthrough
    that self-deadlocks.
    """
    with (
        patch("repo_weaver.weave.gitio.is_git_repo", return_value=is_repo),
        patch("repo_weaver.weave.gitio.get_origin_url", return_value=origin_url),
        patch("repo_weaver.materialize.gitio.get_origin_url", return_value=origin_url),
        patch("repo_weaver.materialize.gitio.get_window_rev", return_value=None),
        patch("repo_weaver.materialize.gitio.get_commits_name_only", return_value=[]),
        patch("repo_weaver.materialize.gitio.gh_merged_prs", return_value=([], None)),
        patch("repo_weaver.materialize.gitio.get_shortlog_authors", return_value=[]),
    ):
        yield


def _weave_dry(corpus: Path, repo_path: Path, origin_url: str | None) -> int:
    """Run weave() in dry-run + no-fetch mode with all external I/O mocked."""
    with _patched_weave(origin_url=origin_url):
        return weave(
            corpus=str(corpus),
            repo=str(repo_path),
            since=_SINCE,
            until=_UNTIL,
            dry_run=True,
            no_fetch=True,
        )


def _inbox_names(corpus: Path) -> list[str]:
    return sorted(f.name for f in (corpus / "_inbox").iterdir())


# ---------------------------------------------------------------------------
# INC1 — INCREMENTAL COLLISION REGRESSION
# ---------------------------------------------------------------------------


def test_incremental_collision_two_different_owners(tmp_path: Path) -> None:
    """Two repos with different owners woven one-at-a-time → two DISTINCT inbox files.

    This is the primary regression guard for the SILENT-OVERWRITE bug.
    Before the fix both calls produced ``2026-06-23-changes.md``; the second
    silently overwrote the first.  After the fix:
      • repo A → ``owner1__foo-2026-06-23-changes.md``
      • repo B → ``owner2__bar-2026-06-23-changes.md``
    """
    repo_a = tmp_path / "foo"
    repo_b = tmp_path / "bar"
    _init_git_repo(repo_a)
    _init_git_repo(repo_b)
    corpus = _setup_corpus(tmp_path)

    rc_a = _weave_dry(corpus, repo_a, "https://github.com/owner1/foo.git")
    assert rc_a == 0, f"weave(repo_a) failed with rc={rc_a}"

    rc_b = _weave_dry(corpus, repo_b, "https://github.com/owner2/bar.git")
    assert rc_b == 0, f"weave(repo_b) failed with rc={rc_b}"

    names = _inbox_names(corpus)

    expected_a = f"owner1__foo-{_UNTIL}-changes.md"
    expected_b = f"owner2__bar-{_UNTIL}-changes.md"

    assert expected_a in names, (
        f"REGRESSION: digest for owner1/foo not found in inbox.\n"
        f"Expected {expected_a!r} in {names}"
    )
    assert expected_b in names, (
        f"REGRESSION: digest for owner2/bar not found in inbox.\n"
        f"Expected {expected_b!r} in {names}"
    )
    assert expected_a != expected_b, "Sanity: filenames must differ."

    # Confirm the old bare filename is gone entirely.
    bare = f"{_UNTIL}-changes.md"
    assert bare not in names, (
        f"Bug not fixed: bare filename {bare!r} still present in inbox.  Inbox: {names}"
    )


# ---------------------------------------------------------------------------
# INC2 — SAME-BASENAME DIFFERENT-OWNER INCREMENTAL
# ---------------------------------------------------------------------------


def test_same_basename_different_owner_incremental(tmp_path: Path) -> None:
    """Two 'skills' repos (bkrabach vs microsoft) woven one-at-a-time → distinct files."""
    repo_bk = tmp_path / "amplifier-bundle-skills-bk"
    repo_ms = tmp_path / "amplifier-bundle-skills-ms"
    _init_git_repo(repo_bk)
    _init_git_repo(repo_ms)
    corpus = _setup_corpus(tmp_path)

    rc = _weave_dry(
        corpus,
        repo_bk,
        "https://github.com/bkrabach/amplifier-bundle-skills.git",
    )
    assert rc == 0

    rc = _weave_dry(
        corpus,
        repo_ms,
        "https://github.com/microsoft/amplifier-bundle-skills.git",
    )
    assert rc == 0

    names = _inbox_names(corpus)

    expected_bk = f"bkrabach__amplifier-bundle-skills-{_UNTIL}-changes.md"
    expected_ms = f"microsoft__amplifier-bundle-skills-{_UNTIL}-changes.md"

    assert expected_bk in names, (
        f"Expected bkrabach-scoped filename in inbox; got {names}"
    )
    assert expected_ms in names, (
        f"Expected microsoft-scoped filename in inbox; got {names}"
    )
    assert expected_bk != expected_ms


# ---------------------------------------------------------------------------
# INC3a — NO-REMOTE FALLBACK
# ---------------------------------------------------------------------------


def test_no_remote_falls_back_to_basename_no_crash(tmp_path: Path) -> None:
    """Local-only repo (no remote) → filename uses directory basename, no crash."""
    repo = tmp_path / "my-local-tool"
    _init_git_repo(repo)
    corpus = _setup_corpus(tmp_path)

    rc = _weave_dry(corpus, repo, origin_url=None)
    assert rc == 0, f"weave() with no-remote repo returned rc={rc}"

    names = _inbox_names(corpus)
    expected = f"my-local-tool-{_UNTIL}-changes.md"
    assert expected in names, (
        f"Expected basename-fallback filename {expected!r}; inbox: {names}"
    )

    # Must not contain a fabricated double-underscore owner prefix.
    for name in names:
        if name.endswith("-changes.md"):
            assert "__" not in name, (
                f"Filename {name!r} contains '__' — owner must not be fabricated "
                f"when there is no remote."
            )


# ---------------------------------------------------------------------------
# INC3b — TWO NO-REMOTE DIFFERENT BASENAMES
# ---------------------------------------------------------------------------


def test_two_no_remote_different_basenames_distinct_files(tmp_path: Path) -> None:
    """Two local-only repos with distinct basenames → two distinct inbox files."""
    repo_foo = tmp_path / "local-foo"
    repo_bar = tmp_path / "local-bar"
    _init_git_repo(repo_foo)
    _init_git_repo(repo_bar)
    corpus = _setup_corpus(tmp_path)

    rc = _weave_dry(corpus, repo_foo, origin_url=None)
    assert rc == 0

    rc = _weave_dry(corpus, repo_bar, origin_url=None)
    assert rc == 0

    names = _inbox_names(corpus)

    expected_foo = f"local-foo-{_UNTIL}-changes.md"
    expected_bar = f"local-bar-{_UNTIL}-changes.md"

    assert expected_foo in names, f"Expected {expected_foo!r} in inbox; got {names}"
    assert expected_bar in names, f"Expected {expected_bar!r} in inbox; got {names}"


# ---------------------------------------------------------------------------
# INC3c — TWO NO-REMOTE SAME BASENAME LIMITATION (accepted collision)
# ---------------------------------------------------------------------------


def test_two_no_remote_same_basename_no_crash(tmp_path: Path) -> None:
    """Two local-only repos with the SAME basename → collision is unavoidable; no crash.

    Without a remote there is no org to disambiguate.  We accept this limitation
    (documented) and assert only that no exception is raised and the single
    resulting file uses the basename form (no fabricated owner).
    """
    repo_a = tmp_path / "same-name-a" / "my-tool"
    repo_b = tmp_path / "same-name-b" / "my-tool"
    _init_git_repo(repo_a)
    _init_git_repo(repo_b)
    corpus = _setup_corpus(tmp_path)

    rc_a = _weave_dry(corpus, repo_a, origin_url=None)
    assert rc_a == 0, f"weave(repo_a) raised or returned rc={rc_a}"

    rc_b = _weave_dry(corpus, repo_b, origin_url=None)
    assert rc_b == 0, f"weave(repo_b) raised or returned rc={rc_b}"

    # Only one file in inbox since basenames match (second overwrote first).
    names = _inbox_names(corpus)
    digest_files = [n for n in names if n.endswith("-changes.md")]
    assert len(digest_files) == 1, (
        f"Two same-basename no-remote repos collide to ONE file — expected 1, "
        f"got {len(digest_files)}: {digest_files}"
    )
    assert digest_files[0] == f"my-tool-{_UNTIL}-changes.md"


# ---------------------------------------------------------------------------
# INC4a — DEDUP/IDEMPOTENCY: single-repo weave() archive-skip
# ---------------------------------------------------------------------------


def test_single_repo_archive_skip_qualified_name(tmp_path: Path) -> None:
    """weave() skips when the QUALIFIED digest is already in _archive/.

    After the fix, weave() computes file_qualifier the same way materialize()
    does and checks _archive/ for the qualified filename.  If it is present,
    weave() returns 0 without writing anything to _inbox/.
    """
    repo = tmp_path / "myrepo"
    _init_git_repo(repo)
    corpus = _setup_corpus(tmp_path)

    origin_url = "https://github.com/acme/myrepo.git"
    qualified_name = f"acme__myrepo-{_UNTIL}-changes.md"

    # Pre-seed _archive/ with the qualified filename.
    (corpus / "_archive" / qualified_name).write_text(
        "# archived from prior run\n", encoding="utf-8"
    )

    rc = _weave_dry(corpus, repo, origin_url=origin_url)
    assert rc == 0, f"weave() returned rc={rc} (expected 0 on archive-skip)"

    # _inbox/ must be empty — no new materialisation should have occurred.
    inbox_files = list((corpus / "_inbox").iterdir())
    assert len(inbox_files) == 0, (
        f"Archive-skip did not fire: {[f.name for f in inbox_files]} written to _inbox/"
    )


# ---------------------------------------------------------------------------
# INC4b — DEDUP/IDEMPOTENCY: weave_multi() archive-skip still matches qualified name
# ---------------------------------------------------------------------------


def test_weave_multi_archive_skip_qualified_name(tmp_path: Path) -> None:
    """weave_multi() skips a repo whose QUALIFIED digest is already in _archive/.

    This verifies that the archive-skip in weave_multi() continues to match the
    new qualified filename form after the SILENT-OVERWRITE fix to materialize().
    """
    repo_a = tmp_path / "repo-alpha"
    repo_b = tmp_path / "repo-beta"
    _init_git_repo(repo_a)
    _init_git_repo(repo_b)
    corpus = _setup_corpus(tmp_path)

    origin_a = "https://github.com/org/repo-alpha.git"
    origin_b = "https://github.com/org/repo-beta.git"
    qualified_a = f"org__repo-alpha-{_UNTIL}-changes.md"

    # Pre-seed _archive/ with repo-alpha's QUALIFIED digest.
    (corpus / "_archive" / qualified_a).write_text(
        "# archived from prior run\n", encoding="utf-8"
    )

    # get_origin_url returns different URLs per repo path.
    def _origin_side_effect(repo_path: str) -> str | None:
        if "repo-alpha" in repo_path:
            return origin_a
        if "repo-beta" in repo_path:
            return origin_b
        return None

    # Capture the real parse_owner_repo before any patch is applied so the
    # side_effect below calls the genuine implementation (not the mock).
    from repo_weaver import gitio as _gitio  # noqa: PLC0415

    _real_parse = _gitio.parse_owner_repo

    with (
        patch("repo_weaver.weave.gitio.is_git_repo", return_value=True),
        patch(
            "repo_weaver.weave.gitio.get_origin_url", side_effect=_origin_side_effect
        ),
        patch("repo_weaver.weave.gitio.parse_owner_repo", side_effect=_real_parse),
        patch(
            "repo_weaver.materialize.gitio.get_origin_url",
            side_effect=_origin_side_effect,
        ),
        patch("repo_weaver.materialize.gitio.get_window_rev", return_value=None),
        patch("repo_weaver.materialize.gitio.get_commits_name_only", return_value=[]),
        patch("repo_weaver.materialize.gitio.gh_merged_prs", return_value=([], None)),
        patch("repo_weaver.materialize.gitio.get_shortlog_authors", return_value=[]),
    ):
        rc = weave_multi(
            corpus=str(corpus),
            repos=[str(repo_a), str(repo_b)],
            since=_SINCE,
            until=_UNTIL,
            dry_run=True,
            no_fetch=True,
            _sleep=lambda _: None,
        )

    assert rc == 0, f"weave_multi() returned rc={rc}"

    names = _inbox_names(corpus)

    # repo-alpha was archived → should NOT appear in _inbox/.
    assert not any("repo-alpha" in n for n in names), (
        f"repo-alpha digest was archived but still appeared in _inbox/: {names}"
    )

    # repo-beta was NOT archived → must appear in _inbox/.
    expected_b = f"org__repo-beta-{_UNTIL}-changes.md"
    assert expected_b in names, (
        f"Expected repo-beta digest {expected_b!r} in _inbox/; got {names}"
    )


# ---------------------------------------------------------------------------
# INC4c — DEDUP/IDEMPOTENCY: no-remote archive-skip uses basename form
# ---------------------------------------------------------------------------


def test_no_remote_archive_skip_uses_basename(tmp_path: Path) -> None:
    """weave() with no-remote repo skips when BASENAME digest is already archived.

    Confirms the archive-skip path uses the same basename fallback as
    materialize() so the check is consistent for local-only repos.
    """
    repo = tmp_path / "local-proj"
    _init_git_repo(repo)
    corpus = _setup_corpus(tmp_path)

    basename_name = f"local-proj-{_UNTIL}-changes.md"
    (corpus / "_archive" / basename_name).write_text("# archived\n", encoding="utf-8")

    rc = _weave_dry(corpus, repo, origin_url=None)
    assert rc == 0, f"weave() returned rc={rc}"

    inbox_files = list((corpus / "_inbox").iterdir())
    assert len(inbox_files) == 0, (
        f"Archive-skip must fire for no-remote basename; "
        f"inbox unexpectedly contains: {[f.name for f in inbox_files]}"
    )
