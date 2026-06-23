"""Production-readiness tests covering all six design-council defects (A–D).

All tests are deterministic, fast, and require no network access.
External tools (git, gh, wiki-weaver) are faked via PATH injection.

Test matrix
-----------

C1  _classify_failure() explicit allowlist
    - permanent on auth error (401)
    - permanent on permission error
    - permanent on 404
    - "4294" does NOT match 429 transient
    - "429" isolated → transient
    - "503", "529" → transient
    - empty text → transient (safe first-try default)
    - cycle-cap text → not_converged
    - overloaded_error → transient
    - rate limit → transient

C2  Retry loop: stranded-in-inbox detection
    - wiki-weaver exits 0 but leaves source in _inbox/ → non-zero, not converged

C3  weave() / weave_multi(): invalid-repo guard
    - non-existent repo path → non-zero, no _inbox/ files

C4  gh_merged_prs(): error is NOT masked as empty list
    - fake gh exits non-zero → ([], error_str) returned, not ([], None)

C5  get_window_rev(): no HEAD fallback when until predates first commit
    - repo with commit dated today, until="2000-01-01" → None
    - repo with commit dated today, until="2099-01-01" → SHA returned
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

from repo_weaver.weave import (
    _DEFAULT_MAX_CYCLES,
    _classify_failure,
    _retry_failed_sources,
    weave,
    weave_multi,
)


# ---------------------------------------------------------------------------
# C1 — _classify_failure() explicit allowlist
# ---------------------------------------------------------------------------

_DUMMY_CORPUS = Path("/tmp")  # Ledger won't exist; only captured_output matters


def test_classify_permanent_on_401_auth():
    """Auth error text → permanent (do NOT retry)."""
    text = "ERROR: 401 Unauthorized — invalid or missing API key"
    assert _classify_failure("src.md", _DUMMY_CORPUS, text) == "permanent"


def test_classify_permanent_on_permission_error():
    """PermissionError text → permanent."""
    text = "PermissionError: [Errno 13] Permission denied: '/corpus/_inbox/src.md'"
    assert _classify_failure("src.md", _DUMMY_CORPUS, text) == "permanent"


def test_classify_permanent_on_404():
    """404 Not Found → permanent."""
    text = "ERROR: 404 Not Found — resource does not exist"
    assert _classify_failure("src.md", _DUMMY_CORPUS, text) == "permanent"


def test_classify_4294_is_not_429_transient():
    """The numeric string '4294' must NOT be misclassified as '429' transient."""
    text = "ERROR: code 4294 batch processing limit exceeded"
    result = _classify_failure("src.md", _DUMMY_CORPUS, text)
    assert result == "permanent", (
        f"'4294' should not match the '429' transient code boundary; got {result!r}"
    )


def test_classify_429_isolated_is_transient():
    """Standalone '429' (word-boundary) → transient."""
    text = "HTTP 429 Too Many Requests — rate limit hit"
    assert _classify_failure("src.md", _DUMMY_CORPUS, text) == "transient"


def test_classify_503_is_transient():
    """HTTP 503 → transient."""
    assert (
        _classify_failure("src.md", _DUMMY_CORPUS, "503 Service Unavailable")
        == "transient"
    )


def test_classify_529_is_transient():
    """HTTP 529 (Anthropic overloaded) → transient."""
    assert (
        _classify_failure("src.md", _DUMMY_CORPUS, "529 error overloaded")
        == "transient"
    )


def test_classify_empty_text_is_transient():
    """No diagnostic text yet → transient (safe first-retry default to gather info)."""
    assert _classify_failure("src.md", _DUMMY_CORPUS, captured_output="") == "transient"


def test_classify_cycle_cap_is_not_converged():
    """Cycle-cap text → not_converged."""
    text = "ERROR: cycle cap reached -- not converged after 4 cycles"
    assert _classify_failure("src.md", _DUMMY_CORPUS, text) == "not_converged"


def test_classify_overloaded_error_is_transient():
    """Anthropic overloaded_error → transient."""
    text = "ERROR: overloaded_error -- provider overloaded processing src.md"
    assert _classify_failure("src.md", _DUMMY_CORPUS, text) == "transient"


def test_classify_rate_limit_is_transient():
    """rate limit text → transient."""
    assert (
        _classify_failure(
            "src.md", _DUMMY_CORPUS, "rate limit exceeded, retry after 60s"
        )
        == "transient"
    )


# ---------------------------------------------------------------------------
# Shared fake wiki-weaver script (extends test_retry.py's variant with
# additional behaviors needed by C2 tests)
# ---------------------------------------------------------------------------

_FAKE_WW_SCRIPT = """\
#!/usr/bin/env python3
# Fake wiki-weaver for repo-weaver production-readiness tests.
# Extends the test_retry.py variant with a 'strandedxit0' behavior.
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
    max_cycles = 4
    i = 1
    while i < len(args):
        if args[i] == "--wiki" and i + 1 < len(args):
            wiki = args[i + 1]
            i += 2
        elif args[i] == "--source" and i + 1 < len(args):
            source = args[i + 1]
            i += 2
        elif args[i] == "--max-cycles" and i + 1 < len(args):
            max_cycles = int(args[i + 1])
            i += 2
        else:
            i += 1

    if not wiki:
        print("ERROR: --wiki required", file=sys.stderr)
        return 1

    corpus = Path(wiki)
    failed_dir = corpus / "_failed"
    inbox = corpus / "_inbox"
    archive_dir = corpus / "_archive"
    failed_dir.mkdir(exist_ok=True)
    archive_dir.mkdir(exist_ok=True)

    config_path = corpus / ".fake-ww-config.json"
    config = (
        json.loads(config_path.read_text(encoding="utf-8"))
        if config_path.exists()
        else {}
    )

    behavior = config.get("behavior", "succeed")
    fail_limit = int(config.get("fail_limit", 0))

    if source:
        src_path = inbox / source
        paths = [src_path] if src_path.exists() else []
    else:
        paths = sorted(inbox.glob("*.md"))

    call_log_path = corpus / ".fake-ww-calls.jsonl"

    for src_path in paths:
        name = src_path.name
        call_key = "calls:" + name
        call_num = int(config.get(call_key, 0)) + 1
        config[call_key] = call_num

        with open(call_log_path, "a", encoding="utf-8") as fh:
            fh.write(
                json.dumps({
                    "source": name,
                    "max_cycles": max_cycles,
                    "call_num": call_num,
                    "behavior": behavior,
                })
                + "\\n"
            )

        if behavior == "strandedxit0":
            # Exit 0 but do NOT move the source anywhere.
            # This simulates wiki-weaver crashing after starting but before
            # completing: source stays in _inbox/, not archived, not failed.
            pass

        elif behavior == "not_converged":
            print(
                f"ERROR: cycle cap reached -- max_cycles exceeded for {name}",
                file=sys.stderr,
            )
            if src_path.exists():
                src_path.rename(failed_dir / name)

        elif behavior == "transient" and call_num <= fail_limit:
            print(
                f"ERROR: overloaded_error -- provider overloaded processing {name}",
                file=sys.stderr,
            )
            if src_path.exists():
                src_path.rename(failed_dir / name)

        else:
            # Success path
            if src_path.exists():
                src_path.rename(archive_dir / name)
            pages_dir = corpus / "pages"
            pages_dir.mkdir(exist_ok=True)
            (pages_dir / (name + ".page.md")).write_text(
                f"# Page for {name}\\n", encoding="utf-8"
            )

    config_path.write_text(json.dumps(config), encoding="utf-8")
    return 0


sys.exit(main())
"""

_SOURCE_NAME = "2026-01-01-digest.md"


def _install_fake_ww(bin_dir: Path) -> None:
    script = bin_dir / "wiki-weaver"
    script.write_text(_FAKE_WW_SCRIPT, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _setup_corpus(
    tmp_path: Path,
    *,
    source_name: str = _SOURCE_NAME,
    ledger_entry: dict | None = None,
) -> Path:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    for sub in ("_failed", "_inbox", "_archive"):
        (corpus / sub).mkdir()
    (corpus / "_failed" / source_name).write_text(
        "# test source content\n", encoding="utf-8"
    )
    if ledger_entry is not None:
        (corpus / ".processed.jsonl").write_text(
            json.dumps(ledger_entry) + "\n", encoding="utf-8"
        )
    return corpus


def _configure_fake(corpus: Path, behavior: str, fail_limit: int = 0) -> None:
    (corpus / ".fake-ww-config.json").write_text(
        json.dumps({"behavior": behavior, "fail_limit": fail_limit}),
        encoding="utf-8",
    )


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
# C2 — stranded-in-inbox detection
# ---------------------------------------------------------------------------


def test_stranded_in_inbox_fails_loud(tmp_path, fake_ww_env):
    """wiki-weaver exits 0 but leaves source in _inbox/ → non-zero, never converged.

    Verifies:
    - _retry_failed_sources returns non-zero.
    - Source is NOT reported as converged.
    - Source ends up back in _failed/ (rescued from _inbox/).
    """
    corpus = _setup_corpus(tmp_path)
    _configure_fake(corpus, behavior="strandedxit0")

    rc = _retry_failed_sources(
        corpus=str(corpus),
        max_retries=1,
        max_cycles=_DEFAULT_MAX_CYCLES,
        retry_base_delay=0.0,
        _sleep=lambda _: None,
    )

    assert rc != 0, "Should return non-zero when source is stranded in _inbox/"
    # Source must NOT be in _archive/ (would falsely indicate convergence).
    assert not (corpus / "_archive" / _SOURCE_NAME).exists(), (
        "Stranded source must not be reported as archived/converged"
    )
    # Source should have been rescued back to _failed/ for the final report.
    assert (corpus / "_failed" / _SOURCE_NAME).exists(), (
        "Rescued source should be in _failed/ so the final summary counts it"
    )


def test_stranded_in_inbox_not_counted_as_success(tmp_path, fake_ww_env):
    """Even with max_retries > 1, a stranded source never becomes success."""
    corpus = _setup_corpus(tmp_path)
    _configure_fake(corpus, behavior="strandedxit0")

    rc = _retry_failed_sources(
        corpus=str(corpus),
        max_retries=2,
        max_cycles=_DEFAULT_MAX_CYCLES,
        retry_base_delay=0.0,
        _sleep=lambda _: None,
    )

    assert rc != 0
    assert not (corpus / "_archive" / _SOURCE_NAME).exists()


# ---------------------------------------------------------------------------
# C3 — invalid-repo guard in weave() / weave_multi()
# ---------------------------------------------------------------------------


def test_weave_invalid_repo_fails_loud(tmp_path):
    """weave() with a non-existent repo path → non-zero, no _inbox/ files written.

    Verifies:
    - Return code is non-zero.
    - No documents written to _inbox/ (phantom corpus prevention).
    """
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "_inbox").mkdir()

    fake_repo = str(tmp_path / "does-not-exist")

    rc = weave(
        corpus=str(corpus),
        repo=fake_repo,
        since="2026-01-01",
        until="2026-06-01",
        dry_run=True,  # skip wiki-weaver — we only test the guard
        _sleep=lambda _: None,
    )

    assert rc != 0, f"Expected non-zero for invalid repo, got {rc}"
    inbox_files = list((corpus / "_inbox").iterdir())
    assert inbox_files == [], (
        f"No files should be written to _inbox/ for an invalid repo: {inbox_files}"
    )


def test_weave_multi_invalid_repo_fails_loud(tmp_path):
    """weave_multi() with an invalid repo → non-zero, no _inbox/ files written."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "_inbox").mkdir()

    fake_repo_a = str(tmp_path / "fake-repo-a")
    fake_repo_b = str(tmp_path / "fake-repo-b")

    rc = weave_multi(
        corpus=str(corpus),
        repos=[fake_repo_a, fake_repo_b],
        since="2026-01-01",
        until="2026-06-01",
        dry_run=True,
        _sleep=lambda _: None,
    )

    assert rc != 0, f"Expected non-zero for invalid repos, got {rc}"
    inbox_files = list((corpus / "_inbox").iterdir())
    assert inbox_files == [], (
        f"No files should be written to _inbox/ for invalid repos: {inbox_files}"
    )


# ---------------------------------------------------------------------------
# C4 — gh_merged_prs() error is NOT masked as empty list
# ---------------------------------------------------------------------------

_FAKE_GH_AUTH_ERROR_SCRIPT = """\
#!/usr/bin/env python3
# Fake gh that exits non-zero simulating an auth failure.
import sys
args = sys.argv[1:]
if args and args[0] == "pr":
    print("error: you must be authenticated to use this command", file=sys.stderr)
    sys.exit(1)
sys.exit(0)
"""

_FAKE_GH_SUCCESS_EMPTY_SCRIPT = """\
#!/usr/bin/env python3
# Fake gh that exits 0 with an empty list (no PRs).
import sys
print("[]")
sys.exit(0)
"""


@pytest.fixture()
def fake_gh_auth_error_env(tmp_path):
    """Put a fake failing gh first on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "gh"
    script.write_text(_FAKE_GH_AUTH_ERROR_SCRIPT, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{bin_dir}{os.pathsep}{old_path}"
    yield bin_dir
    os.environ["PATH"] = old_path


@pytest.fixture()
def fake_gh_success_empty_env(tmp_path):
    """Put a fake successful-but-empty gh first on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "gh"
    script.write_text(_FAKE_GH_SUCCESS_EMPTY_SCRIPT, encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{bin_dir}{os.pathsep}{old_path}"
    yield bin_dir
    os.environ["PATH"] = old_path


def test_gh_merged_prs_error_not_masked(fake_gh_auth_error_env):
    """gh exits non-zero → ([], error_str) not ([], None).

    The caller MUST be able to distinguish a gh failure from genuine zero-PR
    activity so the change digest can surface the error rather than silently
    writing 'no merged PRs this window' when the real cause is auth failure.
    """
    from repo_weaver.gitio import gh_merged_prs

    prs, error = gh_merged_prs("owner/repo", "2026-01-01", "2026-06-01")

    assert prs == [], "No PRs should be returned when gh fails"
    assert error is not None, (
        "error must be a non-None string when gh exits non-zero — "
        "returning None here masks the failure as 'zero PRs'"
    )
    assert len(error) > 0, "Error string must be non-empty"


def test_gh_merged_prs_success_empty_returns_none_error(fake_gh_success_empty_env):
    """gh exits 0 with empty list → ([], None): genuine zero-PR window."""
    from repo_weaver.gitio import gh_merged_prs

    prs, error = gh_merged_prs("owner/repo", "2026-01-01", "2026-06-01")

    assert prs == []
    assert error is None, (
        "error must be None when gh succeeds — caller should show "
        "'no merged PRs' not a spurious error"
    )


def test_gh_error_appears_in_change_digest(tmp_path, fake_gh_auth_error_env):
    """gh failure → change-digest text contains 'gh error' not 'no merged PRs'."""
    # We need a real-looking git repo so materialize() doesn't error.
    # Minimal: just a directory with a .git — get_origin_url() returns None,
    # which skips the gh call. We need to inject a fake origin so gh IS called.
    # Easier: mock at the gitio level. But since we're testing the full
    # materialize() path with a fake origin, let's give a git repo with an
    # origin pointing to a fake GitHub URL.
    import subprocess as sp

    repo_dir = tmp_path / "testrepo"
    repo_dir.mkdir()
    sp.run(["git", "init", str(repo_dir)], capture_output=True)
    sp.run(
        [
            "git",
            "-C",
            str(repo_dir),
            "remote",
            "add",
            "origin",
            "https://github.com/example/test-repo.git",
        ],
        capture_output=True,
    )

    from repo_weaver.materialize import materialize

    # since/until window with no real commits → 0 commits; only gh call matters.
    docs = materialize(
        repo=str(repo_dir),
        since="2000-01-01",
        until="2000-12-31",
    )

    assert docs, "materialize() should still produce a change digest"
    change_digest_content = docs[0][1]  # first doc is always the digest

    # With a gh error, the digest must NOT say "no merged PRs" as if it's normal.
    # It must contain something indicating gh failed.
    assert "gh error" in change_digest_content.lower(), (
        "Change digest must surface the gh error, not silently show 'no merged PRs'. "
        f"Got digest snippet: {change_digest_content[:400]!r}"
    )


# ---------------------------------------------------------------------------
# C5 — get_window_rev(): no HEAD fallback when until predates first commit
# ---------------------------------------------------------------------------


def _init_git_repo(path: Path) -> str:
    """Create a minimal git repo with one commit and return its SHA."""
    path.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "t@test.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "t@test.com",
    }
    subprocess.run(["git", "init", str(path)], capture_output=True, env=env)
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


def test_get_window_rev_before_first_commit_returns_none(tmp_path):
    """until='2000-01-01' with a repo first-committed today → None, not HEAD.

    This prevents module snapshots from reflecting current-HEAD state as if
    it were the historical state at the 2000-01-01 window boundary.
    """
    from repo_weaver.gitio import get_window_rev

    repo_dir = tmp_path / "repo"
    head_sha = _init_git_repo(repo_dir)
    assert head_sha, "git init/commit should produce a HEAD SHA"

    result = get_window_rev(str(repo_dir), "2000-01-01")
    assert result is None, (
        f"Expected None when until='2000-01-01' predates the first commit, "
        f"but got {result!r} (= HEAD {head_sha[:8]}). "
        "This would cause module snapshots to reflect current HEAD state "
        "at a historical window where the repo didn't exist yet."
    )


def test_get_window_rev_future_date_returns_sha(tmp_path):
    """until='2099-01-01' (future) → returns HEAD SHA (no regression)."""
    from repo_weaver.gitio import get_window_rev

    repo_dir = tmp_path / "repo"
    head_sha = _init_git_repo(repo_dir)

    result = get_window_rev(str(repo_dir), "2099-01-01")
    assert result is not None, "Should return a SHA for a future until date"
    assert result == head_sha, (
        f"Expected HEAD SHA {head_sha[:8]}, got {(result or '')[:8]}"
    )


def test_get_window_rev_today_returns_sha(tmp_path):
    """until=today → returns the commit's SHA (normal use case)."""
    from repo_weaver.gitio import get_window_rev
    from datetime import date

    repo_dir = tmp_path / "repo"
    head_sha = _init_git_repo(repo_dir)

    today = date.today().isoformat()
    result = get_window_rev(str(repo_dir), today)
    assert result is not None, "Should return a SHA when until=today"
    assert result == head_sha


# ---------------------------------------------------------------------------
# A — packaging: policy/schema.md is inside the package
# ---------------------------------------------------------------------------


def test_policy_schema_is_inside_package():
    """repo_weaver/policy/schema.md must exist inside the package directory.

    This ensures the schema is included in wheel installs (uv tool install)
    where the top-level policy/ directory is not present.
    """
    from repo_weaver.cli import _POLICY_SCHEMA

    assert _POLICY_SCHEMA.exists(), (
        f"policy/schema.md not found at {_POLICY_SCHEMA}. "
        "It must be at repo_weaver/policy/schema.md for wheel installs to work."
    )
    content = _POLICY_SCHEMA.read_text(encoding="utf-8")
    assert "## Page types" in content, "schema.md appears to be empty or wrong file"


def test_policy_schema_not_only_at_project_root():
    """The package-internal schema path must NOT be the top-level policy/ directory."""
    from repo_weaver.cli import _POLICY_SCHEMA
    import repo_weaver

    pkg_dir = Path(repo_weaver.__file__).parent
    assert _POLICY_SCHEMA.is_relative_to(pkg_dir), (
        f"_POLICY_SCHEMA ({_POLICY_SCHEMA}) must be inside the repo_weaver package "
        f"directory ({pkg_dir}), not at the project root."
    )
