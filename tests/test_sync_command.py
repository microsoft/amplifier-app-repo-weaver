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
"""

from __future__ import annotations

import argparse
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
    # No-remote fallback form (no "__" qualifier) — must be ignored.
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

    def fake_gh_list_repos(owner: str) -> list[dict[str, Any]]:
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
            ]
        if owner == "bkrabach":
            return [
                # Tracked but IS a fork, pushed after last-sync -> excluded (fork)
                {
                    "name": "amplifier-agui-poc",
                    "isFork": True,
                    "pushedAt": "2026-07-03T00:00:00Z",
                    "nameWithOwner": "bkrabach/amplifier-agui-poc",
                },
            ]
        return []

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


# ---------------------------------------------------------------------------
# SYNC4 — dry_run does NOT clone or weave
# ---------------------------------------------------------------------------


def test_sync_dry_run_does_not_clone_or_weave(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    corpus = _make_corpus_with_tracked(tmp_path)

    def fake_gh_list_repos(owner: str) -> list[dict[str, Any]]:
        return [
            {
                "name": "amplifier-app-repo-weaver",
                "isFork": False,
                "pushedAt": "2026-07-01T00:00:00Z",
                "nameWithOwner": "microsoft/amplifier-app-repo-weaver",
            }
        ]

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
    """Sanity check the non-dry-run path DOES call clone + weave once per changed repo."""
    corpus = _make_corpus_with_tracked(tmp_path)

    def fake_gh_list_repos(owner: str) -> list[dict[str, Any]]:
        if owner == "microsoft":
            return [
                {
                    "name": "amplifier-app-repo-weaver",
                    "isFork": False,
                    "pushedAt": "2026-07-01T00:00:00Z",
                    "nameWithOwner": "microsoft/amplifier-app-repo-weaver",
                }
            ]
        return []

    monkeypatch.setattr(gitio, "gh_list_repos", fake_gh_list_repos)

    with (
        patch("repo_weaver.sync._ensure_local_clone", return_value=True) as mock_clone,
        patch("repo_weaver.sync._weave", return_value=0) as mock_weave,
    ):
        result = sync.sync_corpus(
            corpus=str(corpus),
            clones_dir=str(tmp_path / "clones"),
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
            }
        ],
        "errors": [],
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
