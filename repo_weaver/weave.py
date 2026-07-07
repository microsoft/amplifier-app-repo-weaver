"""Orchestrate: materialise source docs → _inbox → wiki-weaver ingest.

The only side effects are:
  1. Writing markdown files into ``<corpus>/_inbox/`` (idempotent).
  2. Spawning ``wiki-weaver ingest`` (only when ``dry_run=False``).

The target git repo is never mutated.

**Multi-repo usage** — use ``weave_multi()`` when a corpus spans several repos.
``weave()`` remains the single-repo primitive; ``weave_multi()`` with one repo
delegates to it so single-repo behaviour is bit-for-bit identical.

**Resilience** — after every ``wiki-weaver ingest`` call the ``.wiki/failed/``
directory is inspected and any stranded sources are automatically retried:

* **TRANSIENT** (overloaded / api / timeout / named HTTP 4xx-5xx codes) →
  exponential back-off, same cycle budget.  On the final attempt the cycle
  budget is also bumped as a belt-and-suspenders measure.
* **NOT-CONVERGED** (cycle cap hit) → no back-off; ``--max-cycles`` is
  increased by ``_DEFAULT_CYCLES_BUMP`` on each attempt.
* **PERMANENT** (auth errors, permission denied, 404, or any unrecognised
  error text) → NOT retried; fail loud immediately.
* **UNKNOWN** (no diagnostic text yet) → treated as transient for the first
  attempt so we can gather more information from the per-source run.

If a source is still in ``.wiki/failed/`` after all retries a loud named summary
is printed to stderr and the exit code is non-zero.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

from wiki_weaver.lib import (
    wiki_failed,
    wiki_inbox,
    wiki_ledger,
    wiki_sources,
)

from . import gitio
from .materialize import materialize as _materialize

# ---------------------------------------------------------------------------
# Corpus configuration helpers
# ---------------------------------------------------------------------------

# Policy schema shipped with repo-weaver.  Stored inside the package at
# repo_weaver/policy/schema.md so it is included in both editable installs
# and wheel installs (uv tool install) without any extra configuration.
_POLICY_SCHEMA: Path = Path(__file__).parent / "policy" / "schema.md"

# Filename stored inside each corpus to record the list of registered repos.
_CORPUS_CONFIG: str = ".repo-weaver.json"


def _load_corpus_config(corpus: str) -> dict[str, object]:
    """Load the corpus config from ``<corpus>/.repo-weaver.json``.

    Returns an empty dict when the file does not exist or is unreadable —
    callers should treat an empty dict as "no config yet".
    """
    cfg_path = Path(corpus) / _CORPUS_CONFIG
    if cfg_path.exists():
        try:
            return json.loads(cfg_path.read_text(encoding="utf-8"))  # type: ignore[return-value]
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_corpus_config(corpus: str, cfg: dict[str, object]) -> None:
    """Write corpus config to ``<corpus>/.repo-weaver.json``."""
    cfg_path = Path(corpus) / _CORPUS_CONFIG
    cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Clone-staleness guard  (Change 4)
# ---------------------------------------------------------------------------


def _ensure_fresh_clone(repo: str, no_fetch: bool = False) -> None:
    """Warn (and optionally fast-forward) when a local clone is behind origin.

    When ``no_fetch=False`` (default):

    1. ``git fetch origin`` is run to update remote refs.
    2. If the local clone is behind ``origin/<default_branch>`` the user
       is warned with the repo name and the commit-count delta.
    3. When the working tree is clean a ``git merge --ff-only`` is attempted
       so subsequent materialisation runs on fresh state.  On success a
       confirmation line is printed.
    4. If the tree is dirty or the fast-forward fails, a loud warning is
       printed and processing continues with the stale local state — the
       empty-window silent-wrong-result is avoided.

    When ``no_fetch=True``:
       Skips the network check entirely.  Use for offline/repeatable runs.

    Never raises — all git failures are printed as warnings.
    """
    if no_fetch:
        return

    repo_name = Path(repo).name
    origin_url = gitio.get_origin_url(repo)
    if not origin_url:
        # No remote configured — nothing to check.
        return

    fetch_ok = gitio.fetch_origin(repo)
    if not fetch_ok:
        print(
            f"[repo-weaver] WARNING: git fetch failed for {repo_name!r} — "
            "proceeding with local state (may be stale).",
            file=sys.stderr,
        )
        return

    default_branch = gitio.get_default_branch(repo)
    behind = gitio.commits_behind_origin(repo, default_branch)

    if behind == 0:
        return  # Up to date; no action needed.

    print(
        f"[repo-weaver] WARNING: {repo_name!r} is {behind} commit(s) behind "
        f"origin/{default_branch} — windows may be incomplete.",
        file=sys.stderr,
    )

    if gitio.is_working_tree_clean(repo):
        print(
            f"[repo-weaver] Attempting fast-forward: "
            f"git merge --ff-only origin/{default_branch}"
        )
        if gitio.fast_forward_origin(repo, default_branch):
            print(
                f"[repo-weaver] Fast-forward succeeded — proceeding on fresh state "
                f"({behind} new commit(s) applied)."
            )
        else:
            print(
                f"[repo-weaver] WARNING: fast-forward failed for {repo_name!r} — "
                "proceeding with stale local state.",
                file=sys.stderr,
            )
    else:
        print(
            f"[repo-weaver] WARNING: working tree of {repo_name!r} is dirty — "
            "cannot fast-forward.  Proceeding with stale local state.",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# Resume-from-checkpoint: progress tracking for replay_windows()
# ---------------------------------------------------------------------------

#: Progress file stored inside the corpus alongside ``.repo-weaver.json``.
#: Safe to delete: deletion is equivalent to ``--restart`` (redo from scratch).
_REPLAY_PROGRESS_FILE = ".replay-progress.json"


def _window_key(since: str, until: str) -> str:
    """Return a stable, human-readable key for a ``(since, until)`` pair."""
    return f"{since}->{until}"


def _load_replay_progress(corpus_path: Path) -> set[str]:
    """Load the set of completed window keys from the progress file.

    Returns an empty set when the file does not exist or is unreadable.
    A malformed / unreadable file is treated as "no progress" so the run
    restarts cleanly rather than crashing on a bad JSON file.
    """
    prog_file = corpus_path / _REPLAY_PROGRESS_FILE
    if not prog_file.exists():
        return set()
    try:
        data = json.loads(prog_file.read_text(encoding="utf-8"))
        completed = data.get("completed_windows", [])
        if isinstance(completed, list):
            return {str(k) for k in completed}
    except (json.JSONDecodeError, OSError):
        pass
    return set()


def _save_replay_progress(corpus_path: Path, completed: set[str]) -> None:
    """Atomically write the replay progress file.

    Uses write-to-tmp + ``Path.replace()`` so the file is never left in a
    half-written state.  The progress file is safe to delete at any time:
    deletion is equivalent to ``--restart`` (forces a full redo).
    """
    data: dict[str, object] = {
        "version": 1,
        "completed_windows": sorted(completed),
    }
    tmp = corpus_path / (_REPLAY_PROGRESS_FILE + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(corpus_path / _REPLAY_PROGRESS_FILE)  # atomic on POSIX


# ---------------------------------------------------------------------------
# Retry / resilience configuration
# ---------------------------------------------------------------------------

#: Default max digest cycles sent to ``wiki-weaver ingest``.
#: Raised from wiki-weaver's built-in default of 3 to handle active repos.
_DEFAULT_MAX_CYCLES: int = 4

#: Default number of per-source retry attempts after an initial ingest failure.
_DEFAULT_MAX_RETRIES: int = 3

#: Base back-off delay (seconds) for transient errors (doubles each attempt).
_DEFAULT_RETRY_BASE_DELAY: float = 5.0

#: Extra cycles added per retry attempt for NOT-CONVERGED sources.
_DEFAULT_CYCLES_BUMP: int = 2

# ---------------------------------------------------------------------------
# Failure classification — EXPLICIT ALLOWLIST
#
# Only the patterns below are considered transient (safe to retry).
# Anything else → 'permanent' → DO NOT retry; fail loud immediately.
# ---------------------------------------------------------------------------

# Substrings (lower-cased) that definitively indicate a *transient* provider
# error.  These are long enough to be unambiguous without word-boundary checks.
_TRANSIENT_TEXT_MARKERS: tuple[str, ...] = (
    "overloaded_error",  # Anthropic/provider overloaded error type
    "overloaded",  # provider overloaded message text
    "internal server error",
    "api_error",
    "rate limit",
    "timeout",
)

# HTTP status codes that indicate transient server-side conditions.
# Uses word-boundary matching so "4294" does NOT match "429", etc.
_TRANSIENT_CODE_RE: re.Pattern[str] = re.compile(r"\b(429|500|503|504|529)\b")

# Substrings (lower-cased) that indicate a *convergence* failure.
_CONVERGE_MARKERS: tuple[str, ...] = (
    "cycle cap",
    "not converged",
    "did not converge",
    "cycles exceeded",
    "cycles exhausted",
    "failed to converge",
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _tee_subprocess(cmd: list[str]) -> tuple[int, str]:
    """Run *cmd*, echo output to the terminal, return ``(returncode, combined_output)``.

    Used for *per-source* retry calls where we need the output text to
    classify the next failure.  The initial full-corpus ingest uses
    :func:`subprocess.run` with inherited file descriptors so progress
    streams in real time.
    """
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.stdout:
        sys.stdout.write(result.stdout)
        sys.stdout.flush()
    if result.stderr:
        sys.stderr.write(result.stderr)
        sys.stderr.flush()
    return result.returncode, result.stdout + result.stderr


def _read_ledger_for_source(corpus_path: Path, source_name: str) -> str:
    """Return raw text of all ``.wiki/.processed.jsonl`` rows that mention *source_name*."""
    ledger_path = wiki_ledger(corpus_path)
    if not ledger_path.exists():
        return ""
    try:
        text = ledger_path.read_text(encoding="utf-8")
    except OSError:
        return ""

    parts: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            row = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if row.get("source") == source_name or row.get("filename") == source_name:
            parts.append(stripped)
    return "\n".join(parts)


def _classify_failure(
    source_name: str,
    corpus_path: Path,
    captured_output: str = "",
) -> str:
    """Classify a ``.wiki/failed/`` source as ``'not_converged'``, ``'transient'``, or ``'permanent'``.

    Uses an **explicit allowlist**: only returns ``'transient'`` for named,
    tested markers; returns ``'permanent'`` when there is diagnostic text that
    does not match any known transient/convergence pattern.  This prevents
    unknown errors from being silently retried and hiding root causes.

    When *no* diagnostic text is available (empty captured output and no
    ledger entry), returns ``'transient'`` as a safe one-shot default so the
    first per-source retry can gather real diagnostic output.

    Checks *captured_output* first (from the most-recent per-source ingest
    call), then falls back to the ``.wiki/.processed.jsonl`` ledger.

    Classification priority:
    1. ``'not_converged'`` — cycle-cap markers present (different remedy).
    2. ``'transient'``     — explicit named transient markers present.
    3. ``'transient'``     — no diagnostic text yet (first-run safe default).
    4. ``'permanent'``     — text is present but matches no known pattern.
    """
    text = (
        (captured_output + "\n" + _read_ledger_for_source(corpus_path, source_name))
        .lower()
        .strip()
    )

    # With no text we cannot classify — treat as transient to gather info.
    if not text:
        return "transient"

    # Convergence markers take priority — different remedy than transient.
    if any(m in text for m in _CONVERGE_MARKERS):
        return "not_converged"

    # Explicit transient text markers (long enough to be unambiguous).
    if any(m in text for m in _TRANSIENT_TEXT_MARKERS):
        return "transient"

    # Numeric HTTP codes with word-boundary matching (avoids "4294" → "429").
    if _TRANSIENT_CODE_RE.search(text):
        return "transient"

    # Diagnostic text present but no known transient/convergence pattern →
    # treat as permanent: do NOT retry, fail loud.
    return "permanent"


def _retry_failed_sources(
    corpus: str,
    max_retries: int = _DEFAULT_MAX_RETRIES,
    max_cycles: int = _DEFAULT_MAX_CYCLES,
    retry_base_delay: float = _DEFAULT_RETRY_BASE_DELAY,
    cycles_bump: int = _DEFAULT_CYCLES_BUMP,
    _sleep: Optional[Callable[[float], None]] = None,
) -> int:
    """Retry any sources in ``<corpus>/.wiki/failed/`` up to *max_retries* times.

    Tactic per failure class:

    * **TRANSIENT** — exponential back-off starting at *retry_base_delay*
      seconds (doubles each attempt).  Same cycle budget.  On the last
      attempt cycles are also bumped as belt-and-suspenders.
    * **NOT-CONVERGED** — no back-off; ``--max-cycles`` grows by
      *cycles_bump* on each attempt.
    * **PERMANENT** — not retried; left in ``.wiki/failed/`` and reported loudly.
    * **UNKNOWN** (no text yet) — treated as transient for one attempt.

    **Stranded-in-inbox detection**: after each per-source ingest attempt the
    source must be in ``_sources/`` (success) or back in ``.wiki/failed/``
    (failure).  If it is found in neither location (e.g. wiki-weaver crashed
    mid-run leaving it in ``_inbox/``), it is rescued back to ``.wiki/failed/`` and
    classified as permanent — no further retries.

    Each retry emits a progress line: source, attempt N/M, reason,
    back-off seconds, cycle budget.

    Returns 0 if every source eventually leaves ``.wiki/failed/``, 1 otherwise.
    Exhausted sources are left in ``.wiki/failed/`` and a named summary is
    printed to stderr — **no silent fallbacks**.
    """
    if _sleep is None:
        _sleep = time.sleep

    corpus_path = Path(corpus)
    failed_dir = wiki_failed(corpus_path)
    inbox = wiki_inbox(corpus_path)
    archive_dir = wiki_sources(corpus_path)
    inbox.mkdir(parents=True, exist_ok=True)

    if not failed_dir.exists():
        return 0

    initial_failed = sorted(p for p in failed_dir.iterdir() if p.is_file())
    if not initial_failed:
        return 0

    # Per-source state: name → {"attempts": int, "last_reason": str}
    source_state: dict[str, dict[str, Any]] = {}
    for fp in initial_failed:
        name = fp.name
        # Classify from ledger (no per-source captured output from initial full ingest).
        reason = _classify_failure(name, corpus_path, captured_output="")
        source_state[name] = {"attempts": 0, "last_reason": reason}

    for attempt in range(1, max_retries + 1):
        still_failing = (
            sorted(p for p in failed_dir.iterdir() if p.is_file())
            if failed_dir.exists()
            else []
        )
        if not still_failing:
            break

        for failed_file in still_failing:
            source_name = failed_file.name
            state = source_state.setdefault(
                source_name, {"attempts": 0, "last_reason": "transient"}
            )
            reason = str(state["last_reason"])

            # Permanent failures: skip immediately, leave in .wiki/failed/.
            if reason == "permanent":
                print(
                    f"[repo-weaver] SKIP  source={source_name!r}  "
                    f"attempt={attempt}/{max_retries}  reason=permanent (not retrying)",
                )
                continue

            state["attempts"] = state["attempts"] + 1

            # Compute delay and cycle budget for this attempt.
            if reason == "not_converged":
                delay = 0.0
                cycles = max_cycles + (attempt - 1) * cycles_bump
            else:
                # transient or initial unknown
                delay = retry_base_delay * (2 ** (attempt - 1))
                cycles = max_cycles
                # Last attempt: also bump cycles (belt-and-suspenders).
                if attempt == max_retries:
                    cycles += cycles_bump

            print(
                f"[repo-weaver] RETRY  source={source_name!r}  "
                f"attempt={attempt}/{max_retries}  "
                f"reason={reason}  "
                f"backoff={delay:.1f}s  "
                f"max-cycles={cycles}"
            )

            if delay > 0:
                _sleep(delay)

            # Move source: .wiki/failed/ → _inbox/ before calling ingest.
            inbox_dest = inbox / source_name
            failed_file.rename(inbox_dest)

            # Per-source ingest with appropriate cycle budget.
            cmd = [
                "wiki-weaver",
                "ingest",
                "--wiki",
                corpus,
                "--source",
                source_name,
                "--max-cycles",
                str(cycles),
            ]
            print(f"[repo-weaver] Running: {' '.join(cmd)}")
            _rc, output = _tee_subprocess(cmd)

            # ---- Determine outcome ----
            in_failed = (failed_dir / source_name).exists()
            in_archive = archive_dir.exists() and (archive_dir / source_name).exists()

            if in_failed:
                # Still in .wiki/failed/: reclassify for the next round.
                new_reason = _classify_failure(source_name, corpus_path, output)
                state["last_reason"] = new_reason
                if new_reason == "permanent":
                    print(
                        f"\n[repo-weaver] PERMANENT FAILURE  source={source_name!r}: "
                        f"{output[:300].strip()}",
                        file=sys.stderr,
                    )
            elif in_archive:
                # Moved to _sources/ → genuine success.
                print(
                    f"[repo-weaver] OK  source={source_name!r} "
                    f"converged on attempt {attempt}."
                )
                state["last_reason"] = "success"
            else:
                # Source is in neither .wiki/failed/ nor _sources/.
                # Likely stranded in _inbox/ (wiki-weaver crashed mid-run).
                in_inbox = (inbox / source_name).exists()
                loc = (
                    "_inbox/ (wiki-weaver may have crashed mid-run)"
                    if in_inbox
                    else "unknown location"
                )
                print(
                    f"\n[repo-weaver] ERROR: source={source_name!r} stranded in {loc} "
                    f"after wiki-weaver exit — not archived, not failed. "
                    "Treating as permanent failure.",
                    file=sys.stderr,
                )
                # Rescue: move back to .wiki/failed/ so the final report counts it.
                if in_inbox:
                    (inbox / source_name).rename(failed_dir / source_name)
                else:
                    # Create a sentinel entry so the final loop catches it.
                    (failed_dir / source_name).write_text(
                        "# sentinel: stranded after wiki-weaver exit\n",
                        encoding="utf-8",
                    )
                state["last_reason"] = "permanent"

    # ---- Final report ----
    still_failed = (
        sorted(p.name for p in failed_dir.iterdir() if p.is_file())
        if failed_dir.exists()
        else []
    )

    if not still_failed:
        return 0

    print(
        f"\n[repo-weaver] ERROR: {len(still_failed)} source(s) exhausted all "
        f"{max_retries} retries and remain in .wiki/failed/:",
        file=sys.stderr,
    )
    for name in still_failed:
        last_reason = str(source_state.get(name, {}).get("last_reason", "unknown"))
        print(f"  - {name}  (last failure: {last_reason})", file=sys.stderr)
    print(
        "[repo-weaver] Inspect .wiki/failed/ and .wiki/.processed.jsonl for details.",
        file=sys.stderr,
    )
    return 1


def _run_ingest_with_retry(
    corpus: str,
    max_cycles: int = _DEFAULT_MAX_CYCLES,
    max_retries: int = _DEFAULT_MAX_RETRIES,
    retry_base_delay: float = _DEFAULT_RETRY_BASE_DELAY,
    _sleep: Optional[Callable[[float], None]] = None,
) -> int:
    """Run ``wiki-weaver ingest`` then auto-retry any ``.wiki/failed/`` sources.

    The initial full-corpus ingest streams directly to the terminal (real-time
    progress).  Per-source retry calls capture output for failure classification.

    Returns 0 if all sources ultimately succeed, non-zero on persistent failure.
    """
    cmd = [
        "wiki-weaver",
        "ingest",
        "--wiki",
        corpus,
        "--max-cycles",
        str(max_cycles),
    ]
    print(f"\n[repo-weaver] Running: {' '.join(cmd)}")
    # Stream the initial full-corpus ingest for real-time terminal feedback.
    initial_result = subprocess.run(cmd)
    initial_rc = initial_result.returncode

    if initial_rc != 0:
        print(
            f"[repo-weaver] WARNING: initial ingest exited {initial_rc}; "
            "checking .wiki/failed/ for retriable sources.",
            file=sys.stderr,
        )

    # Snapshot whether there is anything in .wiki/failed/ to retry BEFORE
    # calling _retry_failed_sources (which drains it down to empty on
    # success).  This lets us tell apart two scenarios that both yield
    # retry_rc == 0:
    #   (a) nothing was in .wiki/failed/ at all -- the initial rc, if
    #       non-zero, reflects a failure the retry mechanism never saw
    #       (e.g. a crash that produced no per-source failure artifacts),
    #       so it must stand.
    #   (b) sources WERE in .wiki/failed/ and every one was recovered --
    #       the run's final state is success, even though the initial
    #       subprocess exit code (e.g. -9 from an OOM kill) was non-zero.
    failed_dir = wiki_failed(Path(corpus))
    had_failures_to_retry = failed_dir.exists() and any(
        p.is_file() for p in failed_dir.iterdir()
    )

    retry_rc = _retry_failed_sources(
        corpus=corpus,
        max_retries=max_retries,
        max_cycles=max_cycles,
        retry_base_delay=retry_base_delay,
        _sleep=_sleep,
    )

    if retry_rc != 0:
        # Retry ran and unresolved failures remain -- report that.
        return retry_rc
    if had_failures_to_retry:
        # Retry ran and fully recovered every source that failed: the final
        # state is success, regardless of the stale initial exit code.
        return 0
    # Nothing was ever in .wiki/failed/ to retry: honour the initial rc
    # as-is (covers the ordinary success case, and a failure mode that
    # never produced per-source failure artifacts for retry to find).
    return initial_rc


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def weave(
    corpus: str,
    repo: str,
    since: Optional[str],
    until: Optional[str],
    max_prs: int = 15,
    max_modules: int = 5,
    dry_run: bool = False,
    max_cycles: int = _DEFAULT_MAX_CYCLES,
    max_retries: int = _DEFAULT_MAX_RETRIES,
    retry_base_delay: float = _DEFAULT_RETRY_BASE_DELAY,
    classify: bool = True,
    no_fetch: bool = False,
    _sleep: Optional[Callable[[float], None]] = None,
) -> int:
    """Materialise source documents and optionally run wiki-weaver ingest.

    Args:
        corpus:           Path to the wiki corpus directory (must be initialised).
        repo:             Path to the local git repository.
        since:            Window start (exclusive). Defaults to one day before the
                          repo's first commit so that the first commit is included.
        until:            Window end (inclusive). Defaults to today.
        max_prs:          Maximum PRs to include in the change digest.
        max_modules:      Maximum module snapshot documents to emit.
        dry_run:          If True, write _inbox files but skip the ingest step.
        max_cycles:       Digest cycle budget passed to ``wiki-weaver ingest``.
                          Default raised to 4 (from wiki-weaver's built-in 3) to
                          handle active repos.
        max_retries:      Max per-source retry attempts after a ``.wiki/failed/`` event.
        retry_base_delay: Base exponential back-off delay in seconds (transient).
        classify:         If True (default), classify PRs as routine/substantive.
        no_fetch:         If True, skip ``git fetch`` staleness check before
                          materialising.  Use for offline/repeatable runs.
        _sleep:           Injectable sleep callable (tests pass a no-op).

    Returns:
        Exit code: 0 = success, non-zero = failure.
    """
    # ---- Validate repo ----
    if not gitio.is_git_repo(repo):
        print(
            f"ERROR: repo is not a valid git repository or is unreachable: {repo!r}\n"
            "Check that the path exists and is inside a git working tree.",
            file=sys.stderr,
        )
        return 1

    # ---- Resolve date defaults ----
    today = date.today().isoformat()
    if until is None:
        until = today

    if since is None:
        first = gitio.get_first_commit_date(repo)
        if first:
            # Move back one day so the first commit falls inside the window.
            first_date = date.fromisoformat(first)
            since = (first_date - timedelta(days=1)).isoformat()
        else:
            since = "2000-01-01"

    print(f"[repo-weaver] Window: {since} \u2192 {until}")
    print(f"[repo-weaver] Repo:   {repo}")
    print(f"[repo-weaver] Corpus: {corpus}")
    if dry_run:
        print("[repo-weaver] Mode:   dry-run (skipping ingest)\n")

    # ---- Archive-skip / idempotency check ----
    # Compute the qualified filename that materialize() will use for this
    # repo+window.  If it already lives in _sources/, the window was already
    # processed — skip loudly rather than re-materialising and re-ingesting.
    # This mirrors the archive-skip in weave_multi(); both must compute
    # file_qualifier the same way so the check is always consistent.
    _origin_url = gitio.get_origin_url(repo)
    _ar_owner_repo = gitio.parse_owner_repo(_origin_url) if _origin_url else None
    if _ar_owner_repo is not None:
        _file_qualifier = f"{_ar_owner_repo[0]}__{_ar_owner_repo[1]}"
    else:
        _file_qualifier = Path(repo).name
    _changes_filename = f"{_file_qualifier}-{until}-changes.md"
    _archive_dir = wiki_sources(Path(corpus))
    if _archive_dir.exists() and (_archive_dir / _changes_filename).exists():
        print(
            f"[repo-weaver] {Path(repo).name} \u2014 "
            f"change digest already archived for window "
            f"{since} \u2192 {until}; skipping."
        )
        return 0

    # ---- Staleness check (Change 4) ----
    _ensure_fresh_clone(repo, no_fetch=no_fetch)

    # ---- Materialise ----
    docs = _materialize(
        repo, since, until, max_prs=max_prs, max_modules=max_modules, classify=classify
    )

    if not docs:
        print(
            "[repo-weaver] No source documents generated for this window.",
            file=sys.stderr,
        )
        return 0

    # ---- Write to _inbox ----
    inbox = wiki_inbox(Path(corpus))
    inbox.mkdir(parents=True, exist_ok=True)

    print(f"[repo-weaver] Writing {len(docs)} source document(s) to {inbox}/")
    for filename, content in docs:
        out_path = inbox / filename
        out_path.write_text(content, encoding="utf-8")
        print(f"  -> {out_path}")

    if dry_run:
        print(
            f"\n[repo-weaver] dry-run complete \u2014 {len(docs)} file(s) written to _inbox/.\n"
            "[repo-weaver] Inspect the files above, then run without --dry-run to ingest."
        )
        return 0

    # ---- Ingest via wiki-weaver (with auto-retry) ----
    return _run_ingest_with_retry(
        corpus=corpus,
        max_cycles=max_cycles,
        max_retries=max_retries,
        retry_base_delay=retry_base_delay,
        _sleep=_sleep,
    )


def weave_multi(
    corpus: str,
    repos: list[str],
    since: Optional[str],
    until: Optional[str],
    max_prs: int = 15,
    max_modules: int = 5,
    dry_run: bool = False,
    max_cycles: int = _DEFAULT_MAX_CYCLES,
    max_retries: int = _DEFAULT_MAX_RETRIES,
    retry_base_delay: float = _DEFAULT_RETRY_BASE_DELAY,
    classify: bool = True,
    no_fetch: bool = False,
    _sleep: Optional[Callable[[float], None]] = None,
) -> int:
    """Materialise source documents for multiple repos and optionally ingest.

    When *repos* contains exactly one entry this delegates to ``weave()``
    so single-repo behaviour is bit-for-bit identical (same qualified
    filenames, same log output, same archive-skip semantics).

    When *repos* contains more than one entry:
      * Each repo is validated before materialisation (``git rev-parse
        --is-inside-work-tree``).  An invalid/unreachable repo aborts the
        entire run loudly — a corpus with phantom-empty entries is worse
        than a hard stop.
      * Each repo is materialised with a ``repo_qualifier`` equal to
        ``Path(repo).name`` (the directory base-name, e.g.
        ``"amplifier-app-team-pulse"``).  This qualifier is injected into
        every filename *and* into the body of every document so the
        synthesiser never merges pages from different repos.
      * All docs from all repos are collected and written to ``_inbox/`` in
        one pass before the single ``wiki-weaver ingest`` call.
      * If *since* is ``None`` each repo derives its own start date
        (one day before that repo's first commit) independently.

    Args:
        corpus:           Path to the wiki corpus directory (must be initialised).
        repos:            Ordered list of absolute paths to local git repositories.
        since:            Window start (exclusive).  ``None`` → per-repo auto-detect.
        until:            Window end (inclusive).  ``None`` → today.
        max_prs:          Maximum PRs to include in each repo's change digest.
        max_modules:      Maximum module snapshot documents to emit per repo.
        dry_run:          If True, write _inbox files but skip the ingest step.
        max_cycles:       Digest cycle budget passed to ``wiki-weaver ingest``.
        max_retries:      Max per-source retry attempts after a ``.wiki/failed/`` event.
        retry_base_delay: Base exponential back-off delay in seconds (transient).
        _sleep:           Injectable sleep callable (tests pass a no-op).

    Returns:
        Exit code: 0 = success, non-zero = failure.
    """
    if not repos:
        print("ERROR: weave_multi called with empty repos list.", file=sys.stderr)
        return 1

    # Single-repo: delegate to weave() for exact backward compatibility.
    if len(repos) == 1:
        return weave(
            corpus=corpus,
            repo=repos[0],
            since=since,
            until=until,
            max_prs=max_prs,
            max_modules=max_modules,
            dry_run=dry_run,
            max_cycles=max_cycles,
            max_retries=max_retries,
            retry_base_delay=retry_base_delay,
            classify=classify,
            no_fetch=no_fetch,
            _sleep=_sleep,
        )

    # ---- Multi-repo path ----
    today = date.today().isoformat()
    effective_until = until if until is not None else today
    total = len(repos)

    print(f"[repo-weaver] Corpus:     {corpus}")
    print(f"[repo-weaver] Repos:      {total} repo(s)")
    print(f"[repo-weaver] Window end: {effective_until}")
    if dry_run:
        print("[repo-weaver] Mode:       dry-run (skipping ingest)")
    print(
        f"\n[repo-weaver] Processing {total} repo(s). "
        "Ingest is sequential and can take several minutes per source.\n"
    )

    # ---- Phase 1: validate all repos before doing any work ----
    for repo in repos:
        if not gitio.is_git_repo(repo):
            print(
                f"\nERROR: repo is not a valid git repository or is unreachable: {repo!r}\n"
                "Fix the repo path or remove it from the corpus config and re-run.",
                file=sys.stderr,
            )
            return 1

    # ---- Phase 2: materialise each repo ----
    all_docs: list[tuple[str, str]] = []
    corpus_path = Path(corpus)
    archive_dir = wiki_sources(corpus_path)

    for idx, repo in enumerate(repos, 1):
        repo_qualifier = Path(repo).name  # e.g. "amplifier-app-team-pulse"

        # Compute the org-scoped file qualifier that materialize() will use for
        # filenames.  This must match the internal logic in mat.materialize() so
        # the archive-skip check below compares against the actual on-disk name.
        # When the owner is known: "owner__repo" (double-underscore, no slash).
        # When there is no remote: basename fallback — never fabricate an owner.
        _origin = gitio.get_origin_url(repo)
        _owner_repo = gitio.parse_owner_repo(_origin) if _origin else None
        if _owner_repo is not None:
            file_qualifier = f"{_owner_repo[0]}__{_owner_repo[1]}"
        else:
            file_qualifier = repo_qualifier

        # Per-repo since resolution (needed before the archive-skip check).
        if since is not None:
            effective_since = since
        else:
            first = gitio.get_first_commit_date(repo)
            if first:
                first_date = date.fromisoformat(first)
                effective_since = (first_date - timedelta(days=1)).isoformat()
            else:
                effective_since = "2000-01-01"

        # Resume skip: if this repo's change digest is already in _sources/
        # for this window, there is no point materialising and re-ingesting it
        # (wiki-weaver would dedup-skip it anyway).  Skip loudly so the user
        # can see what was spared on a resumed run.
        changes_filename = f"{file_qualifier}-{effective_until}-changes.md"
        if archive_dir.exists() and (archive_dir / changes_filename).exists():
            print(
                f"[repo {idx}/{total}] {repo_qualifier} \u2014 "
                f"change digest already archived for window "
                f"{effective_since} \u2192 {effective_until}; skipping."
            )
            continue

        print(f"[repo {idx}/{total}] {repo_qualifier} \u2014 materializing\u2026")
        print(f"[repo-weaver]   Window: {effective_since} \u2192 {effective_until}")

        # Staleness check (Change 4)
        _ensure_fresh_clone(repo, no_fetch=no_fetch)

        docs = _materialize(
            repo=repo,
            since=effective_since,
            until=effective_until,
            max_prs=max_prs,
            max_modules=max_modules,
            repo_qualifier=repo_qualifier,
            classify=classify,
        )

        if not docs:
            print(
                f"[repo {idx}/{total}] {repo_qualifier} \u2014 no documents in window."
            )
        else:
            print(
                f"[repo {idx}/{total}] {repo_qualifier} \u2014 "
                f"{len(docs)} doc(s) queued for ingest."
            )
            all_docs.extend(docs)

    if not all_docs:
        print(
            "\n[repo-weaver] No source documents generated for any repo in this window.",
            file=sys.stderr,
        )
        return 0

    # ---- Write to _inbox ----
    inbox = wiki_inbox(Path(corpus))
    inbox.mkdir(parents=True, exist_ok=True)

    print(f"\n[repo-weaver] Writing {len(all_docs)} source document(s) to {inbox}/")
    for filename, content in all_docs:
        out_path = inbox / filename
        out_path.write_text(content, encoding="utf-8")
        print(f"  -> {out_path}")

    if dry_run:
        print(
            f"\n[repo-weaver] dry-run complete \u2014 {len(all_docs)} file(s) written to _inbox/.\n"
            "[repo-weaver] Inspect the files above, then run without --dry-run to ingest."
        )
        return 0

    # ---- Ingest via wiki-weaver (with auto-retry) ----
    print(
        f"\n[repo-weaver] Ingesting {len(all_docs)} source(s). "
        "Ingest is sequential — this can take several minutes per source.\n"
    )
    return _run_ingest_with_retry(
        corpus=corpus,
        max_cycles=max_cycles,
        max_retries=max_retries,
        retry_base_delay=retry_base_delay,
        _sleep=_sleep,
    )


# ---------------------------------------------------------------------------
# Replay orchestration: multi-window with resume-from-checkpoint
# ---------------------------------------------------------------------------


def replay_windows(
    corpus: str,
    repos: list[str],
    windows: list[tuple[str, str]],
    max_prs: int = 15,
    max_modules: int = 5,
    max_cycles: int = _DEFAULT_MAX_CYCLES,
    max_retries: int = _DEFAULT_MAX_RETRIES,
    retry_base_delay: float = _DEFAULT_RETRY_BASE_DELAY,
    classify: bool = True,
    restart: bool = False,
    no_fetch: bool = False,
    _sleep: Optional[Callable[[float], None]] = None,
) -> int:
    """Replay successive time windows with resume-from-checkpoint support.

    Progress is tracked in ``<corpus>/.replay-progress.json``.  On a re-run
    with the same ``windows`` list, completed windows are skipped and
    processing resumes at the first incomplete one.

    **Fail-loud guarantee**: a window is marked complete ONLY when
    ``weave_multi`` returns 0.  A window that failed (sources in ``.wiki/failed/``,
    provider error, etc.) is never recorded as done, so the next run
    re-attempts it — including any sources still in ``.wiki/failed/`` from the
    prior run, which ``_retry_failed_sources`` will pick up and retry.

    Args:
        corpus:           Path to the wiki corpus directory.
        repos:            Ordered list of git repo paths.
        windows:          Ordered list of ``(since, until)`` pairs.
        classify:         If True (default), classify PRs as routine/substantive.
                          Pass False to implement ``--no-classify`` A/B testing.
        restart:          If True, ignore and clear any existing progress file,
                          forcing a full redo from the first window.
        *rest:            Forwarded verbatim to ``weave_multi``.

    Returns:
        0 if all windows completed successfully; non-zero on first failure.
    """
    corpus_path = Path(corpus)
    total = len(windows)

    # ---- Load (or clear) progress ----
    if restart:
        prog_file = corpus_path / _REPLAY_PROGRESS_FILE
        if prog_file.exists():
            prog_file.unlink()
            print(
                "[repo-weaver] --restart: cleared replay progress; redoing all windows."
            )
        completed: set[str] = set()
    else:
        completed = _load_replay_progress(corpus_path)

    skipped = sum(
        1 for since, until in windows if _window_key(since, until) in completed
    )
    if skipped and not restart:
        print(
            f"[repo-weaver] Resume: {skipped}/{total} window(s) already completed; "
            f"skipping and resuming at the first incomplete window."
        )

    print(
        f"[repo-weaver] Replay: {total} window(s) across "
        f"{len(repos)} repo(s). "
        "Ingest is sequential and can take several minutes per window.\n"
    )

    for idx, (since, until) in enumerate(windows, 1):
        key = _window_key(since, until)

        if key in completed:
            print(
                f"[window {idx}/{total}] {since} \u2192 {until}  "
                "[SKIP \u2014 already completed]"
            )
            continue

        print(f"[window {idx}/{total}] {since} \u2192 {until}")
        rc = weave_multi(
            corpus=corpus,
            repos=repos,
            since=since,
            until=until,
            max_prs=max_prs,
            max_modules=max_modules,
            dry_run=False,
            max_cycles=max_cycles,
            max_retries=max_retries,
            retry_base_delay=retry_base_delay,
            classify=classify,
            no_fetch=no_fetch,
            _sleep=_sleep,
        )
        if rc != 0:
            print(
                f"\nERROR: ingest failed for window {since} \u2192 {until} "
                f"(exit {rc}).  Stopping replay.\n"
                "[repo-weaver] Re-run without --restart to resume from this window.",
                file=sys.stderr,
            )
            return rc

        # Mark complete ONLY on success — fail-loud guarantee.
        completed.add(key)
        _save_replay_progress(corpus_path, completed)

    print("\n[repo-weaver] Replay complete.")
    return 0


# ---------------------------------------------------------------------------
# Importable lib: init() and ask()
# ---------------------------------------------------------------------------


def init(
    corpus: str,
    repos: Optional[list[str]] = None,
) -> int:
    """Scaffold a corpus directory and install the code-fit schema.

    This is the importable equivalent of ``repo-weaver init``.  It:

    1. Runs ``wiki-weaver init <corpus> --plain`` to scaffold the wiki layout.
    2. Copies the bundled ``repo_weaver/policy/schema.md`` into the corpus so
       that ``wiki-weaver ingest`` understands the code-fit entity model.
    3. Saves the list of registered repo paths into ``.repo-weaver.json``.

    Args:
        corpus: Path to the corpus directory (will be created if absent).
        repos:  Optional list of absolute or relative repo paths to register.
                Paths are resolved to absolute before saving.  Pass ``None``
                (or omit) to create a repo-less corpus.

    Returns:
        0 on success, non-zero on failure (mirrors CLI exit-code convention).
    """
    print(f"[repo-weaver] Initialising wiki at {corpus} ...")
    r = subprocess.run(["wiki-weaver", "init", corpus, "--plain"])
    if r.returncode != 0:
        print(
            f"ERROR: wiki-weaver init failed (exit {r.returncode})",
            file=sys.stderr,
        )
        return r.returncode

    # Install code-fit schema — REQUIRED: a corpus without a schema is broken.
    if not _POLICY_SCHEMA.exists():
        print(
            "ERROR: policy/schema.md not found in the repo-weaver package.\n"
            "This is required for wiki-weaver to understand the corpus structure.\n"
            "Reinstall repo-weaver:  pip install --force-reinstall repo-weaver\n"
            "                   OR:  uv tool install --reinstall repo-weaver",
            file=sys.stderr,
        )
        return 1

    policy_dst = Path(corpus) / "policy"
    policy_dst.mkdir(parents=True, exist_ok=True)
    schema_dst = policy_dst / "schema.md"
    shutil.copy2(_POLICY_SCHEMA, schema_dst)
    print(f"[repo-weaver] Installed schema: {schema_dst}")

    # Save corpus config (list of repo absolute paths).
    cfg: dict[str, object] = {}
    if repos:
        repo_paths = [str(Path(rp).resolve()) for rp in repos]
        cfg["repos"] = repo_paths
        for rp in repo_paths:
            origin = gitio.get_origin_url(rp)
            label = f" ({origin})" if origin else ""
            print(f"[repo-weaver] Registered repo: {rp}{label}")

    _save_corpus_config(corpus, cfg)
    print(f"[repo-weaver] Corpus config: {Path(corpus) / _CORPUS_CONFIG}")
    print("[repo-weaver] Done.  Run `repo-weaver weave --corpus <dir>` to populate.")
    return 0


def ask(
    question: str,
    corpus: str,
    output_json: bool = False,
) -> int:
    """Query the corpus and print the answer via ``wiki-weaver ask``.

    This is the importable equivalent of ``repo-weaver ask``.  It delegates
    to ``wiki-weaver ask``, which reads the corpus, retrieves relevant pages,
    and synthesises a cited answer.  Output is written to stdout exactly as
    ``wiki-weaver`` produces it.

    Args:
        question:    The natural-language question to answer.
        corpus:      Path to an initialised and populated wiki corpus directory.
        output_json: If True, appends ``--json`` so wiki-weaver returns
                     ``{"answer": ..., "pages_used": ..., "refused": ...}``
                     instead of plain text.

    Returns:
        The exit code from ``wiki-weaver ask`` (0 = answered, non-zero = error
        or refused).
    """
    cmd = ["wiki-weaver", "ask", question, "--wiki", corpus]
    if output_json:
        cmd.append("--json")
    r = subprocess.run(cmd)
    return r.returncode
