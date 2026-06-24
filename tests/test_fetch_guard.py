"""Tests for Change 4: fetch-or-warn before materialize (stale-clone guard).

Proves:
  FG1  When a local clone is behind origin, a WARNING is emitted to stderr
       and a fast-forward is attempted.
  FG2  When ``--no-fetch`` (no_fetch=True), the staleness check is skipped
       entirely — no warning, no git fetch.

Uses a real local bare-repo + clone setup (no network, no mocks) so the
full git fetch + rev-list + merge --ff-only code path is exercised.

All tests are deterministic and require no external services.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from repo_weaver.weave import _ensure_fresh_clone


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@test.com",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@test.com",
}


def _git(*args: str, cwd: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        capture_output=True,
        text=True,
        cwd=cwd,
        env=_GIT_ENV,
    )


def _make_stale_clone(tmp_path: Path) -> tuple[Path, Path]:
    """Create an origin bare repo + a local clone that is one commit behind.

    Returns (origin, local).

    Timeline:
      1. ``origin`` is a bare repo initialised with one commit (via a seed clone).
      2. ``local`` is cloned from ``origin`` at that point.
      3. A second commit is pushed to ``origin`` (via the seed clone).
      4. ``local`` is now one commit behind ``origin`` — staleness confirmed
         AFTER a ``git fetch`` updates its remote refs.
    """
    origin = tmp_path / "origin.git"
    seed = tmp_path / "seed"
    local = tmp_path / "local"

    # 1. Bare origin
    _git("init", "--bare", str(origin))

    # 2. Seed clone to push the initial commit
    _git("clone", str(origin), str(seed))
    _git("-C", str(seed), "config", "user.email", "test@test.com")
    _git("-C", str(seed), "config", "user.name", "Test")
    (seed / "init.txt").write_text("initial content", encoding="utf-8")
    _git("-C", str(seed), "add", "init.txt")
    _git("-C", str(seed), "commit", "-m", "initial commit")
    _git("-C", str(seed), "push", "origin", "HEAD")

    # 3. Local clone (starts even with origin at commit 1)
    _git("clone", str(origin), str(local))
    _git("-C", str(local), "config", "user.email", "test@test.com")
    _git("-C", str(local), "config", "user.name", "Test")

    # 4. Push a second commit to origin (local is now behind by 1)
    (seed / "extra.txt").write_text("extra content", encoding="utf-8")
    _git("-C", str(seed), "add", "extra.txt")
    _git("-C", str(seed), "commit", "-m", "second commit (behind trigger)")
    _git("-C", str(seed), "push", "origin", "HEAD")

    return origin, local


# ---------------------------------------------------------------------------
# FG1 — warn + FF attempted when clone is behind
# ---------------------------------------------------------------------------


def test_warn_when_clone_is_behind(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:  # type: ignore[type-arg]
    """When the local clone is behind origin, WARNING is emitted to stderr
    and a fast-forward is attempted (and succeeds for a clean working tree).

    Verifies:
    - stderr contains a stale-clone WARNING message.
    - After the call, the local clone has been fast-forwarded to HEAD of origin
      (confirming the FF path was actually exercised).
    """
    _, local = _make_stale_clone(tmp_path)

    _ensure_fresh_clone(str(local), no_fetch=False)

    captured = capsys.readouterr()
    combined = captured.err + captured.out

    assert "WARNING" in combined, (
        f"Expected a WARNING on stderr when clone is stale; "
        f"got stderr={captured.err!r}, stdout={captured.out!r}"
    )
    assert "behind" in combined.lower(), (
        f"WARNING must mention 'behind'; got: {combined!r}"
    )

    # Verify the fast-forward was applied: local HEAD must now equal origin HEAD.
    r_local = _git("-C", str(local), "rev-parse", "HEAD")
    r_origin = _git("-C", str(local), "rev-parse", "origin/HEAD")
    assert r_local.returncode == 0 and r_origin.returncode == 0, (
        "git rev-parse commands must succeed"
    )
    local_sha = r_local.stdout.strip()
    origin_sha = r_origin.stdout.strip()
    assert local_sha == origin_sha, (
        f"After _ensure_fresh_clone, local HEAD ({local_sha[:8]}) must equal "
        f"origin HEAD ({origin_sha[:8]}) — fast-forward not applied?"
    )


def test_no_warning_when_already_up_to_date(
    tmp_path: Path,
    capsys: pytest.CaptureFixture,  # type: ignore[type-arg]
) -> None:
    """When local clone is already up to date, no WARNING is emitted."""
    _, local = _make_stale_clone(tmp_path)

    # Fast-forward local to be current first
    _git("-C", str(local), "fetch", "origin")
    _git("-C", str(local), "merge", "--ff-only", "origin/HEAD")

    _ensure_fresh_clone(str(local), no_fetch=False)

    captured = capsys.readouterr()
    combined = captured.err + captured.out
    assert "WARNING" not in combined, (
        f"No WARNING expected when clone is up to date; got: {combined!r}"
    )


# ---------------------------------------------------------------------------
# FG2 — --no-fetch skips the check entirely
# ---------------------------------------------------------------------------


def test_no_fetch_skips_warning(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:  # type: ignore[type-arg]
    """With no_fetch=True, the staleness check is skipped.

    Even when the local clone IS behind, no WARNING is emitted and no
    git fetch or fast-forward is performed.

    Verifies:
    - No WARNING or stale-related message appears.
    - Local HEAD remains at the old commit (no FF was applied).
    """
    _, local = _make_stale_clone(tmp_path)

    # Record current local HEAD before the call
    r_before = _git("-C", str(local), "rev-parse", "HEAD")
    sha_before = r_before.stdout.strip()

    _ensure_fresh_clone(str(local), no_fetch=True)

    captured = capsys.readouterr()
    combined = captured.err + captured.out
    assert "WARNING" not in combined, (
        f"With no_fetch=True, no WARNING should be emitted; got: {combined!r}"
    )

    # Local HEAD must be unchanged (no FF performed)
    r_after = _git("-C", str(local), "rev-parse", "HEAD")
    sha_after = r_after.stdout.strip()
    assert sha_before == sha_after, (
        f"With no_fetch=True, local HEAD must not change "
        f"({sha_before[:8]} → {sha_after[:8]})"
    )


# ---------------------------------------------------------------------------
# FG3 — no remote → no-op (no crash, no warning)
# ---------------------------------------------------------------------------


def test_no_remote_is_noop(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:  # type: ignore[type-arg]
    """A repo with no 'origin' remote does not warn or crash."""
    repo = tmp_path / "no-remote"
    repo.mkdir()
    _git("init", str(repo))
    _git("-C", str(repo), "config", "user.email", "test@test.com")
    _git("-C", str(repo), "config", "user.name", "Test")
    (repo / "f.txt").write_text("x", encoding="utf-8")
    _git("-C", str(repo), "add", "f.txt")
    _git("-C", str(repo), "commit", "-m", "solo commit")

    # Should complete without raising and without any WARNING
    _ensure_fresh_clone(str(repo), no_fetch=False)

    captured = capsys.readouterr()
    combined = captured.err + captured.out
    assert "WARNING" not in combined, (
        f"No WARNING expected for repo without remote; got: {combined!r}"
    )
