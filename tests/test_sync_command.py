"""Tests for the ``repo-weaver sync`` command (deterministic glue over weave).

Test matrix
-----------
SYNC1  Last-sync-date derivation from a fake ``_sources/`` directory: the
       max ``YYYY-MM-DD`` across ``*-changes.md`` filenames is returned, and
       an empty/missing ``_sources/`` yields ``None``.

SYNC2  Tracked-repo derivation: only ``owner__repo`` qualified filenames
       contribute to the tracked set; the no-remote basename form is ignored.

SYNC3  CHANGED selection: with ``gh_list_repos`` monkeypatched to return a mix
       of tracked/untracked, fork/non-fork, and pushed-before/after-last-sync
       repos, only tracked + non-fork + pushed-since-last-sync repos are
       selected as CHANGED.

SYNC4  ``dry_run=True`` returns the changed list but does NOT clone or call
       the weave path (spied via monkeypatch).

SYNC5  CLI ``--dry-run`` wiring: ``cmd_sync`` calls ``sync_corpus`` with
       ``dry_run=True`` and prints the changed list without weaving.

SYNC6/7  Per-repo success determined by the landed digest file, not the raw
       ``weave()`` returncode (retry-recovery regression).

SYNC8  Per-repo watermark: ``_per_repo_last_sync`` computes each tracked
       repo's own last-sync date independently, and the main regression case
       -- a repo whose own last-sync date is OLDER than another repo's more
       recent digest must still be detected as CHANGED for activity that
       falls between its own last-sync date and the other repo's date (this
       was previously invisible under the corpus-wide-watermark bug).

SYNC9  Fail-loud ``gh`` discovery failures: a genuine ``gh_list_repos`` error
       is recorded in ``discovery_failed`` (distinct from "gh succeeded,
       zero repos") and makes the CLI exit non-zero; a genuine zero-repo
       response is NOT a discovery failure and exits zero.

SYNC10 ``sync --json`` emits a valid, parseable JSON result with the expected
       keys, and its exit code matches the human-readable path.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from repo_weaver import cli, gitio, sync

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_source(corpus: Path, filename: str) -> None:
    sources = corpus / "_sources"
    sources.mkdir(parents=True, exist_ok=True)
    (sources / filename).write_text("dummy content", encoding="utf-8")


# ---------------------------------------------------------------------------
# SYNC1 — last-sync-date derivation
# ---------------------------------------------------------------------------


def test_last_sync_date_missing_sources_dir_returns_none(tmp_path: Path):
    """No _sources/ directory at all -> None."""
    assert sync._last_sync_date(tmp_path) is None


def test_last_sync_date_empty_sources_dir_returns_none(tmp_path: Path):
    (tmp_path / "_sources").mkdir()
    assert sync._last_sync_date(tmp_path) is None


def test_last_sync_date_picks_max_across_filenames(tmp_path: Path):
    _write_source(
        tmp_path, "microsoft__amplifier-app-repo-weaver-2026-06-25-changes.md"
    )
    _write_source(tmp_path, "bkrabach__amplifier-agui-poc-2026-07-05-changes.md")
    _write_source(
        tmp_path, "michaeljabbour__amplifier-bundle-skills-2026-06-30-changes.md"
    )
    assert sync._last_sync_date(tmp_path) == "2026-07-05"


def test_last_sync_date_ignores_non_changes_files(tmp_path: Path):
    """Module snapshots and unrelated files must not affect the date."""
    _write_source(
        tmp_path, "microsoft__amplifier-app-repo-weaver-2026-06-25-changes.md"
    )
    _write_source(
        tmp_path, "module-microsoft__amplifier-app-repo-weaver-cli-2099-01-01.md"
    )
    (tmp_path / "_sources" / "README.md").write_text("x", encoding="utf-8")
    assert sync._last_sync_date(tmp_path) == "2026-06-25"


# ---------------------------------------------------------------------------
# SYNC2 — tracked-repo derivation
# ---------------------------------------------------------------------------


def test_tracked_repos_only_owner_qualified_filenames(tmp_path: Path):
    _write_source(
        tmp_path, "microsoft__amplifier-app-repo-weaver-2026-06-25-changes.md"
    )
    _write_source(tmp_path, "bkrabach__amplifier-agui-poc-2026-07-05-changes.md")
    # No-remote fallback form (no "__" qualifier) -- must be ignored.
    _write_source(tmp_path, "some-local-only-repo-2026-06-20-changes.md")

    tracked, owners = sync._tracked_repos(tmp_path)

    assert tracked == {
        ("microsoft", "amplifier-app-repo-weaver"),
        ("bkrabach", "amplifier-agui-poc"),
    }
    assert owners == {"microsoft", "bkrabach"}


# ---------------------------------------------------------------------------
# SYNC3 — CHANGED selection via monkeypatched gh_list_repos
# ---------------------------------------------------------------------------


def _make_corpus_with_tracked(tmp_path: Path) -> Path:
    corpus = tmp_path / "corpus"
    _write_source(corpus, "microsoft__amplifier-app-repo-weaver-2026-06-25-changes.md")
    _write_source(corpus, "microsoft__amplifier-bundle-skills-2026-06-25-changes.md")
    _write_source(corpus, "bkrabach__amplifier-agui-poc-2026-06-25-changes.md")
    return corpus


def test_sync_dry_run_selects_only_tracked_nonfork_pushed_since(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    corpus = _make_corpus_with_tracked(tmp_path)

    def fake_gh_list_repos(
        owner: str, include_forks: bool = True, visibility: str = "all"
    ) -> tuple[list[dict[str, Any]], None]:
        if owner == "microsoft":
            return [
                # Tracked + non-fork + pushed AFTER last-sync -> CHANGED
                {
                    "name": "amplifier-app-repo-weaver",
                    "isFork": False,
                    "pushedAt": "2026-07-01T00:00:00Z",
                    "nameWithOwner": "microsoft/amplifier-app-repo-weaver",
                },
                # Tracked + non-fork but pushed BEFORE last-sync -> unchanged
                {
                    "name": "amplifier-bundle-skills",
                    "isFork": False,
                    "pushedAt": "2026-06-20T00:00:00Z",
                    "nameWithOwner": "microsoft/amplifier-bundle-skills",
                },
                # NOT tracked at all -> ignored regardless of push date
                {
                    "name": "amplifier-untracked-repo",
                    "isFork": False,
                    "pushedAt": "2026-07-02T00:00:00Z",
                    "nameWithOwner": "microsoft/amplifier-untracked-repo",
                },
            ], None
        if owner == "bkrabach":
            return [
                # Tracked but IS a fork, pushed after last-sync -> excluded (fork)
                {
                    "name": "amplifier-agui-poc",
                    "isFork": True,
                    "pushedAt": "2026-07-03T00:00:00Z",
                    "nameWithOwner": "bkrabach/amplifier-agui-poc",
                },
            ], None
        return [], None

    monkeypatch.setattr(gitio, "gh_list_repos", fake_gh_list_repos)

    result = sync.sync_corpus(
        corpus=str(corpus),
        clones_dir=str(tmp_path / "clones"),
        dry_run=True,
    )

    assert result["last_sync"] == "2026-06-25"
    changed_names = {e["nameWithOwner"] for e in result["changed"]}
    assert changed_names == {"microsoft/amplifier-app-repo-weaver"}
    assert result["owners"] == {"bkrabach": 0, "microsoft": 1}
    assert result["discovery_failed"] == []


# ---------------------------------------------------------------------------
# SYNC4 — dry_run does NOT clone or weave
# ---------------------------------------------------------------------------


def test_sync_dry_run_does_not_clone_or_weave(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    corpus = _make_corpus_with_tracked(tmp_path)

    def fake_gh_list_repos(
        owner: str, include_forks: bool = True, visibility: str = "all"
    ) -> tuple[list[dict[str, Any]], None]:
        return [
            {
                "name": "amplifier-app-repo-weaver",
                "isFork": False,
                "pushedAt": "2026-07-01T00:00:00Z",
                "nameWithOwner": "microsoft/amplifier-app-repo-weaver",
            }
        ], None

    monkeypatch.setattr(gitio, "gh_list_repos", fake_gh_list_repos)

    with (
        patch("repo_weaver.sync._ensure_local_clone") as mock_clone,
        patch("repo_weaver.sync._weave") as mock_weave,
    ):
        result = sync.sync_corpus(
            corpus=str(corpus),
            clones_dir=str(tmp_path / "clones"),
            dry_run=True,
        )

        mock_clone.assert_not_called()
        mock_weave.assert_not_called()

    assert len(result["changed"]) == 1
    assert "woven" not in result
    # dry-run must not create the clones directory as a side effect either.
    assert not (tmp_path / "clones").exists()


def test_sync_non_dry_run_clones_and_weaves_each_changed_repo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Sanity check the non-dry-run path DOES call clone + weave once per changed repo.

    The mocked ``_weave`` writes the expected change-digest into ``_sources/``
    as its side effect (mirroring what a real successful weave call does) --
    accounting is based on that file's presence, not the raw returncode alone
    (see SYNC6/SYNC7 below).
    """
    corpus = _make_corpus_with_tracked(tmp_path)

    def fake_gh_list_repos(
        owner: str, include_forks: bool = True, visibility: str = "all"
    ) -> tuple[list[dict[str, Any]], None]:
        if owner == "microsoft":
            return [
                {
                    "name": "amplifier-app-repo-weaver",
                    "isFork": False,
                    "pushedAt": "2026-07-01T00:00:00Z",
                    "nameWithOwner": "microsoft/amplifier-app-repo-weaver",
                }
            ], None
        return [], None

    monkeypatch.setattr(gitio, "gh_list_repos", fake_gh_list_repos)

    def fake_weave_writes_digest(**kwargs: Any) -> int:
        _write_source(
            corpus, "microsoft__amplifier-app-repo-weaver-2026-07-07-changes.md"
        )
        return 0

    with (
        patch("repo_weaver.sync._ensure_local_clone", return_value=True) as mock_clone,
        patch(
            "repo_weaver.sync._weave", side_effect=fake_weave_writes_digest
        ) as mock_weave,
    ):
        result = sync.sync_corpus(
            corpus=str(corpus),
            clones_dir=str(tmp_path / "clones"),
            until="2026-07-07",
            dry_run=False,
        )

    mock_clone.assert_called_once()
    mock_weave.assert_called_once()
    _, weave_kwargs = mock_weave.call_args
    assert weave_kwargs["since"] == "2026-06-25"
    assert weave_kwargs["max_modules"] == 0
    assert result["woven"] == [
        {"repo": "microsoft/amplifier-app-repo-weaver", "returncode": 0}
    ]
    assert result["failed"] == []


# ---------------------------------------------------------------------------
# SYNC6 / SYNC7 -- per-repo success determined by the landed digest file,
# not the raw weave() returncode (regression for the retry-recovery bug:
# an OOM-killed (-9) initial ingest whose source is later recovered by
# wiki-weaver's own .wiki/failed/ retry logic must count as succeeded).
# ---------------------------------------------------------------------------


def test_sync_counts_repo_as_succeeded_when_digest_lands_despite_nonzero_rc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """weave() reports failure (e.g. stale -9 from an OOM-killed initial ingest)
    but its own internal retry-from-.wiki/failed/ recovery lands the digest
    anyway. sync_corpus must count this repo as succeeded (empirical check:
    the expected _sources/*-changes.md file exists), and the CLI must exit 0.
    """
    corpus = _make_corpus_with_tracked(tmp_path)

    def fake_gh_list_repos(
        owner: str, include_forks: bool = True, visibility: str = "all"
    ) -> tuple[list[dict[str, Any]], None]:
        if owner == "microsoft":
            return [
                {
                    "name": "amplifier-app-repo-weaver",
                    "isFork": False,
                    "pushedAt": "2026-07-01T00:00:00Z",
                    "nameWithOwner": "microsoft/amplifier-app-repo-weaver",
                }
            ], None
        return [], None

    monkeypatch.setattr(gitio, "gh_list_repos", fake_gh_list_repos)

    def fake_weave_recovers_but_reports_stale_failure(**kwargs: Any) -> int:
        # Simulates weave()'s own retry mechanism recovering the source even
        # though the returncode still reflects the initial (pre-retry) crash.
        _write_source(
            corpus, "microsoft__amplifier-app-repo-weaver-2026-07-07-changes.md"
        )
        return -9

    with (
        patch("repo_weaver.sync._ensure_local_clone", return_value=True),
        patch(
            "repo_weaver.sync._weave",
            side_effect=fake_weave_recovers_but_reports_stale_failure,
        ),
    ):
        result = sync.sync_corpus(
            corpus=str(corpus),
            clones_dir=str(tmp_path / "clones"),
            until="2026-07-07",
            dry_run=False,
        )

    assert result["woven"] == [
        {"repo": "microsoft/amplifier-app-repo-weaver", "returncode": -9}
    ]
    assert result["failed"] == [], (
        "A repo whose digest landed must never be reported as failed, "
        "regardless of weave()'s raw returncode."
    )

    args = argparse.Namespace(
        corpus=str(corpus),
        clones_dir=str(tmp_path / "clones"),
        since=None,
        until="2026-07-07",
        dry_run=False,
        max_modules=0,
    )
    with patch("repo_weaver.cli.sync_corpus", return_value=result):
        rc = cli.cmd_sync(args)
    assert rc == 0


def test_sync_counts_repo_as_failed_when_digest_never_lands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A genuine failure -- the expected digest never appears in _sources/ --
    must still be counted as failed and the CLI must exit non-zero.
    """
    corpus = _make_corpus_with_tracked(tmp_path)

    def fake_gh_list_repos(
        owner: str, include_forks: bool = True, visibility: str = "all"
    ) -> tuple[list[dict[str, Any]], None]:
        if owner == "microsoft":
            return [
                {
                    "name": "amplifier-app-repo-weaver",
                    "isFork": False,
                    "pushedAt": "2026-07-01T00:00:00Z",
                    "nameWithOwner": "microsoft/amplifier-app-repo-weaver",
                }
            ], None
        return [], None

    monkeypatch.setattr(gitio, "gh_list_repos", fake_gh_list_repos)

    with (
        patch("repo_weaver.sync._ensure_local_clone", return_value=True),
        patch("repo_weaver.sync._weave", return_value=1),
    ):
        result = sync.sync_corpus(
            corpus=str(corpus),
            clones_dir=str(tmp_path / "clones"),
            until="2026-07-07",
            dry_run=False,
        )

    assert result["woven"] == [
        {"repo": "microsoft/amplifier-app-repo-weaver", "returncode": 1}
    ]
    assert result["failed"] == ["microsoft/amplifier-app-repo-weaver"]

    args = argparse.Namespace(
        corpus=str(corpus),
        clones_dir=str(tmp_path / "clones"),
        since=None,
        until="2026-07-07",
        dry_run=False,
        max_modules=0,
    )
    with patch("repo_weaver.cli.sync_corpus", return_value=result):
        rc = cli.cmd_sync(args)
    assert rc == 1


# ---------------------------------------------------------------------------
# SYNC5 — CLI --dry-run wiring
# ---------------------------------------------------------------------------


def test_cmd_sync_dry_run_calls_sync_corpus_with_dry_run_true():
    args = argparse.Namespace(
        corpus="/fake/corpus",
        clones_dir="~/fake-clones",
        since=None,
        until=None,
        dry_run=True,
        max_modules=0,
    )

    fake_result = {
        "last_sync": "2026-06-25",
        "until": "2026-07-05",
        "owners": {"microsoft": 1},
        "changed": [
            {
                "owner": "microsoft",
                "repo": "amplifier-app-repo-weaver",
                "nameWithOwner": "microsoft/amplifier-app-repo-weaver",
                "pushedAt": "2026-07-01T00:00:00Z",
                "since": "2026-06-25",
            }
        ],
        "errors": [],
        "discovery_failed": [],
    }

    with patch("repo_weaver.cli.sync_corpus", return_value=fake_result) as mock_sync:
        rc = cli.cmd_sync(args)

    mock_sync.assert_called_once_with(
        corpus="/fake/corpus",
        clones_dir="~/fake-clones",
        since=None,
        until=None,
        dry_run=True,
        max_modules=0,
    )
    assert rc == 0


def test_cmd_sync_reports_error_on_missing_last_sync_date():
    args = argparse.Namespace(
        corpus="/fake/corpus",
        clones_dir="~/fake-clones",
        since=None,
        until=None,
        dry_run=True,
        max_modules=0,
    )

    with patch(
        "repo_weaver.cli.sync_corpus",
        side_effect=ValueError("No last-sync date available"),
    ):
        rc = cli.cmd_sync(args)

    assert rc == 1


# ---------------------------------------------------------------------------
# SYNC8 — per-repo watermark: each tracked repo's own last-sync date
# ---------------------------------------------------------------------------


def test_per_repo_last_sync_computes_each_repo_independently(tmp_path: Path):
    """Two repos with different digest histories -> two independent watermarks."""
    _write_source(
        tmp_path, "microsoft__amplifier-app-repo-weaver-2026-06-01-changes.md"
    )
    _write_source(tmp_path, "microsoft__amplifier-bundle-skills-2026-07-05-changes.md")
    # A second, older digest for the same repo -- max() must still win.
    _write_source(
        tmp_path, "microsoft__amplifier-app-repo-weaver-2026-05-01-changes.md"
    )

    per_repo = sync._per_repo_last_sync(tmp_path)

    assert per_repo == {
        ("microsoft", "amplifier-app-repo-weaver"): "2026-06-01",
        ("microsoft", "amplifier-bundle-skills"): "2026-07-05",
    }


def test_sync_detects_repo_change_masked_by_old_corpus_wide_watermark_regression(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """THE regression case: repo A's own last-sync (2026-06-01) is much older
    than repo B's (2026-07-05). Repo A pushed at 2026-06-15 -- AFTER its own
    last-sync but BEFORE the corpus-wide max (2026-07-05).

    Under the old (buggy) corpus-wide-watermark behaviour, repo A's push date
    would be compared against 2026-07-05 (repo B's date) and found NOT
    changed -- silently hiding real activity. The fix compares repo A's push
    date against ITS OWN last-sync date (2026-06-01), correctly detecting it
    as CHANGED.
    """
    corpus = tmp_path / "corpus"
    _write_source(corpus, "microsoft__amplifier-app-repo-weaver-2026-06-01-changes.md")
    _write_source(corpus, "microsoft__amplifier-bundle-skills-2026-07-05-changes.md")

    def fake_gh_list_repos(
        owner: str, include_forks: bool = True, visibility: str = "all"
    ) -> tuple[list[dict[str, Any]], None]:
        assert owner == "microsoft"
        return [
            # Pushed AFTER its own last-sync (2026-06-01) but BEFORE the
            # other repo's more recent digest (2026-07-05) -- this is the
            # activity the old global-watermark bug would silently hide.
            {
                "name": "amplifier-app-repo-weaver",
                "isFork": False,
                "pushedAt": "2026-06-15T00:00:00Z",
                "nameWithOwner": "microsoft/amplifier-app-repo-weaver",
            },
            # Not pushed since its own last-sync -> unchanged.
            {
                "name": "amplifier-bundle-skills",
                "isFork": False,
                "pushedAt": "2026-07-05T00:00:00Z",
                "nameWithOwner": "microsoft/amplifier-bundle-skills",
            },
        ], None

    monkeypatch.setattr(gitio, "gh_list_repos", fake_gh_list_repos)

    result = sync.sync_corpus(
        corpus=str(corpus),
        clones_dir=str(tmp_path / "clones"),
        dry_run=True,
    )

    changed_names = {e["nameWithOwner"] for e in result["changed"]}
    assert changed_names == {"microsoft/amplifier-app-repo-weaver"}, (
        "Repo A's activity between its own last-sync and repo B's more "
        "recent digest must be detected -- this is the per-repo-watermark "
        "regression fix."
    )
    # Each changed entry carries its OWN effective since, not the corpus max.
    entry = next(iter(result["changed"]))
    assert entry["since"] == "2026-06-01"


def test_sync_explicit_since_override_applies_globally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """An explicit --since overrides EVERY repo's own watermark (intentional
    caller directive), not just the reporting field."""
    corpus = tmp_path / "corpus"
    _write_source(corpus, "microsoft__amplifier-app-repo-weaver-2026-06-01-changes.md")
    _write_source(corpus, "microsoft__amplifier-bundle-skills-2026-07-05-changes.md")

    def fake_gh_list_repos(
        owner: str, include_forks: bool = True, visibility: str = "all"
    ) -> tuple[list[dict[str, Any]], None]:
        return [
            {
                "name": "amplifier-app-repo-weaver",
                "isFork": False,
                "pushedAt": "2026-06-10T00:00:00Z",
                "nameWithOwner": "microsoft/amplifier-app-repo-weaver",
            },
            {
                "name": "amplifier-bundle-skills",
                "isFork": False,
                "pushedAt": "2026-06-10T00:00:00Z",
                "nameWithOwner": "microsoft/amplifier-bundle-skills",
            },
        ], None

    monkeypatch.setattr(gitio, "gh_list_repos", fake_gh_list_repos)

    # Explicit --since predates both pushes -> BOTH repos changed, regardless
    # of their own (later) per-repo digest dates.
    result = sync.sync_corpus(
        corpus=str(corpus),
        clones_dir=str(tmp_path / "clones"),
        since="2026-06-01",
        dry_run=True,
    )

    changed_names = {e["nameWithOwner"] for e in result["changed"]}
    assert changed_names == {
        "microsoft/amplifier-app-repo-weaver",
        "microsoft/amplifier-bundle-skills",
    }
    assert all(e["since"] == "2026-06-01" for e in result["changed"])


# ---------------------------------------------------------------------------
# SYNC9 — fail-loud gh discovery failures
# ---------------------------------------------------------------------------


def test_sync_zero_repos_success_is_not_a_discovery_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """gh succeeds but returns zero repos -> no error, no discovery_failed, exit 0."""
    corpus = _make_corpus_with_tracked(tmp_path)

    def fake_gh_list_repos(
        owner: str, include_forks: bool = True, visibility: str = "all"
    ) -> tuple[list[dict[str, Any]], None]:
        return [], None

    monkeypatch.setattr(gitio, "gh_list_repos", fake_gh_list_repos)

    result = sync.sync_corpus(
        corpus=str(corpus),
        clones_dir=str(tmp_path / "clones"),
        dry_run=True,
    )

    assert result["discovery_failed"] == []
    assert result["changed"] == []

    args = argparse.Namespace(
        corpus=str(corpus),
        clones_dir=str(tmp_path / "clones"),
        since=None,
        until=None,
        dry_run=True,
        max_modules=0,
    )
    with patch("repo_weaver.cli.sync_corpus", return_value=result):
        rc = cli.cmd_sync(args)
    assert rc == 0


def test_sync_records_discovery_failed_and_cli_exits_nonzero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A genuine gh failure (auth/rate-limit/network) is distinguishable from
    "zero repos" -- recorded in discovery_failed, surfaced in errors, and the
    CLI exits non-zero even though nothing else failed.
    """
    corpus = _make_corpus_with_tracked(tmp_path)

    def fake_gh_list_repos(
        owner: str, include_forks: bool = True, visibility: str = "all"
    ) -> tuple[list[dict[str, Any]], str | None]:
        if owner == "microsoft":
            return [], "gh error: authentication failed"
        return [], None

    monkeypatch.setattr(gitio, "gh_list_repos", fake_gh_list_repos)

    result = sync.sync_corpus(
        corpus=str(corpus),
        clones_dir=str(tmp_path / "clones"),
        dry_run=True,
    )

    assert result["discovery_failed"] == ["microsoft"]
    assert any("authentication failed" in e for e in result["errors"])
    # The CHANGED list is incomplete for "microsoft" -- no repos evaluated.
    assert result["changed"] == []

    args = argparse.Namespace(
        corpus=str(corpus),
        clones_dir=str(tmp_path / "clones"),
        since=None,
        until=None,
        dry_run=True,
        max_modules=0,
    )
    with patch("repo_weaver.cli.sync_corpus", return_value=result):
        rc = cli.cmd_sync(args)
    assert rc == 1, (
        "A genuine gh discovery failure must exit non-zero even in dry-run "
        "and even with an empty changed list -- this is NOT a real no-op."
    )


# ---------------------------------------------------------------------------
# SYNC10 — `sync --json`
# ---------------------------------------------------------------------------


def test_cmd_sync_json_output_is_valid_json_with_expected_keys(
    capsys: pytest.CaptureFixture[str],
):
    args = argparse.Namespace(
        corpus="/fake/corpus",
        clones_dir="~/fake-clones",
        since=None,
        until=None,
        dry_run=True,
        max_modules=0,
        json=True,
    )

    fake_result = {
        "last_sync": "2026-06-25",
        "until": "2026-07-05",
        "owners": {"microsoft": 1},
        "changed": [
            {
                "owner": "microsoft",
                "repo": "amplifier-app-repo-weaver",
                "nameWithOwner": "microsoft/amplifier-app-repo-weaver",
                "pushedAt": "2026-07-01T00:00:00Z",
                "since": "2026-06-25",
            }
        ],
        "errors": [],
        "discovery_failed": [],
    }

    with patch("repo_weaver.cli.sync_corpus", return_value=fake_result):
        rc = cli.cmd_sync(args)

    assert rc == 0
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed["last_sync"] == "2026-06-25"
    assert parsed["until"] == "2026-07-05"
    assert len(parsed["changed"]) == 1
    assert (
        parsed["changed"][0]["nameWithOwner"] == "microsoft/amplifier-app-repo-weaver"
    )
    assert parsed["errors"] == []
    assert parsed["discovery_failed"] == []


def test_cmd_sync_json_output_reflects_discovery_failure_exit_code(
    capsys: pytest.CaptureFixture[str],
):
    args = argparse.Namespace(
        corpus="/fake/corpus",
        clones_dir="~/fake-clones",
        since=None,
        until=None,
        dry_run=True,
        max_modules=0,
        json=True,
    )

    fake_result = {
        "last_sync": "2026-06-25",
        "until": "2026-07-05",
        "owners": {},
        "changed": [],
        "errors": ["microsoft: gh error: authentication failed"],
        "discovery_failed": ["microsoft"],
    }

    with patch("repo_weaver.cli.sync_corpus", return_value=fake_result):
        rc = cli.cmd_sync(args)

    assert rc == 1
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed["discovery_failed"] == ["microsoft"]
