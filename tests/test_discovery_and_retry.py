"""Tests for gh CLI retry/back-off (gitio._run_gh_with_retry) and the
discovery MECHANISM (gitio.discover_repos + `repo-weaver discover`).

Test matrix
-----------
RETRY1  A command that fails twice then succeeds is retried and the final
        success is returned; the retry sleep is called with the expected
        exponential back-off delays.
RETRY2  A command that fails every attempt surfaces the LAST failure --
        never swallowed as a fabricated success.
RETRY3  A command that succeeds on the first attempt never sleeps/retries.

DISC1   discover_repos() applies each rule's own include_forks/visibility
        and merges + deduplicates matched repos across rules by
        nameWithOwner.
DISC2   A failing rule (gh discovery error) does not abort discovery of the
        other rules -- errors collected, not raised.
DISC3   gh_list_repos(include_forks=False) filters forks client-side.
DISC4   CLI `discover --rules-file ... --json` produces valid JSON with the
        expected keys.
DISC5   CLI `discover` surfaces a clear error for a missing/invalid rules file.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from repo_weaver import cli, gitio

# ---------------------------------------------------------------------------
# RETRY -- gitio._run_gh_with_retry
# ---------------------------------------------------------------------------


def _completed(
    returncode: int, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["gh"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def test_run_gh_with_retry_recovers_after_two_failures(monkeypatch: pytest.MonkeyPatch):
    calls: list[list[str]] = []
    results = [
        _completed(1, stderr="gh: rate limit exceeded"),
        _completed(1, stderr="gh: rate limit exceeded"),
        _completed(0, stdout='[{"name": "ok"}]'),
    ]

    def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return results[len(calls) - 1]

    sleeps: list[float] = []

    monkeypatch.setattr(gitio, "_run", fake_run)

    result = gitio._run_gh_with_retry(
        ["gh", "repo", "list", "someowner"],
        _sleep=lambda d: sleeps.append(d),
    )

    assert result.returncode == 0
    assert len(calls) == 3
    # Exponential back-off starting at 1.0s: 1s before attempt 2, 2s before attempt 3.
    assert sleeps == [1.0, 2.0]


def test_run_gh_with_retry_surfaces_failure_when_all_attempts_fail(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[list[str]] = []

    def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return _completed(1, stderr="gh: permanent auth failure")

    sleeps: list[float] = []

    monkeypatch.setattr(gitio, "_run", fake_run)

    result = gitio._run_gh_with_retry(
        ["gh", "repo", "list", "someowner"],
        _sleep=lambda d: sleeps.append(d),
    )

    assert result.returncode == 1, (
        "A genuinely-failing command must never be reported as success."
    )
    assert "permanent auth failure" in result.stderr
    assert len(calls) == 3  # default max_attempts
    assert sleeps == [1.0, 2.0]


def test_run_gh_with_retry_does_not_sleep_on_first_try_success(
    monkeypatch: pytest.MonkeyPatch,
):
    calls: list[list[str]] = []

    def fake_run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return _completed(0, stdout="[]")

    sleeps: list[float] = []
    monkeypatch.setattr(gitio, "_run", fake_run)

    result = gitio._run_gh_with_retry(
        ["gh", "repo", "list", "someowner"],
        _sleep=lambda d: sleeps.append(d),
    )

    assert result.returncode == 0
    assert len(calls) == 1
    assert sleeps == []


def test_gh_list_repos_filters_forks_client_side(monkeypatch: pytest.MonkeyPatch):
    def fake_run_gh_with_retry(
        cmd: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        return _completed(
            0,
            stdout=json.dumps(
                [
                    {"name": "a", "isFork": False, "nameWithOwner": "o/a"},
                    {"name": "b", "isFork": True, "nameWithOwner": "o/b"},
                ]
            ),
        )

    monkeypatch.setattr(gitio, "_run_gh_with_retry", fake_run_gh_with_retry)

    repos, error = gitio.gh_list_repos("o", include_forks=False)
    assert error is None
    assert [r["name"] for r in repos] == ["a"]

    repos_all, error_all = gitio.gh_list_repos("o", include_forks=True)
    assert error_all is None
    assert {r["name"] for r in repos_all} == {"a", "b"}


def test_gh_list_repos_surfaces_error_on_nonzero_exit(monkeypatch: pytest.MonkeyPatch):
    def fake_run_gh_with_retry(
        cmd: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        return _completed(1, stderr="gh: authentication failed")

    monkeypatch.setattr(gitio, "_run_gh_with_retry", fake_run_gh_with_retry)

    repos, error = gitio.gh_list_repos("o")
    assert repos == []
    assert error is not None
    assert "authentication failed" in error


# ---------------------------------------------------------------------------
# DISC -- gitio.discover_repos()
# ---------------------------------------------------------------------------


def test_discover_repos_applies_per_rule_include_forks_and_merges(
    monkeypatch: pytest.MonkeyPatch,
):
    def fake_gh_list_repos(
        owner: str, include_forks: bool = True, visibility: str = "all"
    ) -> tuple[list[dict[str, Any]], str | None]:
        if owner == "microsoft":
            # Org rule: include_forks=True -- both repos should surface.
            assert include_forks is True
            return [
                {
                    "name": "amplifier-foo",
                    "isFork": False,
                    "nameWithOwner": "microsoft/amplifier-foo",
                },
                {
                    "name": "amplifier-bar",
                    "isFork": True,
                    "nameWithOwner": "microsoft/amplifier-bar",
                },
                {
                    "name": "other-repo",
                    "isFork": False,
                    "nameWithOwner": "microsoft/other-repo",
                },
            ], None
        if owner == "someuser":
            # Personal rule: include_forks=False -- forks already filtered by
            # gh_list_repos itself (simulated here since we're faking that layer).
            assert include_forks is False
            return [
                {
                    "name": "amplifier-baz",
                    "isFork": False,
                    "nameWithOwner": "someuser/amplifier-baz",
                },
            ], None
        return [], None

    monkeypatch.setattr(gitio, "gh_list_repos", fake_gh_list_repos)

    rules: list[dict[str, object]] = [
        {
            "owner": "microsoft",
            "match": "amplifier*",
            "include_forks": True,
            "visibility": "all",
        },
        {
            "owner": "someuser",
            "match": "amplifier*",
            "include_forks": False,
            "visibility": "all",
        },
    ]

    matched, errors = gitio.discover_repos(rules)

    assert errors == []
    names = {r["nameWithOwner"] for r in matched}
    assert names == {
        "microsoft/amplifier-foo",
        "microsoft/amplifier-bar",
        "someuser/amplifier-baz",
    }
    # "other-repo" does not match the "amplifier*" glob -- excluded.
    assert "microsoft/other-repo" not in names


def test_discover_repos_deduplicates_by_name_with_owner(
    monkeypatch: pytest.MonkeyPatch,
):
    def fake_gh_list_repos(
        owner: str, include_forks: bool = True, visibility: str = "all"
    ) -> tuple[list[dict[str, Any]], str | None]:
        return [
            {
                "name": "amplifier-foo",
                "isFork": False,
                "nameWithOwner": "microsoft/amplifier-foo",
            },
        ], None

    monkeypatch.setattr(gitio, "gh_list_repos", fake_gh_list_repos)

    # Two rules that both resolve to the same owner/repo -- must not duplicate.
    rules: list[dict[str, object]] = [
        {"owner": "microsoft", "match": "amplifier*"},
        {"owner": "microsoft", "match": "amplifier-f*"},
    ]

    matched, errors = gitio.discover_repos(rules)
    assert errors == []
    assert len(matched) == 1


def test_discover_repos_failing_rule_does_not_abort_others(
    monkeypatch: pytest.MonkeyPatch,
):
    def fake_gh_list_repos(
        owner: str, include_forks: bool = True, visibility: str = "all"
    ) -> tuple[list[dict[str, Any]], str | None]:
        if owner == "brokenowner":
            return [], "gh error: authentication failed"
        return [
            {
                "name": "amplifier-ok",
                "isFork": False,
                "nameWithOwner": f"{owner}/amplifier-ok",
            },
        ], None

    monkeypatch.setattr(gitio, "gh_list_repos", fake_gh_list_repos)

    rules: list[dict[str, object]] = [
        {"owner": "brokenowner", "match": "amplifier*"},
        {"owner": "goodowner", "match": "amplifier*"},
    ]

    matched, errors = gitio.discover_repos(rules)

    assert len(errors) == 1
    assert "brokenowner" in errors[0]
    assert "authentication failed" in errors[0]
    # The good rule's repo still surfaces despite the other rule's failure.
    assert {r["nameWithOwner"] for r in matched} == {"goodowner/amplifier-ok"}


# ---------------------------------------------------------------------------
# DISC -- `repo-weaver discover` CLI
# ---------------------------------------------------------------------------


def test_cmd_discover_json_output_is_valid_json_with_expected_keys(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    rules_file = tmp_path / "rules.json"
    rules_file.write_text(
        json.dumps([{"owner": "microsoft", "match": "amplifier*"}]),
        encoding="utf-8",
    )

    fake_matched = [
        {"nameWithOwner": "microsoft/amplifier-foo", "pushedAt": "2026-07-01T00:00:00Z"}
    ]

    args = argparse.Namespace(rules_file=str(rules_file), json=True)

    with patch("repo_weaver.gitio.discover_repos", return_value=(fake_matched, [])):
        rc = cli.cmd_discover(args)

    assert rc == 0
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed["matched"] == fake_matched
    assert parsed["errors"] == []


def test_cmd_discover_json_exits_nonzero_when_errors_present(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    rules_file = tmp_path / "rules.json"
    rules_file.write_text(
        json.dumps([{"owner": "brokenowner", "match": "amplifier*"}]),
        encoding="utf-8",
    )

    args = argparse.Namespace(rules_file=str(rules_file), json=True)

    with patch(
        "repo_weaver.gitio.discover_repos",
        return_value=([], ["brokenowner: gh error: authentication failed"]),
    ):
        rc = cli.cmd_discover(args)

    assert rc == 1
    captured = capsys.readouterr()
    parsed = json.loads(captured.out)
    assert parsed["errors"] == ["brokenowner: gh error: authentication failed"]


def test_cmd_discover_reports_error_for_missing_rules_file(tmp_path: Path):
    args = argparse.Namespace(
        rules_file=str(tmp_path / "does-not-exist.json"), json=False
    )
    rc = cli.cmd_discover(args)
    assert rc == 1


def test_cmd_discover_reports_error_for_invalid_json(tmp_path: Path):
    rules_file = tmp_path / "rules.json"
    rules_file.write_text("not valid json {{{", encoding="utf-8")
    args = argparse.Namespace(rules_file=str(rules_file), json=False)
    rc = cli.cmd_discover(args)
    assert rc == 1


def test_cmd_discover_reports_error_when_rules_file_is_not_a_list(tmp_path: Path):
    rules_file = tmp_path / "rules.json"
    rules_file.write_text(json.dumps({"owner": "microsoft"}), encoding="utf-8")
    args = argparse.Namespace(rules_file=str(rules_file), json=False)
    rc = cli.cmd_discover(args)
    assert rc == 1
