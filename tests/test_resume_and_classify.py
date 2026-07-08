"""Tests for Feature 1 (resume-from-checkpoint) and Feature 2 (--no-classify).

All tests are deterministic, fast, and require no network access.
External tools (wiki-weaver) are faked via PATH injection (same pattern as
test_retry.py / test_prod_readiness.py).  git is used against real local
repos created in tmp_path (no remote, no network).

Test matrix
-----------
S1  Resume: completed window skipped; incomplete window runs.
S2  Resume: --restart forces ALL windows regardless of progress file.
S3  Resume: source in _failed/ from a prior run is RE-ATTEMPTED on resume.
S4  Resume: weave_multi skips archived repo (multi-repo optimisation).
F2a --no-classify: dependabot PR gets a full detail block (not collapsed).
F2b --no-classify=False (default): dependabot PR is collapsed in Routine section.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from wiki_weaver.lib import wiki_failed, wiki_sources

from repo_weaver.materialize import _build_change_digest
from repo_weaver.weave import (
    _load_replay_progress,
    _save_replay_progress,
    _window_key,
    replay_windows,
    weave_multi,
)


# ---------------------------------------------------------------------------
# Fake wiki-weaver (stdlib only) — succeeds for every source, logs calls
# ---------------------------------------------------------------------------

_FAKE_WW_SCRIPT = """\
#!/usr/bin/env python3
# Fake wiki-weaver for resume/no-classify tests.
# Succeeds for every source it encounters, logging each call.
import json
import sys
from pathlib import Path


def main() -> int:
    args = sys.argv[1:]
    if not args or args[0] in ("--version", "-V"):
        print("wiki-weaver 0.0.0-fake")
        return 0
    if args[0] != "ingest":
        return 0

    wiki = None
    source = None
    i = 1
    while i < len(args):
        if args[i] == "--wiki" and i + 1 < len(args):
            wiki = args[i + 1]
            i += 2
        elif args[i] == "--source" and i + 1 < len(args):
            source = args[i + 1]
            i += 2
        else:
            i += 1

    if not wiki:
        print("ERROR: --wiki required", file=sys.stderr)
        return 1

    corpus = Path(wiki)
    inbox = corpus / "_inbox"
    archive = corpus / "_sources"
    (corpus / ".wiki").mkdir(exist_ok=True)
    archive.mkdir(exist_ok=True)
    (corpus / ".wiki" / "failed").mkdir(exist_ok=True)

    call_log = corpus / ".fake-ww-calls.jsonl"
    paths = [inbox / source] if source else sorted(inbox.glob("*.md"))

    for src in paths:
        if not src.exists():
            continue
        with open(call_log, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"source": src.name}) + "\\n")
        src.rename(archive / src.name)

    return 0


sys.exit(main())
"""


def _install_fake_ww(bin_dir: Path) -> None:
    script = bin_dir / "wiki-weaver"
    script.write_text(_FAKE_WW_SCRIPT, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _read_call_log(corpus: Path) -> list[str]:
    """Return list of source filenames processed by the fake wiki-weaver."""
    log = corpus / ".fake-ww-calls.jsonl"
    if not log.exists():
        return []
    return [
        json.loads(line)["source"]
        for line in log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


@pytest.fixture()
def fake_ww_env(tmp_path):
    """Put a temp bin dir with the fake wiki-weaver first on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _install_fake_ww(bin_dir)
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{bin_dir}{os.pathsep}{old_path}"
    yield bin_dir
    os.environ["PATH"] = old_path


# ---------------------------------------------------------------------------
# Minimal git repo helper (no network, no remote)
# ---------------------------------------------------------------------------


def _init_git_repo(path: Path) -> str:
    """Create a minimal local git repo with one empty commit; return HEAD SHA."""
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
    r = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
    )
    return r.stdout.strip()


def _setup_corpus(tmp_path: Path) -> Path:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "_inbox").mkdir()
    wiki_sources(corpus).mkdir(parents=True, exist_ok=True)
    wiki_failed(corpus).mkdir(parents=True, exist_ok=True)
    return corpus


# ---------------------------------------------------------------------------
# S1 — Resume: completed window skipped; incomplete window runs
# ---------------------------------------------------------------------------


def test_replay_skips_completed_window(tmp_path, fake_ww_env):
    """Window 1 already in progress file → skipped; window 2 runs and completes.

    Verifies:
    - No wiki-weaver calls for window-1 sources (filename contains 'until_w1').
    - wiki-weaver called for window-2 sources.
    - Progress file updated to include both windows after a successful run.
    """
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    corpus = _setup_corpus(tmp_path)

    # Two non-overlapping windows in the past (repo commit is from today, so
    # neither window contains any commits — materialize still produces a digest).
    w1_since, w1_until = "2000-01-01", "2020-01-01"
    w2_since, w2_until = "2020-01-01", "2024-01-01"
    windows = [(w1_since, w1_until), (w2_since, w2_until)]

    # Pre-mark window 1 as complete.
    _save_replay_progress(corpus, {_window_key(w1_since, w1_until)})

    rc = replay_windows(
        corpus=str(corpus),
        repos=[str(repo)],
        windows=windows,
        _sleep=lambda _: None,
    )

    assert rc == 0, f"Expected rc=0, got {rc}"

    calls = _read_call_log(corpus)
    # Window 1 change-digest filename ends with "2020-01-01-changes.md"
    assert not any("2020-01-01" in s for s in calls), (
        f"Window 1 sources should be skipped (already completed); call log: {calls}"
    )
    # Window 2 change-digest filename ends with "2024-01-01-changes.md"
    assert any("2024-01-01" in s for s in calls), (
        f"Window 2 source should have been processed; call log: {calls}"
    )

    # Progress file must now contain both windows.
    completed = _load_replay_progress(corpus)
    assert _window_key(w1_since, w1_until) in completed, (
        "Window 1 should still be recorded as complete"
    )
    assert _window_key(w2_since, w2_until) in completed, (
        "Window 2 should now be recorded as complete"
    )


# ---------------------------------------------------------------------------
# S2 — Resume: --restart forces ALL windows regardless of progress file
# ---------------------------------------------------------------------------


def test_replay_restart_forces_all_windows(tmp_path, fake_ww_env):
    """--restart ignores and clears the progress file; BOTH windows run.

    Verifies:
    - wiki-weaver called for both window-1 and window-2 sources.
    - Progress file is rebuilt (both windows complete) after the run.
    """
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    corpus = _setup_corpus(tmp_path)

    w1_since, w1_until = "2000-01-01", "2020-01-01"
    w2_since, w2_until = "2020-01-01", "2024-01-01"
    windows = [(w1_since, w1_until), (w2_since, w2_until)]

    # Both windows pre-marked complete — restart must override.
    _save_replay_progress(
        corpus,
        {_window_key(w1_since, w1_until), _window_key(w2_since, w2_until)},
    )

    rc = replay_windows(
        corpus=str(corpus),
        repos=[str(repo)],
        windows=windows,
        restart=True,
        _sleep=lambda _: None,
    )

    assert rc == 0, f"Expected rc=0, got {rc}"

    calls = _read_call_log(corpus)
    assert any("2020-01-01" in s for s in calls), (
        f"Window 1 source missing from call log (restart not honoured?): {calls}"
    )
    assert any("2024-01-01" in s for s in calls), (
        f"Window 2 source missing from call log: {calls}"
    )

    completed = _load_replay_progress(corpus)
    assert _window_key(w1_since, w1_until) in completed
    assert _window_key(w2_since, w2_until) in completed


# ---------------------------------------------------------------------------
# S3 — Resume: source in _failed/ from a prior run is re-attempted
# ---------------------------------------------------------------------------


def test_replay_failed_source_reattempted_on_resume(tmp_path, fake_ww_env):
    """Source stranded in _failed/ from a prior run is re-attempted on resume.

    The window that produced the failure is NOT in the progress file (a failed
    window is never marked complete).  On resume, the window re-runs and
    _retry_failed_sources retries the stranded source, which the fake
    wiki-weaver converges successfully.

    Verifies:
    - Return code 0 (all sources converge).
    - The old failed source ends up in _archive/ (was genuinely re-attempted).
    - Progress file records the window as complete after the successful run.
    """
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    corpus = _setup_corpus(tmp_path)

    # Simulate a source left in .wiki/failed/ from a prior run.
    old_failed_name = "2023-06-15-old-digest.md"
    (wiki_failed(corpus) / old_failed_name).write_text(
        "# stranded from prior run\n", encoding="utf-8"
    )

    # Progress file is empty: the window was never marked complete.
    w_since, w_until = "2000-01-01", "2024-01-01"
    windows = [(w_since, w_until)]

    rc = replay_windows(
        corpus=str(corpus),
        repos=[str(repo)],
        windows=windows,
        _sleep=lambda _: None,
    )

    assert rc == 0, f"Expected rc=0, got {rc}"

    # The old failed source must have been retried and archived.
    assert (wiki_sources(corpus) / old_failed_name).exists(), (
        f"Stranded source {old_failed_name!r} was not re-attempted and archived. "
        f"_sources/ contains: {[p.name for p in wiki_sources(corpus).iterdir()]}"
    )

    # Window must now be in the progress file.
    completed = _load_replay_progress(corpus)
    assert _window_key(w_since, w_until) in completed, (
        "Window should be recorded as complete after all sources converged"
    )


# ---------------------------------------------------------------------------
# S4 — Resume: weave_multi skips archived repo (multi-repo path)
# ---------------------------------------------------------------------------


def test_weave_multi_skips_archived_repo(tmp_path, fake_ww_env):
    """In multi-repo mode, a repo whose change digest already lives in _archive/
    is skipped entirely (no materialize, no ingest call for its files).

    Setup: two repos; repo-a's change digest pre-placed in _archive/.
    Expected: wiki-weaver only processes repo-b sources; repo-a is silent.
    """
    repo_a = tmp_path / "repo-a"
    repo_b = tmp_path / "repo-b"
    _init_git_repo(repo_a)
    _init_git_repo(repo_b)
    corpus = _setup_corpus(tmp_path)

    until = "2024-01-01"

    # Simulate repo-a's change digest already in _sources/ from a prior run.
    archived_name = f"repo-a-{until}-changes.md"
    (wiki_sources(corpus) / archived_name).write_text(
        "# archived from prior run\n", encoding="utf-8"
    )

    rc = weave_multi(
        corpus=str(corpus),
        repos=[str(repo_a), str(repo_b)],
        since="2000-01-01",
        until=until,
        dry_run=False,
        _sleep=lambda _: None,
    )

    assert rc == 0, f"Expected rc=0, got {rc}"

    calls = _read_call_log(corpus)
    assert not any("repo-a" in s for s in calls), (
        f"repo-a sources should have been skipped (already archived); call log: {calls}"
    )
    assert any("repo-b" in s for s in calls), (
        f"repo-b sources should have been processed; call log: {calls}"
    )


# ---------------------------------------------------------------------------
# F2 — --no-classify: unit tests against _build_change_digest
#
# Both tests use unittest.mock.patch to inject synthetic PR dicts so no
# real gh subprocess is involved — pure deterministic unit tests.
# ---------------------------------------------------------------------------

# Synthetic PR fixtures used in both F2 tests.

_DEPENDABOT_PR: dict[str, object] = {
    "number": 1,
    "title": "chore(deps): Bump axios from 0.21.1 to 0.21.4",
    "author": {"login": "dependabot[bot]", "is_bot": True},
    "mergedAt": "2024-06-15T10:00:00Z",
    "body": "Bumps axios from 0.21.1 to 0.21.4.",
    "files": [{"path": "package.json"}, {"path": "package-lock.json"}],
}

_SUBSTANTIVE_PR: dict[str, object] = {
    "number": 42,
    "title": "feat(auth): add OAuth 2.0 login flow",
    "author": {"login": "alice"},
    "mergedAt": "2024-06-20T10:00:00Z",
    "body": "Adds full OAuth 2.0 PKCE flow with token refresh.",
    "files": [{"path": "src/auth.py"}, {"path": "tests/test_auth.py"}],
}


def _make_digest(classify: bool) -> str:
    """Call _build_change_digest with patched gh returning both synthetic PRs."""
    fake_prs = [_DEPENDABOT_PR, _SUBSTANTIVE_PR]
    with (
        patch(
            "repo_weaver.materialize.gitio.gh_merged_prs",
            return_value=(fake_prs, None),
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
            since="2024-01-01",
            until="2024-06-30",
            until_rev=None,
            commits=[],
            owner_repo=("example-owner", "example-repo"),
            max_prs=15,
            repo_qualifier=None,
            classify=classify,
        )


def test_no_classify_lists_all_prs_with_full_detail():
    """--no-classify: every PR (including dependabot) gets a full detail block.

    Verifies:
    - A flat '## Merged Pull Requests' section is produced.
    - Both PR #1 (dependabot) and PR #42 (substantive) appear as detail blocks.
    - The classified split sections ('## Substantive Changes' /
      '## Routine Maintenance') are absent.
    """
    digest = _make_digest(classify=False)

    assert "## Merged Pull Requests" in digest, (
        "Expected flat '## Merged Pull Requests' header under --no-classify; "
        f"digest snippet: {digest[:500]!r}"
    )
    assert "### PR #1:" in digest, (
        "Dependabot PR must appear with a full detail block under --no-classify; "
        f"digest snippet: {digest[:800]!r}"
    )
    assert "### PR #42:" in digest, (
        "Substantive PR must appear with a full detail block under --no-classify"
    )
    assert "## Substantive Changes" not in digest, (
        "Under --no-classify, the 'Substantive Changes' section must NOT appear"
    )
    assert "## Routine Maintenance" not in digest, (
        "Under --no-classify, the 'Routine Maintenance' section must NOT appear"
    )


def test_classify_default_collapses_routine_prs():
    """Default (classify=True): dependabot PR is collapsed; substantive PR is detailed.

    Verifies:
    - '## Substantive Changes' and '## Routine Maintenance' sections appear.
    - PR #1 (dependabot) does NOT get a full detail block (it is collapsed).
    - PR #42 (substantive) DOES get a full detail block.
    """
    digest = _make_digest(classify=True)

    assert "## Substantive Changes" in digest, (
        "Expected '## Substantive Changes' section when classify=True"
    )
    assert "## Routine Maintenance" in digest, (
        "Expected '## Routine Maintenance' section when classify=True"
    )
    assert "### PR #1:" not in digest, (
        "Routine (dependabot) PR #1 must be collapsed — not given a full detail "
        f"block — when classify=True; digest snippet: {digest[:800]!r}"
    )
    assert "### PR #42:" in digest, (
        "Substantive PR #42 must appear with a full detail block when classify=True"
    )
