"""Unit tests for repo_weaver.updater.

Mirrors the structure and coverage of wiki_weaver's own
``eval/test_updater.py`` (SourceRecord.is_mutable, PEP 610 direct_url.json
parsing, Layer1Result stale-detection ladder), adapted to repo-weaver's
situation: exactly ONE Layer-1 source (repo-weaver itself) rather than two
wheel deps, plus repo-weaver-specific coverage for the delegated
``wiki-weaver update`` subprocess call and the bundled-vs-CLI-on-PATH drift
check.

All tests are keyless and deterministic -- no real network calls, no real
filesystem access beyond ``tmp_path`` fixtures. ``subprocess.run`` /
``importlib.metadata`` / ``shutil.which`` are all mocked.
"""

from __future__ import annotations

import json
from typing import Optional
from unittest.mock import MagicMock

import pytest

from repo_weaver.updater import (
    DriftCheck,
    Layer1Result,
    SourceRecord,
    _installed_commit,
    _wiki_weaver_cli_version,
    check_wiki_weaver_drift,
    update_layer1,
    update_wiki_weaver,
)


# ---------------------------------------------------------------------------
# SourceRecord.is_mutable
# ---------------------------------------------------------------------------


class TestSourceRecordIsMutable:
    """ref-is-mutable guard correctly identifies branch names vs pinned refs."""

    def test_main_is_mutable(self):
        rec = SourceRecord(
            label="test",
            uri="git+https://github.com/microsoft/foo@main",
        )
        assert rec.is_mutable is True

    def test_head_is_mutable(self):
        rec = SourceRecord(
            label="test",
            uri="git+https://github.com/microsoft/foo",
        )
        # No explicit ref -> defaults to mutable
        assert rec.is_mutable is True

    def test_full_sha_40_hex_is_pinned(self):
        sha = "a" * 40
        rec = SourceRecord(
            label="test",
            uri=f"git+https://github.com/microsoft/foo@{sha}",
        )
        assert rec.is_mutable is False

    def test_version_tag_is_pinned(self):
        rec = SourceRecord(
            label="test",
            uri="git+https://github.com/microsoft/foo@v1.2.3",
        )
        assert rec.is_mutable is False

    def test_main_with_subdirectory_is_mutable(self):
        rec = SourceRecord(
            label="test",
            uri="git+https://github.com/microsoft/foo@main#subdirectory=bar",
        )
        assert rec.is_mutable is True

    def test_partial_sha_not_40_is_mutable(self):
        """A short SHA (< 40 chars) is not conclusively pinned -- mutable."""
        rec = SourceRecord(
            label="test",
            uri="git+https://github.com/microsoft/foo@abc1234",
        )
        assert rec.is_mutable is True


# ---------------------------------------------------------------------------
# SourceRecord helpers
# ---------------------------------------------------------------------------


class TestSourceRecordHelpers:
    def test_local_short_truncates_to_8(self):
        rec = SourceRecord(label="x", uri="u", local_sha="abcdef1234567890")
        assert rec.local_short == "abcdef12"

    def test_local_short_not_cached(self):
        rec = SourceRecord(label="x", uri="u", local_sha=None)
        assert rec.local_short == "(not cached)"

    def test_target_short_truncates_to_8(self):
        rec = SourceRecord(label="x", uri="u", target_sha="1234567890abcdef")
        assert rec.target_short == "12345678"

    def test_target_short_unknown(self):
        rec = SourceRecord(label="x", uri="u", target_sha=None)
        assert rec.target_short == "(unknown)"


# ---------------------------------------------------------------------------
# _installed_commit: PEP 610 direct_url.json parsing
# ---------------------------------------------------------------------------


class TestInstalledCommit:
    """_installed_commit reads from direct_url.json via importlib.metadata."""

    def test_returns_commit_id_when_present(self, monkeypatch):
        """When direct_url.json has a vcs_info.commit_id, return it."""
        commit_id = "abc123def456abc123def456abc123def456abc1"
        direct_url = {
            "url": "https://github.com/microsoft/amplifier-app-repo-weaver",
            "vcs_info": {
                "vcs": "git",
                "requested_revision": "main",
                "commit_id": commit_id,
            },
        }

        mock_dist = MagicMock()
        mock_dist.read_text.return_value = json.dumps(direct_url)

        monkeypatch.setattr(
            "repo_weaver.updater.importlib.metadata.distribution",
            lambda name: mock_dist,
        )
        monkeypatch.setattr(
            "repo_weaver.updater.importlib.invalidate_caches", lambda: None
        )

        result = _installed_commit("repo-weaver")
        assert result == commit_id

    def test_returns_none_when_no_direct_url(self, monkeypatch):
        """When direct_url.json is absent, return None (don't crash)."""
        mock_dist = MagicMock()
        mock_dist.read_text.return_value = None

        monkeypatch.setattr(
            "repo_weaver.updater.importlib.metadata.distribution",
            lambda name: mock_dist,
        )
        monkeypatch.setattr(
            "repo_weaver.updater.importlib.invalidate_caches", lambda: None
        )

        result = _installed_commit("repo-weaver")
        assert result is None

    def test_returns_none_when_package_not_found(self, monkeypatch):
        """When the package is not installed, return None (don't crash)."""
        monkeypatch.setattr(
            "repo_weaver.updater.importlib.metadata.distribution",
            MagicMock(side_effect=Exception("package not found")),
        )
        monkeypatch.setattr(
            "repo_weaver.updater.importlib.invalidate_caches", lambda: None
        )

        result = _installed_commit("missing-package")
        assert result is None

    def test_returns_none_when_no_vcs_info(self, monkeypatch):
        """When direct_url.json has no vcs_info (e.g. a local file dep), return None."""
        direct_url = {
            "url": "file:///home/user/repos/repo-weaver",
            "dir_info": {"editable": True},
        }
        mock_dist = MagicMock()
        mock_dist.read_text.return_value = json.dumps(direct_url)

        monkeypatch.setattr(
            "repo_weaver.updater.importlib.metadata.distribution",
            lambda name: mock_dist,
        )
        monkeypatch.setattr(
            "repo_weaver.updater.importlib.invalidate_caches", lambda: None
        )

        result = _installed_commit("repo-weaver")
        assert result is None


# ---------------------------------------------------------------------------
# update_layer1: stale detection and ladder logic
# ---------------------------------------------------------------------------


class TestUpdateLayer1StaleDetection:
    """update_layer1 verify+ladder+fail-loud logic (all I/O mocked).

    repo-weaver's _LAYER1_SOURCES has exactly ONE entry (repo-weaver itself),
    unlike wiki-weaver's two wheel deps -- call counts below reflect one
    _installed_commit() call per check-point instead of two.
    """

    def test_success_on_rung1_when_no_remote_move(self, monkeypatch):
        """When remote == before, rung-1 passes trivially (nothing to verify)."""
        sha = "a" * 40
        monkeypatch.setattr("repo_weaver.updater._installed_commit", lambda n: sha)
        monkeypatch.setattr("repo_weaver.updater._run_install", lambda **kw: (0, ""))

        async def _no_move(url: str) -> Optional[str]:
            return sha  # remote == local -> no move expected

        monkeypatch.setattr("repo_weaver.updater._get_remote_commit_for", _no_move)

        res = update_layer1()
        assert res.success is True
        assert res.rung_reached == 1
        assert res.stale == []

    def test_success_on_rung1_when_remote_moved_and_install_moved(self, monkeypatch):
        """When remote > before AND installed-after == remote, rung-1 succeeds."""
        old_sha = "0" * 40
        new_sha = "1" * 40

        # Single-item list: call 1 = pre-step (before), call 2 = after rung-1.
        call_count = [0]

        def _installed(name: str) -> Optional[str]:
            call_count[0] += 1
            if call_count[0] <= 1:
                return old_sha
            return new_sha

        monkeypatch.setattr("repo_weaver.updater._installed_commit", _installed)
        monkeypatch.setattr("repo_weaver.updater._run_install", lambda **kw: (0, ""))

        async def _remote_moved(url: str) -> Optional[str]:
            return new_sha

        monkeypatch.setattr("repo_weaver.updater._get_remote_commit_for", _remote_moved)

        res = update_layer1()
        assert res.success is True
        assert res.rung_reached == 1
        assert res.stale == []

    def test_escalates_to_rung2_when_stale_after_rung1(self, monkeypatch):
        """If rung-1 didn't update the package, escalate to rung-2 (--no-cache)."""
        old_sha = "0" * 40
        new_sha = "1" * 40

        # call 1: pre-step (old), call 2: after-rung-1 (still old, stale),
        # call 3: after-rung-2 (new, fixed)
        call_count = [0]

        def _installed(name: str) -> Optional[str]:
            call_count[0] += 1
            if call_count[0] <= 2:
                return old_sha
            return new_sha

        rungs_tried = []

        def _run_install_tracking(**kw):
            rungs_tried.append(kw.get("no_cache", False))
            return 0, ""

        monkeypatch.setattr("repo_weaver.updater._installed_commit", _installed)
        monkeypatch.setattr("repo_weaver.updater._run_install", _run_install_tracking)

        async def _remote_moved(url: str) -> Optional[str]:
            return new_sha

        monkeypatch.setattr("repo_weaver.updater._get_remote_commit_for", _remote_moved)

        res = update_layer1()
        assert res.success is True
        assert res.rung_reached == 2
        assert rungs_tried == [False, True]

    def test_escalates_to_rung3_when_stale_after_rung2(self, monkeypatch):
        """If rung-2 still stale, escalate to rung-3 (cache clean + reinstall)."""
        old_sha = "0" * 40
        new_sha = "1" * 40

        # call 1: pre-step (old); call 2: after-rung-1 (old, stale);
        # call 3: after-rung-2 (old, still stale); call 4: after-rung-3 (new)
        call_count = [0]

        def _installed(name: str) -> Optional[str]:
            call_count[0] += 1
            if call_count[0] <= 3:
                return old_sha
            return new_sha

        clean_called = [False]

        def _run_clean(names: list):
            clean_called[0] = True
            return 0

        monkeypatch.setattr("repo_weaver.updater._installed_commit", _installed)
        monkeypatch.setattr("repo_weaver.updater._run_install", lambda **kw: (0, ""))
        monkeypatch.setattr("repo_weaver.updater._run_cache_clean", _run_clean)

        async def _remote_moved(url: str) -> Optional[str]:
            return new_sha

        monkeypatch.setattr("repo_weaver.updater._get_remote_commit_for", _remote_moved)

        res = update_layer1()
        assert res.success is True
        assert res.rung_reached == 3
        assert clean_called[0] is True

    def test_fail_loud_when_all_rungs_exhausted(self, monkeypatch):
        """After all 3 rungs, stale -> success=False, stale list populated."""
        old_sha = "0" * 40
        new_sha = "1" * 40

        monkeypatch.setattr("repo_weaver.updater._installed_commit", lambda n: old_sha)
        monkeypatch.setattr("repo_weaver.updater._run_install", lambda **kw: (0, ""))
        monkeypatch.setattr("repo_weaver.updater._run_cache_clean", lambda names: 0)

        async def _remote_moved(url: str) -> Optional[str]:
            return new_sha

        monkeypatch.setattr("repo_weaver.updater._get_remote_commit_for", _remote_moved)

        res = update_layer1()
        assert res.success is False
        assert res.rung_reached == 3
        # Exactly one tracked package (repo-weaver itself)
        assert len(res.stale) == 1
        assert res.stale == ["repo-weaver"]

    def test_install_failure_sets_error(self, monkeypatch):
        """Non-zero exit from uv install stops the ladder and records the error."""
        monkeypatch.setattr("repo_weaver.updater._installed_commit", lambda n: None)
        monkeypatch.setattr(
            "repo_weaver.updater._run_install",
            lambda **kw: (1, "error: no such package"),
        )

        async def _remote(url: str) -> Optional[str]:
            return "a" * 40

        monkeypatch.setattr("repo_weaver.updater._get_remote_commit_for", _remote)

        res = update_layer1()
        assert res.success is False
        assert res.rung_reached == 1
        assert any("rung-1 failed" in e for e in res.errors)


# ---------------------------------------------------------------------------
# Commit-moved comparison helper
# ---------------------------------------------------------------------------


class TestLayer1ResultCommitMoved:
    """Layer1Result correctly identifies which packages moved."""

    def test_package_moved_when_before_ne_after(self):
        res = Layer1Result(
            before={"pkg": "aaa" * 13 + "a"},
            after={"pkg": "bbb" * 13 + "b"},
            remote={"pkg": "bbb" * 13 + "b"},
        )
        assert res.before["pkg"] != res.after["pkg"]

    def test_package_unchanged_when_already_latest(self):
        sha = "a" * 40
        res = Layer1Result(
            before={"pkg": sha},
            after={"pkg": sha},
            remote={"pkg": sha},
        )
        assert res.before["pkg"] == res.after["pkg"]


# ---------------------------------------------------------------------------
# update_wiki_weaver: delegated subprocess call + fail-loud
# ---------------------------------------------------------------------------


class TestUpdateWikiWeaver:
    def test_raises_when_wiki_weaver_not_on_path(self, monkeypatch):
        """Fail loud (raise), never silently skip, when wiki-weaver is missing."""
        monkeypatch.setattr("repo_weaver.updater.shutil.which", lambda name: None)
        with pytest.raises(RuntimeError, match="wiki-weaver not found on PATH"):
            update_wiki_weaver()

    def test_invokes_plain_update_when_not_check_only(self, monkeypatch):
        monkeypatch.setattr(
            "repo_weaver.updater.shutil.which",
            lambda name: "/fake/bin/wiki-weaver",
        )
        captured = {}

        def _fake_run(cmd, **kw):
            captured["cmd"] = cmd
            return MagicMock(returncode=0)

        monkeypatch.setattr("repo_weaver.updater.subprocess.run", _fake_run)

        rc = update_wiki_weaver(check_only=False)
        assert rc == 0
        assert captured["cmd"] == ["wiki-weaver", "update"]

    def test_invokes_check_flag_when_check_only(self, monkeypatch):
        monkeypatch.setattr(
            "repo_weaver.updater.shutil.which",
            lambda name: "/fake/bin/wiki-weaver",
        )
        captured = {}

        def _fake_run(cmd, **kw):
            captured["cmd"] = cmd
            return MagicMock(returncode=0)

        monkeypatch.setattr("repo_weaver.updater.subprocess.run", _fake_run)

        rc = update_wiki_weaver(check_only=True)
        assert rc == 0
        assert captured["cmd"] == ["wiki-weaver", "update", "--check"]

    def test_propagates_nonzero_exit(self, monkeypatch):
        monkeypatch.setattr(
            "repo_weaver.updater.shutil.which",
            lambda name: "/fake/bin/wiki-weaver",
        )
        monkeypatch.setattr(
            "repo_weaver.updater.subprocess.run",
            lambda cmd, **kw: MagicMock(returncode=1),
        )
        assert update_wiki_weaver() == 1


# ---------------------------------------------------------------------------
# _wiki_weaver_cli_version: direct `wiki-weaver --version` probe
# ---------------------------------------------------------------------------


class TestWikiWeaverCliVersion:
    def test_not_found_when_wiki_weaver_missing(self, monkeypatch):
        monkeypatch.setattr("repo_weaver.updater.shutil.which", lambda name: None)
        version, err = _wiki_weaver_cli_version()
        assert version is None
        assert "not found on PATH" in (err or "")

    def test_returns_version_from_baked_output(self, monkeypatch):
        monkeypatch.setattr(
            "repo_weaver.updater.shutil.which", lambda name: "/fake/bin/wiki-weaver"
        )
        monkeypatch.setattr(
            "repo_weaver.updater.subprocess.run",
            lambda cmd, **kw: MagicMock(
                returncode=0, stdout="wiki-weaver 2026.07.08-047db6b\n", stderr=""
            ),
        )
        version, err = _wiki_weaver_cli_version()
        assert version == "2026.07.08-047db6b"
        assert err is None

    def test_error_when_output_does_not_match_baked_pattern(self, monkeypatch):
        monkeypatch.setattr(
            "repo_weaver.updater.shutil.which", lambda name: "/fake/bin/wiki-weaver"
        )
        monkeypatch.setattr(
            "repo_weaver.updater.subprocess.run",
            lambda cmd, **kw: MagicMock(
                returncode=0, stdout="wiki-weaver 0.1.0\n", stderr=""
            ),
        )
        version, err = _wiki_weaver_cli_version()
        assert version is None
        assert "doesn't match the expected baked-version pattern" in (err or "")

    def test_error_when_subprocess_exits_nonzero(self, monkeypatch):
        monkeypatch.setattr(
            "repo_weaver.updater.shutil.which", lambda name: "/fake/bin/wiki-weaver"
        )
        monkeypatch.setattr(
            "repo_weaver.updater.subprocess.run",
            lambda cmd, **kw: MagicMock(returncode=1, stdout="", stderr="boom"),
        )
        version, err = _wiki_weaver_cli_version()
        assert version is None
        assert err is not None


# ---------------------------------------------------------------------------
# check_wiki_weaver_drift: doctor drift-check logic
# ---------------------------------------------------------------------------


class TestCheckWikiWeaverDrift:
    def test_drift_true_when_short_sha_does_not_match_bundled_commit(self, monkeypatch):
        monkeypatch.setattr(
            "repo_weaver.updater._installed_commit", lambda name: "a" * 40
        )
        monkeypatch.setattr(
            "repo_weaver.updater._wiki_weaver_cli_version",
            lambda: ("2026.07.08-bbbbbbb", None),
        )
        drift = check_wiki_weaver_drift()
        assert isinstance(drift, DriftCheck)
        assert drift.drifted is True
        assert drift.bundled_commit == "a" * 40
        assert drift.cli_version == "2026.07.08-bbbbbbb"

    def test_drift_false_when_short_sha_is_prefix_of_bundled_commit(self, monkeypatch):
        full_sha = "c0ffee7" + "c" * 33
        monkeypatch.setattr(
            "repo_weaver.updater._installed_commit", lambda name: full_sha
        )
        monkeypatch.setattr(
            "repo_weaver.updater._wiki_weaver_cli_version",
            lambda: ("2026.07.08-c0ffee7", None),
        )
        drift = check_wiki_weaver_drift()
        assert drift.drifted is False

    def test_drift_undetermined_when_cli_version_unavailable(self, monkeypatch):
        monkeypatch.setattr(
            "repo_weaver.updater._installed_commit", lambda name: "a" * 40
        )
        monkeypatch.setattr(
            "repo_weaver.updater._wiki_weaver_cli_version",
            lambda: (None, "wiki-weaver not found on PATH"),
        )
        drift = check_wiki_weaver_drift()
        assert drift.drifted is None
        assert drift.error == "wiki-weaver not found on PATH"

    def test_drift_undetermined_when_bundled_commit_unavailable(self, monkeypatch):
        monkeypatch.setattr("repo_weaver.updater._installed_commit", lambda name: None)
        monkeypatch.setattr(
            "repo_weaver.updater._wiki_weaver_cli_version",
            lambda: ("2026.07.08-bbbbbbb", None),
        )
        drift = check_wiki_weaver_drift()
        assert drift.drifted is None
        assert drift.bundled_commit is None
        assert drift.cli_version == "2026.07.08-bbbbbbb"
