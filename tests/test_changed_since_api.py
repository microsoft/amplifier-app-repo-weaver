"""Tests for the standalone ``changed_since()`` public API and its supporting
``gitio.gh_repo_pushed_at()`` helper -- the extraction of ``sync_corpus()``'s
inline push/PR/issue union-check into a stable, corpus-agnostic query.

Test matrix
-----------
PUSHED1  gh_repo_pushed_at() invokes the correct `gh repo view` command and
         parses the ISO 8601 ``pushedAt`` field.
PUSHED2  gh_repo_pushed_at() surfaces a genuine gh failure as (None, error).
PUSHED3  gh_repo_pushed_at() treats an empty response as an error (NOT a
         legitimate "zero" case -- a named repo either exists or gh failed).
PUSHED4  gh_repo_pushed_at() surfaces a response missing the ``pushedAt``
         field as an error rather than silently returning None.

CHANGED1 changed_since() reports changed=True with reasons=["push activity"]
         when only the push signal crosses `since`.
CHANGED2 changed_since() reports changed=True with reasons=["PR activity"]
         when only the PR signal crosses `since`.
CHANGED3 changed_since() reports changed=True with reasons=["issue activity"]
         when only the issue signal crosses `since`.
CHANGED4 changed_since() reports changed=False, reasons=[] when no signal
         crosses `since`.
CHANGED5 changed_since() reports both reasons when push AND issue activity
         both cross `since` (order: push, PR, issue).
CHANGED6 changed_since() still returns a decision based on the signals that
         DID succeed when one gh call fails; the failure is recorded in
         `errors` but does not abort the decision.
CHANGED7 changed_since() is a pure query -- makes no reference to corpus
         path, watermark files, or any repo-weaver-owned state.

DECISION1 _resolve_change_decision() is the single shared helper: verified
          directly for all reason combinations and edge cases (falsy dates).

REGRESSION1 sync_corpus()'s CHANGED-selection loop still reuses its own
            bulk-fetched `pushed_date` (no redundant gh_repo_pushed_at() call
            per tracked repo) while routing its union decision through the
            same `_resolve_change_decision()` helper `changed_since()` uses.

PUBLIC1  `changed_since` and `ChangeSignal` are part of the public
         `repo_weaver` package API (`__all__`, importable, correct shape).
"""

from __future__ import annotations

import json
import subprocess
from typing import Any

import pytest

import repo_weaver
from repo_weaver import gitio, sync
from repo_weaver.sync import ChangeSignal, _resolve_change_decision, changed_since

# ---------------------------------------------------------------------------
# Shared helper
# ---------------------------------------------------------------------------


def _completed(
    returncode: int, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["gh"], returncode=returncode, stdout=stdout, stderr=stderr
    )


# ---------------------------------------------------------------------------
# PUSHED -- gitio.gh_repo_pushed_at
# ---------------------------------------------------------------------------


def test_gh_repo_pushed_at_invokes_correct_command_and_parses_date(
    monkeypatch: pytest.MonkeyPatch,
):
    captured_cmd: list[str] = []

    def fake_run_gh_with_retry(
        cmd: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        captured_cmd.extend(cmd)
        return _completed(0, stdout=json.dumps({"pushedAt": "2026-07-08T14:03:34Z"}))

    monkeypatch.setattr(gitio, "_run_gh_with_retry", fake_run_gh_with_retry)

    pushed_at, error = gitio.gh_repo_pushed_at("o/r")

    assert error is None
    assert pushed_at == "2026-07-08T14:03:34Z"
    assert captured_cmd == ["gh", "repo", "view", "o/r", "--json", "pushedAt"]


def test_gh_repo_pushed_at_surfaces_genuine_gh_failure(
    monkeypatch: pytest.MonkeyPatch,
):
    def fake_run_gh_with_retry(
        cmd: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        return _completed(1, stderr="gh: repository not found")

    monkeypatch.setattr(gitio, "_run_gh_with_retry", fake_run_gh_with_retry)

    pushed_at, error = gitio.gh_repo_pushed_at("o/r")
    assert pushed_at is None
    assert error is not None
    assert "repository not found" in error


def test_gh_repo_pushed_at_empty_response_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
):
    """Unlike list-style gh calls, an empty response for a single named repo
    is NOT a legitimate "zero results" case -- it must be treated as a
    failure so callers don't silently mistake it for "never pushed"."""

    def fake_run_gh_with_retry(
        cmd: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        return _completed(0, stdout="")

    monkeypatch.setattr(gitio, "_run_gh_with_retry", fake_run_gh_with_retry)

    pushed_at, error = gitio.gh_repo_pushed_at("o/r")
    assert pushed_at is None
    assert error is not None


def test_gh_repo_pushed_at_missing_field_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
):
    def fake_run_gh_with_retry(
        cmd: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        return _completed(0, stdout=json.dumps({}))

    monkeypatch.setattr(gitio, "_run_gh_with_retry", fake_run_gh_with_retry)

    pushed_at, error = gitio.gh_repo_pushed_at("o/r")
    assert pushed_at is None
    assert error is not None
    assert "pushedAt" in error


# ---------------------------------------------------------------------------
# DECISION -- sync._resolve_change_decision (shared pure helper)
# ---------------------------------------------------------------------------


def test_resolve_change_decision_all_falsy_is_unchanged():
    changed, reasons = _resolve_change_decision("2026-06-01", None, None, None)
    assert changed is False
    assert reasons == []


def test_resolve_change_decision_dates_not_after_since_is_unchanged():
    changed, reasons = _resolve_change_decision(
        "2026-06-10", "2026-06-01", "2026-06-05", "2026-06-10"
    )
    assert changed is False
    assert reasons == []


def test_resolve_change_decision_reason_order_is_push_pr_issue():
    changed, reasons = _resolve_change_decision(
        "2026-06-01", "2026-06-10", "2026-06-10", "2026-06-10"
    )
    assert changed is True
    assert reasons == ["push activity", "PR activity", "issue activity"]


# ---------------------------------------------------------------------------
# CHANGED -- sync.changed_since (public API)
# ---------------------------------------------------------------------------


def _patch_signals(
    monkeypatch: pytest.MonkeyPatch,
    pushed_at: str | None,
    pushed_err: str | None,
    pr_updated: str | None,
    pr_err: str | None,
    issue_updated: str | None,
    issue_err: str | None,
) -> None:
    monkeypatch.setattr(
        gitio, "gh_repo_pushed_at", lambda owner_repo: (pushed_at, pushed_err)
    )

    def fake_gh_most_recent_update(
        owner_repo: str, kind: str
    ) -> tuple[str | None, str | None]:
        if kind == "pr":
            return pr_updated, pr_err
        return issue_updated, issue_err

    monkeypatch.setattr(gitio, "gh_most_recent_update", fake_gh_most_recent_update)


def test_changed_since_push_only(monkeypatch: pytest.MonkeyPatch):
    _patch_signals(
        monkeypatch,
        pushed_at="2026-07-01T00:00:00Z",
        pushed_err=None,
        pr_updated=None,
        pr_err=None,
        issue_updated=None,
        issue_err=None,
    )

    signal = changed_since("o/r", since="2026-06-01")

    assert isinstance(signal, ChangeSignal)
    assert signal.changed is True
    assert signal.reasons == ["push activity"]
    assert signal.pushed_at == "2026-07-01T00:00:00Z"
    assert signal.pr_updated_at is None
    assert signal.issue_updated_at is None
    assert signal.errors == []


def test_changed_since_pr_only(monkeypatch: pytest.MonkeyPatch):
    _patch_signals(
        monkeypatch,
        pushed_at="2026-05-01T00:00:00Z",  # before `since` -- no push signal
        pushed_err=None,
        pr_updated="2026-07-01",
        pr_err=None,
        issue_updated=None,
        issue_err=None,
    )

    signal = changed_since("o/r", since="2026-06-01")

    assert signal.changed is True
    assert signal.reasons == ["PR activity"]
    assert signal.pr_updated_at == "2026-07-01"


def test_changed_since_issue_only(monkeypatch: pytest.MonkeyPatch):
    _patch_signals(
        monkeypatch,
        pushed_at="2026-05-01T00:00:00Z",
        pushed_err=None,
        pr_updated=None,
        pr_err=None,
        issue_updated="2026-07-01",
        issue_err=None,
    )

    signal = changed_since("o/r", since="2026-06-01")

    assert signal.changed is True
    assert signal.reasons == ["issue activity"]
    assert signal.issue_updated_at == "2026-07-01"


def test_changed_since_no_activity_is_unchanged(monkeypatch: pytest.MonkeyPatch):
    _patch_signals(
        monkeypatch,
        pushed_at="2026-05-01T00:00:00Z",
        pushed_err=None,
        pr_updated="2026-05-15",
        pr_err=None,
        issue_updated="2026-05-20",
        issue_err=None,
    )

    signal = changed_since("o/r", since="2026-06-01")

    assert signal.changed is False
    assert signal.reasons == []
    assert signal.errors == []


def test_changed_since_mixed_signals_report_all_firing_reasons(
    monkeypatch: pytest.MonkeyPatch,
):
    _patch_signals(
        monkeypatch,
        pushed_at="2026-07-01T00:00:00Z",
        pushed_err=None,
        pr_updated="2026-05-01",  # does not cross `since`
        pr_err=None,
        issue_updated="2026-07-05",
        issue_err=None,
    )

    signal = changed_since("o/r", since="2026-06-01")

    assert signal.changed is True
    assert signal.reasons == ["push activity", "issue activity"]


def test_changed_since_partial_gh_failure_still_decides_from_succeeding_signals(
    monkeypatch: pytest.MonkeyPatch,
):
    """One gh call (push) fails; the PR signal still succeeds and fires.
    The decision must reflect the signals that DID succeed, and the failure
    must be recorded loudly in `errors` -- never silently swallowed."""
    _patch_signals(
        monkeypatch,
        pushed_at=None,
        pushed_err="gh error: rate limit exceeded",
        pr_updated="2026-07-01",
        pr_err=None,
        issue_updated=None,
        issue_err=None,
    )

    signal = changed_since("o/r", since="2026-06-01")

    assert signal.changed is True
    assert signal.reasons == ["PR activity"]
    assert len(signal.errors) == 1
    assert "rate limit exceeded" in signal.errors[0]
    assert "o/r" in signal.errors[0]


def test_changed_since_all_gh_calls_fail_reports_unchanged_with_errors(
    monkeypatch: pytest.MonkeyPatch,
):
    _patch_signals(
        monkeypatch,
        pushed_at=None,
        pushed_err="gh error: auth failed",
        pr_updated=None,
        pr_err="gh error: auth failed",
        issue_updated=None,
        issue_err="gh error: auth failed",
    )

    signal = changed_since("o/r", since="2026-06-01")

    assert signal.changed is False
    assert signal.reasons == []
    assert len(signal.errors) == 3


def test_changed_since_is_a_pure_query_no_filesystem_or_corpus_access(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
):
    """No corpus/watermark coupling: changed_since() must not touch the
    filesystem at all (beyond whatever gh itself may do, which is mocked
    out here entirely)."""
    _patch_signals(
        monkeypatch,
        pushed_at="2026-07-01T00:00:00Z",
        pushed_err=None,
        pr_updated=None,
        pr_err=None,
        issue_updated=None,
        issue_err=None,
    )

    # Sanity: changed_since() takes no corpus/path argument at all -- this
    # is a signature-level guarantee, not just a runtime behavior check.
    import inspect

    sig = inspect.signature(changed_since)
    assert set(sig.parameters) == {"owner_repo", "since"}

    signal = changed_since("o/r", since="2026-06-01")
    assert signal.changed is True


# ---------------------------------------------------------------------------
# REGRESSION -- sync_corpus() still reuses its own bulk pushed_date and
# routes through the same shared decision helper (no duplicated logic, no
# redundant gh_repo_pushed_at() call per tracked repo).
# ---------------------------------------------------------------------------


def test_sync_corpus_never_calls_gh_repo_pushed_at(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
):
    """sync_corpus() has its own bulk-fetched pushedAt (from gh_list_repos)
    for every tracked repo already -- it must never call the single-repo
    gh_repo_pushed_at(), which would be a redundant `gh` call per repo."""
    sources = tmp_path / "_sources"
    sources.mkdir(parents=True)
    (sources / "o__r-2026-06-01-changes.md").write_text("x", encoding="utf-8")

    def fake_gh_list_repos(owner: str, **kwargs: Any):
        return [
            {
                "name": "r",
                "isFork": False,
                "nameWithOwner": "o/r",
                "pushedAt": "2026-07-01T00:00:00Z",
            }
        ], None

    calls: list[str] = []

    def fake_gh_repo_pushed_at(owner_repo: str):
        calls.append(owner_repo)
        return "SHOULD-NOT-BE-CALLED", None

    monkeypatch.setattr(gitio, "gh_list_repos", fake_gh_list_repos)
    monkeypatch.setattr(gitio, "gh_repo_pushed_at", fake_gh_repo_pushed_at)
    monkeypatch.setattr(
        gitio, "gh_most_recent_update", lambda owner_repo, kind: (None, None)
    )

    result = sync.sync_corpus(
        corpus=str(tmp_path), clones_dir=str(tmp_path / "clones"), dry_run=True
    )

    assert calls == [], "sync_corpus() must reuse its bulk pushedAt, not re-fetch it"
    assert len(result["changed"]) == 1
    assert result["changed"][0]["nameWithOwner"] == "o/r"


# ---------------------------------------------------------------------------
# PUBLIC -- repo_weaver.changed_since / repo_weaver.ChangeSignal public API
# ---------------------------------------------------------------------------


def test_changed_since_and_change_signal_are_public():
    assert hasattr(repo_weaver, "changed_since")
    assert callable(repo_weaver.changed_since)
    assert hasattr(repo_weaver, "ChangeSignal")
    assert "changed_since" in repo_weaver.__all__
    assert "ChangeSignal" in repo_weaver.__all__


def test_change_signal_has_expected_fields():
    signal = ChangeSignal(changed=False)
    assert signal.reasons == []
    assert signal.pushed_at is None
    assert signal.pr_updated_at is None
    assert signal.issue_updated_at is None
    assert signal.errors == []
